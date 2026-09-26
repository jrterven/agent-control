from __future__ import annotations

import asyncio
import base64
from collections import deque
import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import inspect
import json
from pathlib import Path
import random
import re
import ssl
import sqlite3
import time
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from hermes_client import HermesGatewayProvider, ProviderConnection
from hermes_client.connector_protocol import (FrameReader, MAX_FRAME_BYTES, OPERATIONS, VERSION, WRITE_OPERATIONS,
                                             ProtocolError, decode_message, encode_message, send_message, validate_arguments)
from hermes_client.provider import HermesProvider, ProfileManagementServerRequired, RuntimeGenerationChanged, SessionHistoryNotFound
from hermes_client.types import CapabilitySet, NormalizedEvent, PromptAttachment, SessionRoute
from . import __version__
from .media import project_media, read_media
from .storage import OperationLedger, atomic_json
from .tls import cloud_ssl_context
from .media_install import media_profiles
from .background_install import background_profiles
from .chat_modes_install import chat_mode_profiles
from .background_tasks import retired_profile, snapshot as background_snapshot, unavailable as background_unavailable
from hermes_client.compatibility import HERMES_0212_SHA, profile_contract_supports
from .profile_transfer import ProfileImportManagementServerRequired, ProfileImportRefused, ProfileTransfers, TRANSFER_OPERATIONS
from .hermes_media_plugin import queue_directory, validate_policy
from .visual_media import acknowledge as acknowledge_media, next_publication, profile_home, fetch_image, normalize_image

ACTIVE = {"pending", "queued", "accepted", "starting", "streaming", "running", "working", "waiting"}
SAFE_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")


def connection_error_code(error: Exception) -> str:
    """Report actionable transport failures without exception text or secrets."""
    if isinstance(error, ssl.SSLCertVerificationError):
        return "CLOUD_TLS_CERTIFICATE_INVALID"
    if isinstance(error, ssl.SSLError):
        return "CLOUD_TLS_FAILED"
    if isinstance(error, InvalidStatus):
        return "CLOUD_ACCESS_REJECTED" if error.response.status_code in {401, 403} else "CLOUD_HANDSHAKE_FAILED"
    if isinstance(error, ProtocolError):
        return "CLOUD_PROTOCOL_MISMATCH"
    if isinstance(error, TimeoutError):
        return "CLOUD_CONNECTION_TIMEOUT"
    return "CLOUD_CONNECTION_FAILED"


class ConnectorRuntime:
    def __init__(self, directory: Path, config: dict, secrets: dict, provider_factory=HermesGatewayProvider):
        self.directory, self.config, self.secrets = directory, config, secrets
        self.gateway_id = config["gatewayId"]
        self.provider_factory = provider_factory
        profiles = config["profiles"]
        # A previously paired connector can become empty after its last named
        # profile was retired. Pairing itself still requires an approved profile.
        if not isinstance(profiles, list) or len(profiles) > 64 or any(not isinstance(p, str) or not SAFE_PROFILE.fullmatch(p) for p in profiles):
            raise ValueError("Invalid locally approved profiles")
        self.providers = {}
        for profile in profiles:
            connection = ProviderConnection(gateway_id=self.gateway_id, profile_name=profile,
                rest_url=config["restUrl"], ws_url=config["wsUrl"],
                dashboard_token=secrets["hermesToken"], trusted_source_sha=config["sourceSha"])
            self.providers[profile] = provider_factory(connection, self.on_event)
        self.ledger = OperationLedger(directory)
        self.temporary_ledger = OperationLedger(None)
        self.temporary_receipts = {}
        self.temporary_expiries = {}
        self.websocket = None
        self.send_lock = asyncio.Lock()
        self.events: deque[tuple[int, dict, int]] = deque()
        self.event_bytes = 0
        self.sequence = 0
        self.replay_lost = False
        self.event_changed = asyncio.Event()
        self.tasks: set[asyncio.Task] = set()
        self.closed = False
        self.active_work: bool | None = None
        self.connection_error: str | None = None
        self.visual_media_supported = False
        self.media_states: dict[str, dict] = {}
        self.media_pending: dict[str, str] = {}
        self.private_media_pending: dict[str, tuple[str, str]] = {}
        self.media_install_at = 0.0
        self.visual_media_limits = validate_policy(None)
        self.background_tasks_supported = False
        self.background_states: dict[str, dict] = {}
        self.background_install_at = 0.0
        self.chat_modes_install_at = 0.0
        self.background_fingerprints: dict[tuple[str, str], str] = {}
        self.profile_transfer_supported = False
        self.profile_transfers = ProfileTransfers(self)
        from .updates import installation
        self.update_installation = installation(directory)
        self.update_launch_at = 0.0

    async def _background_events(self, profile: str, snapshot: dict):
        if (profile not in self.providers or self.websocket is None or not self.background_tasks_supported
                or self.background_states.get(profile, {}).get("state") != "ready"):
            return
        grouped: dict[str, list] = {}
        for task in snapshot["tasks"]:
            grouped.setdefault(task["storedSessionId"], []).append(task)
        previous = {session for name, session in self.background_fingerprints if name == profile}
        for session_id in sorted(set(grouped) | previous):
            tasks = grouped.get(session_id, [])
            complete = snapshot.get("complete") is True
            # A failed/truncated read cannot certify that a missing task ended.
            data = {"available": snapshot.get("available") is True, "complete": complete,
                "tasks": tasks, "totalCount": len(tasks) if complete else None,
                "activeCount": sum(task["state"] in {"running", "queued"} for task in tasks) if complete else None,
                "pendingDeliveryCount": sum(task["state"] not in {"running", "queued"} and task["deliveryState"] == "pending" for task in tasks) if complete else None,
                "source": "hermes-native-delegation"}
            fingerprint = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
            key = (profile, session_id)
            if self.background_fingerprints.get(key) == fingerprint:
                continue
            await self.on_event(NormalizedEvent.create(type="background.tasks", gateway_id=self.gateway_id,
                profile_name=profile, stored_session_id=session_id, runtime_session_id=None,
                runtime_generation=self.providers[profile].runtime_generation,
                data={**data, "observedAt": snapshot["observedAt"]}))
            self.background_fingerprints[key] = fingerprint
        # Bound diagnostic caches independently of upstream lifecycle retention.
        while len(self.background_fingerprints) > 2048:
            self.background_fingerprints.pop(next(iter(self.background_fingerprints)))

    async def _forget_deleted_profile(self, name: str):
        """Drop only the local sharing state of a confirmed native deletion.

        Persist first so a crash/reconnect cannot re-register this profile.
        This never removes native files or changes another profile's grants.
        """
        if name == "default":
            raise ValueError("The default profile cannot be deleted")
        replacement = {**self.config, "profiles": [profile for profile in self.config["profiles"] if profile != name]}
        atomic_json(self.directory / "config.json", replacement)
        self.config = replacement
        removed = self.providers.pop(name, None)
        self.media_states.pop(name, None)
        self.background_states.pop(name, None)
        self.media_pending = {key: profile for key, profile in self.media_pending.items() if profile != name}
        self.background_fingerprints = {key: value for key, value in self.background_fingerprints.items() if key[0] != name}
        self.events = deque((sequence, message, size) for sequence, message, size in self.events
                            if message["event"].profile_name != name)
        self.event_bytes = sum(size for _, _, size in self.events)
        self.event_changed.set()
        if removed is not None:
            with contextlib.suppress(Exception):
                await removed.close()

    async def _confirm_profile_deleted(self, provider, name: str):
        # A valid native tombstone is also authoritative when the manager was
        # the deleted profile and can no longer answer profile-scoped RPCs.
        retired = await asyncio.to_thread(self._background_snapshot, name)
        if retired.get("retired") is not True:
            if name in {profile.name for profile in await provider.list_profiles()}:
                raise ValueError("Native profile deletion is not confirmed")
        await self._forget_deleted_profile(name)

    def _background_snapshot(self, profile: str, stored_session_id: str | None = None) -> dict:
        if self.config.get("sourceSha") != HERMES_0212_SHA:
            return background_unavailable()
        try:
            if stored_session_id is None:
                retired = retired_profile(Path(self.config["hermesHome"]), profile)
                if retired is not None:
                    return retired
            home = profile_home(Path(self.config["hermesHome"]), profile)
            return background_snapshot(home, stored_session_id)
        except (OSError, ValueError):
            return background_unavailable()

    def _save_media_policy(self, profiles=None):
        for profile in profiles or self.config["profiles"]:
            try:
                if (self.config.get("sourceSha") == HERMES_0212_SHA
                        and retired_profile(Path(self.config["hermesHome"]), profile) is not None):
                    continue
                home = profile_home(Path(self.config["hermesHome"]), profile)
                queue_directory(home)
                atomic_json(home / ".agent-control/media/policy.json", self.visual_media_limits)
            except (OSError, ValueError):
                self.media_states[profile] = {"state": "policyUnavailable"}

    async def on_event(self, event: NormalizedEvent):
        if event.profile_name not in self.providers:
            return
        if str(event.stored_session_id or "").startswith("ac_tmp_") and (event.profile_name, event.stored_session_id) not in self.temporary_expiries:
            return
        self.sequence += 1
        message = {"v": VERSION, "type": "event", "sequence": self.sequence, "event": event}
        size = len(encode_message(message))
        self.events.append((self.sequence, message, size))
        self.event_bytes += size
        while len(self.events) > 2048 or self.event_bytes > 8 * 1024 * 1024:
            _, _, removed = self.events.popleft()
            self.event_bytes -= removed
            self.replay_lost = True
        self.event_changed.set()

    def _forget_temporary(self, profile, stored_id):
        route = (profile, stored_id)
        self.temporary_expiries.pop(route, None)
        self.temporary_ledger.discard_receipts(self.temporary_receipts.pop(route, ()))
        self.events = deque((sequence, message, size) for sequence, message, size in self.events
                            if (message["event"].profile_name, message["event"].stored_session_id) != route)
        self.event_bytes = sum(size for _, _, size in self.events)
        for identifier, owner in list(self.private_media_pending.items()):
            if owner == route:
                self.private_media_pending.pop(identifier, None)
                self.media_pending.pop(identifier, None)
        self.event_changed.set()

    def _acknowledge(self, sequence):
        if type(sequence) is not int or sequence > self.sequence:
            raise ProtocolError("Invalid event acknowledgement")
        while self.events and self.events[0][0] <= sequence:
            _, _, size = self.events.popleft()
            self.event_bytes -= size

    async def _event_sender(self, websocket):
        sent = -1
        while self.websocket is websocket:
            self.event_changed.clear()
            for sequence, message, _ in tuple(self.events):
                if sequence > sent and message["event"].profile_name in self.providers:
                    await send_message(websocket.send, self.send_lock, message)
                    sent = sequence
            await self.event_changed.wait()

    async def _heartbeat(self, websocket):
        from .updates import diagnostic
        while self.websocket is websocket:
            await send_message(websocket.send, self.send_lock, {"v": VERSION, "type": "heartbeat", "version": __version__,
                "updater": diagnostic(self.directory, self.update_installation),
                "profiles": {name: {"generation": provider.runtime_generation,
                    "inventoryComplete": provider.session_inventory_complete,
                    "visualMedia": self.media_states.get(name, {"state": "pendingActivation"}),
                    "backgroundTasks": self.background_states.get(name, {"state": "pendingActivation"})} for name, provider in self.providers.items()}})
            await asyncio.sleep(15)

    async def _media_sender(self, websocket):
        while self.websocket is websocket:
            for profile, provider in tuple(self.providers.items()):
                try:
                    home = profile_home(Path(self.config["hermesHome"]), profile)
                    publication = None
                    if "session.mode.temporary" in getattr(provider, "chat_mode_features", ()):
                        pending = await provider._read("control.media.pending")
                        if pending.get("id"):
                            content = base64.b64decode(pending["content"], validate=True) if pending.get("content") else await asyncio.to_thread(fetch_image, pending["url"], self.visual_media_limits)
                            content, thumbnail, metadata = await asyncio.to_thread(normalize_image, content, self.visual_media_limits)
                            publication = {"v": VERSION, "type": "media.publish", "id": pending["id"], "profile": profile,
                                "sessionId": pending["sessionId"], "metadata": {**pending["metadata"], **metadata}, "content": content, "thumbnail": thumbnail}
                            self.private_media_pending[pending["id"]] = (profile, pending["sessionId"])
                    if publication is None and (home / ".agent-control/media/outbox.sqlite3").is_file():
                        publication = await asyncio.to_thread(next_publication, home, profile)
                    if publication is None:
                        continue
                    # Bind every publication to an actual stored session on this
                    # provider. Never import another profile's files or route.
                    await asyncio.wait_for(provider.history_readonly(publication["sessionId"]), 15)
                    if self.providers.get(profile) is not provider:
                        continue
                    self.media_pending[publication["id"]] = profile
                    await send_message(websocket.send, self.send_lock, publication)
                except SessionHistoryNotFound:
                    if publication and publication["id"] in self.private_media_pending:
                        await self._media_acknowledge({"id": publication["id"], "status": "failed", "errorCode": "forbidden"})
                    elif publication:
                        await asyncio.to_thread(acknowledge_media, home, publication["id"], "failed", "forbidden")
                except (OSError, ValueError, ConnectionError, TimeoutError):
                    # Durable outbox retries after reconnect or transient local failure.
                    continue
                except sqlite3.Error:
                    self.media_states[profile] = {"state": "outboxUnavailable"}
            await asyncio.sleep(.25)

    async def _media_acknowledge(self, message):
        identifier = message.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier) or message.get("status") not in {"ready", "failed"}:
            raise ProtocolError("Invalid media acknowledgement")
        error = message.get("errorCode")
        if error is not None and (not isinstance(error, str) or not re.fullmatch(r"[a-zA-Z0-9_]{1,80}", error)):
            raise ProtocolError("Invalid media acknowledgement")
        profile = self.media_pending.pop(identifier, None)
        private = self.private_media_pending.pop(identifier, None)
        if private:
            provider = self.providers.get(private[0])
            if provider:
                await provider._read("control.media.ack", {**message, "sessionId": private[1]})
            return
        if profile:
            await asyncio.to_thread(acknowledge_media, profile_home(Path(self.config["hermesHome"]), profile), identifier, message["status"], error)

    async def _status_loop(self):
        while not self.closed:
            for (profile, stored_id), expires in list(self.temporary_expiries.items()):
                if time.monotonic() >= expires:
                    self._forget_temporary(profile, stored_id)
            # A maintenance acknowledgement describes a scan performed after
            # this exact request, never an earlier idle snapshot.
            marker = self.directory / "maintenance.request"
            try:
                maintenance_request_id = marker.read_text() if not marker.is_symlink() and marker.stat().st_size <= 128 else None
            except OSError:
                maintenance_request_id = None
            active: bool | None = False
            retired_profiles = set()
            for provider in tuple(self.providers.values()):
                # Session idle does not imply the native delegated workers (or
                # a completion waiting to wake its parent) are idle. Read the
                # profile-local ledger even if Hermes's HTTP inventory fails.
                background = await asyncio.to_thread(self._background_snapshot, provider.connection.profile_name)
                if background.get("retired") is True:
                    retired_profiles.add(provider.connection.profile_name)
                observer = getattr(provider, "observe_background_tasks", None)
                if callable(observer):
                    observer(background)
                await self._background_events(provider.connection.profile_name, background)
                if background.get("retired") is True:
                    # Native tombstones cover both empty SQLite shells and a
                    # fully absent directory; do not reopen a retired runtime.
                    continue
                if (background.get("activeCount") or 0) > 0 or (background.get("pendingDeliveryCount") or 0) > 0:
                    active = True
                elif not background.get("complete") and active is not True:
                    active = None
                try:
                    available_profiles = await asyncio.wait_for(provider.list_profiles(), 10)
                    if provider.connection.profile_name not in {item.name for item in available_profiles}:
                        continue
                    if "session.mode.temporary" in getattr(provider, "chat_mode_features", ()):
                        policy_status = await asyncio.wait_for(provider._read("control.chat_modes"), 10)
                        if policy_status.get("activeTemporary", 0) > 0:
                            active = True
                    sessions = await asyncio.wait_for(provider.list_sessions(), 10)
                    if any(session.status in ACTIVE for session in sessions):
                        active = True
                    elif not provider.session_inventory_complete and active is not True:
                        active = None
                except Exception:
                    if active is not True:
                        active = None
            if self.tasks or self.profile_transfers.has_pending() or self.temporary_expiries:
                active = True
            self.active_work = active
            now = asyncio.get_running_loop().time()
            live_config = {**self.config, "profiles": [name for name in self.config["profiles"] if name not in retired_profiles]}
            if now >= self.chat_modes_install_at and active is False:
                await asyncio.to_thread(chat_mode_profiles, live_config, install=True)
                self.chat_modes_install_at = now + 30
            if self.visual_media_supported and now >= self.media_install_at:
                self.media_states = await asyncio.to_thread(media_profiles, live_config, install=active is False)
                self.media_states.update({name: {"state": "retired"} for name in retired_profiles})
                self.media_install_at = now + 30
            if self.background_tasks_supported and now >= self.background_install_at:
                self.background_states = await asyncio.to_thread(background_profiles, live_config, install=active is False)
                self.background_states.update({name: {"state": "retired"} for name in retired_profiles})
                self.background_install_at = now + 30
            atomic_json(self.directory / "status.json", {"activeWork": active, "fresh": True,
                "observedAt": datetime.now(timezone.utc).isoformat(), "connected": self.websocket is not None,
                "connectionError": self.connection_error,
                "version": __version__, "maintenance": maintenance_request_id is not None,
                "release": self.update_installation["release"] if self.update_installation else None,
                "temporaryChats": len(self.temporary_expiries),
                "maintenanceRequestId": maintenance_request_id, "visualMedia": self.media_states,
                "backgroundTasks": self.background_states})
            await asyncio.sleep(3)

    async def execute(self, message: dict):
        request_id = message.get("id")
        profile, operation = message.get("profile"), message.get("operation")
        args, kwargs = message.get("args"), message.get("kwargs")
        operation_id = message.get("operationId")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-f0-9]{32}", request_id):
            raise ProtocolError("Invalid operation identity")
        response = {"v": VERSION, "type": "response", "id": request_id, "profile": profile}
        ledger_key = None
        ledger = self.ledger
        provider = self.providers.get(profile)
        try:
            if operation not in OPERATIONS or (provider is None and operation != "delete_profile"):
                raise ValueError("INVALID_OPERATION")
            if not isinstance(args, tuple) or not isinstance(kwargs, dict) or len(args) > 4 or len(kwargs) > 8:
                raise ValueError("INVALID_OPERATION")
            if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 200:
                raise ValueError("INVALID_OPERATION")
            if operation == "media":
                if len(args) != 2 or kwargs or any(not isinstance(a, str) or not 1 <= len(a) <= 200 for a in args):
                    raise ValueError("INVALID_OPERATION")
            else:
                validate_arguments(operation, args, kwargs)
            if operation in TRANSFER_OPERATIONS:
                if not self.profile_transfer_supported or self.config.get("sourceSha") != HERMES_0212_SHA:
                    raise ValueError("INVALID_OPERATION")
                if operation == "profile_export":
                    name = args[0] if args else kwargs.get("name")
                    if name not in self.providers or name == "default":
                        raise ValueError("INVALID_OPERATION")
            for arg in (*args, *kwargs.values()):
                if isinstance(arg, SessionRoute) and (arg.gateway_id != self.gateway_id or arg.profile_name != profile):
                    raise ValueError("INVALID_OPERATION")
                if isinstance(arg, PromptAttachment) and (len(arg.content) > 8 * 1024 * 1024 or arg.kind not in {"image", "file"}):
                    raise ValueError("INVALID_OPERATION")
            if operation == "delete_profile" and (len(args) != 1 or not isinstance(args[0], str)
                    or not SAFE_PROFILE.fullmatch(args[0]) or args[0] == "default"):
                raise ValueError("INVALID_OPERATION")
            if operation == "create_profile":
                name = kwargs.get("name")
                if not isinstance(name, str) or not SAFE_PROFILE.fullmatch(name) or len(self.providers) >= 64:
                    raise ValueError("INVALID_OPERATION")
            if (kwargs.get("chat_mode") == "temporary" or any(isinstance(arg, SessionRoute) and arg.stored_session_id.startswith("ac_tmp_") for arg in (*args, *kwargs.values()))):
                ledger = self.temporary_ledger
            if operation in WRITE_OPERATIONS:
                if (self.directory / "maintenance.request").exists():
                    raise ValueError("CONNECTOR_MAINTENANCE")
                next_ledger_key = f"{profile}:{operation}:{operation_id}"
                digest = hashlib.sha256(encode_message({"v": VERSION, "profile": profile, "operation": operation, "args": args, "kwargs": kwargs})).hexdigest()
                previous = ledger.lookup(next_ledger_key, digest)
                if provider is None:
                    # A completed self-delete may be replayed after restart,
                    # but must never reconstruct a provider for its old name.
                    if previous is None:
                        raise ValueError("INVALID_OPERATION")
                    state, receipt = previous
                    if state == "completed":
                        response.update(decode_message(receipt))
                        response["id"] = request_id
                    else:
                        response["error"] = "IDEMPOTENCY_CONFLICT" if state == "conflict" else "CONNECTOR_DELIVERY_UNKNOWN"
                    return response
                if operation == "delete_profile" and previous is None and args[0] not in self.providers:
                    raise ValueError("INVALID_OPERATION")
                if (operation == "delete_profile" and previous is None
                        and self.config.get("sourceSha") == HERMES_0212_SHA):
                    await provider.assert_default_management_server()
                if (self.config.get("sourceSha") == HERMES_0212_SHA
                        and operation in {"delete_profile", "delete_session"}
                        and previous is None):
                    target_profile = args[0] if operation == "delete_profile" else profile
                    route = (args[0] if args else kwargs.get("route")) if operation == "delete_session" else None
                    evidence = await asyncio.to_thread(self._background_snapshot, target_profile,
                        route.stored_session_id if route else None)
                    if (evidence.get("complete") is not True or type(evidence.get("activeCount")) is not int
                            or type(evidence.get("pendingDeliveryCount")) is not int
                            or evidence["activeCount"] != 0 or evidence["pendingDeliveryCount"] != 0):
                        response["error"] = "CONNECTOR_BACKGROUND_BUSY"
                        return response
                    if (self.directory / "maintenance.request").exists():
                        raise ValueError("CONNECTOR_MAINTENANCE")
                ledger_key = next_ledger_key
                state, previous = ledger.reserve(ledger_key, digest)
                if state == "completed":
                    response.update(decode_message(previous))
                    response["id"] = request_id
                    return response
                if state != "new":
                    response["error"] = "IDEMPOTENCY_CONFLICT" if state == "conflict" else "PROMPT_DELIVERY_UNKNOWN" if operation == "submit_prompt" else "CONNECTOR_DELIVERY_UNKNOWN"
                    return response
            if operation == "create_profile":
                # Check after durable replay lookup so a retried completed create
                # returns its receipt, while an existing unshared agent stays private.
                if kwargs["name"] in {item.name for item in await provider.list_profiles()}:
                    raise ValueError("INVALID_OPERATION")
            if operation in TRANSFER_OPERATIONS:
                result = await self.profile_transfers.execute(operation, profile, args, kwargs)
            elif operation == "list_background_tasks":
                # Drain safety needs the native ledger even before installation
                # or after opt-out. Availability only controls the chat feature.
                stored_id = args[0] if args else kwargs.get("stored_session_id")
                if stored_id and stored_id.startswith("ac_tmp_"):
                    result = await provider._read("control.session.background", {"stored_session_id": stored_id})
                else:
                    result = await asyncio.to_thread(self._background_snapshot, profile, stored_id)
                if not stored_id and "session.mode.temporary" in getattr(provider, "chat_mode_features", ()):
                    policy_status = await provider._read("control.chat_modes")
                    result["activeCount"] = (result.get("activeCount") or 0) + policy_status.get("activeTemporary", 0)
                result["available"] = (result.get("available") is True and self.background_tasks_supported
                    and self.background_states.get(profile, {}).get("state") == "ready")
            elif operation == "media":
                history = await provider.history_readonly(args[0])
                result = read_media(history, Path(self.config["hermesHome"]), profile, args[0], args[1])
            else:
                result = await getattr(provider, operation)(*args, **kwargs)
            if operation == "delete_profile":
                await self._confirm_profile_deleted(provider, args[0])
            if operation in {"create_profile", "profile_import_finish"}:
                name = kwargs["name"] if operation == "create_profile" else result.name
                if result.name != name:
                    raise ValueError("INVALID_OPERATION")
                self.config["profiles"] = list(dict.fromkeys([*self.config["profiles"], name]))
                atomic_json(self.directory / "config.json", self.config)
                self.providers[name] = self.provider_factory(replace(provider.connection, profile_name=name), self.on_event)
                if self.visual_media_supported:
                    try:
                        await asyncio.to_thread(self._save_media_policy, [name])
                        # Newly created profiles cannot have active agent work
                        # yet; install before returning the creation receipt.
                        self.media_states.update(await asyncio.to_thread(media_profiles,
                            {**self.config, "profiles": [name]}, install=True))
                    except (OSError, ValueError):
                        self.media_states[name] = {"state": "installationFailed"}
                await asyncio.to_thread(chat_mode_profiles, {**self.config, "profiles": [name]}, install=True)
                if self.background_tasks_supported:
                    self.background_states.update(await asyncio.to_thread(background_profiles,
                        {**self.config, "profiles": [name]}, install=True))
            if operation in {"history", "history_readonly"}:
                session_id = args[0].stored_session_id if operation == "history" else args[0]
                result = project_media(result, Path(self.config["hermesHome"]), profile, session_id)
            if operation == "list_profiles":
                result = [item for item in result if item.name in self.providers]
            if operation == "capabilities":
                result = replace(result, features=result.features | {"connector.historyPageV1"})
                if (self.profile_transfer_supported
                        and self.config.get("sourceSha") == HERMES_0212_SHA
                        and profile_contract_supports(self.config.get("sourceSha"), result.version, "profiles.transfer")
                        and {"profiles.export", "profiles.import", "profiles.transfer"} <= result.methods):
                    result = replace(result, features=(result.features - {"connector.profileTransferV1", "connector.profileTransferV2"}) | {"connector.profileTransferV3"})
                else:
                    result = replace(result, methods=result.methods - {"profiles.transfer", "profiles.export", "profiles.import"},
                                     features=result.features - {"profiles.transfer", "connector.profileTransferV1", "connector.profileTransferV2", "connector.profileTransferV3"})
            response["result"] = result
        except RuntimeGenerationChanged:
            response["error"] = "RUNTIME_GENERATION_CHANGED"
        except SessionHistoryNotFound:
            response["error"] = "SESSION_HISTORY_NOT_FOUND"
        except ProfileImportManagementServerRequired:
            response["error"] = "PROFILE_TRANSFER_DEFAULT_SERVER_REQUIRED"
        except ProfileManagementServerRequired:
            response["error"] = "HERMES_DEFAULT_SERVER_REQUIRED"
        except ProfileImportRefused:
            response["error"] = "PROFILE_TRANSFER_IMPORT_REFUSED"
        except (ConnectionError, OSError, TimeoutError):
            response["error"] = "PROMPT_DELIVERY_UNKNOWN" if operation == "submit_prompt" and ledger_key else "CONNECTOR_DELIVERY_UNKNOWN" if ledger_key else "CONNECTOR_OFFLINE"
        except (ValueError, TypeError, LookupError):
            response["error"] = "INVALID_OPERATION" if ledger_key is None else "CONNECTOR_DELIVERY_UNKNOWN"
        except Exception:
            # Do not expose Hermes messages, tokens, or host paths over errors.
            response["error"] = "PROMPT_DELIVERY_UNKNOWN" if operation == "submit_prompt" else "CONNECTOR_OPERATION_FAILED"
        finally:
            if provider:
                response["generation"] = provider.runtime_generation
                response["inventoryComplete"] = provider.session_inventory_complete
            else:
                response.setdefault("generation", "unknown")
                response.setdefault("inventoryComplete", False)
        if ledger_key:
            ledger.finish(ledger_key, encode_message(response))
            if ledger is self.temporary_ledger:
                stored_id = getattr(response.get("result"), "stored_session_id", None)
                route = next((arg for arg in (*args, *kwargs.values()) if isinstance(arg, SessionRoute)), None)
                stored_id = route.stored_session_id if route else stored_id
                if stored_id:
                    key = (profile, stored_id)
                    self.temporary_receipts.setdefault(key, set()).add(ledger_key)
                    if "error" not in response and operation in {"create_session", "renew_temporary_session"}:
                        self.temporary_expiries[key] = time.monotonic() + 300
                    elif operation == "close_temporary_session":
                        self._forget_temporary(profile, stored_id)
        return response

    async def _respond(self, message, websocket):
        response = await self.execute(message)
        if self.websocket is websocket:
            with contextlib.suppress(Exception):
                await send_message(websocket.send, self.send_lock, response)

    async def _connection(self):
        url = self.config["server"].replace("https://", "wss://", 1) + "/api/v1/connectors/ws"
        async with connect(url, additional_headers={"Authorization": "Bearer " + self.secrets["accessToken"]},
                           max_size=MAX_FRAME_BYTES, max_queue=8, ping_interval=15, ping_timeout=30,
                           open_timeout=15, close_timeout=5, compression=None, proxy=None,
                           happy_eyeballs_delay=0.25,
                           ssl=cloud_ssl_context()) as websocket:
            reader = FrameReader()
            welcome = None
            while welcome is None:
                welcome = reader.feed(await asyncio.wait_for(websocket.recv(), 20))
            if welcome.get("type") != "welcome" or welcome.get("gatewayId") != self.gateway_id or not set(self.providers) <= set(welcome.get("profiles", [])):
                raise ProtocolError("Cloud identity or approved profiles changed; pair again")
            capabilities = welcome.get("capabilities")
            visual_media_supported = isinstance(capabilities, dict) and capabilities.get("visualMediaV1") is True
            if visual_media_supported:
                try:
                    self.visual_media_limits = validate_policy(welcome.get("visualMediaLimits"))
                except ValueError as error:
                    raise ProtocolError("Invalid visual media policy") from error
                await asyncio.to_thread(self._save_media_policy)
            self.visual_media_supported = visual_media_supported
            self.background_tasks_supported = isinstance(capabilities, dict) and capabilities.get("backgroundTasksV1") is True
            self.profile_transfer_supported = isinstance(capabilities, dict) and capabilities.get("profileTransferV3") is True
            # Preserve known sessions to emit authoritative empty inventories
            # after reconnect, while forcing a first snapshot on this transport.
            self.background_fingerprints = {key: "" for key in self.background_fingerprints}
            self.websocket = websocket
            self.connection_error = None
            # New cloud process may have lost its replay cursor; always request an
            # authoritative reconciliation after reconnect, then replay bounded events.
            for name, provider in self.providers.items():
                await self.on_event(NormalizedEvent.create(type="control.reconcile", gateway_id=self.gateway_id,
                    profile_name=name, runtime_generation=provider.runtime_generation,
                    data={"reason": "connector_reconnected", "historyRequired": True, "gap": self.replay_lost}))
            self.replay_lost = False
            sender = asyncio.create_task(self._event_sender(websocket))
            heartbeat = asyncio.create_task(self._heartbeat(websocket))
            media_sender = asyncio.create_task(self._media_sender(websocket)) if self.visual_media_supported else None
            try:
                async for raw in websocket:
                    message = reader.feed(raw)
                    if message is None:
                        continue
                    if message.get("type") == "ack":
                        self._acknowledge(message.get("sequence"))
                    elif message.get("type") == "media.ack" and self.visual_media_supported:
                        await self._media_acknowledge(message)
                    elif message.get("type") == "update.control":
                        from .updates import accept_control, launch_safely
                        accept_control(self.directory, message)
                        if time.monotonic() >= self.update_launch_at:
                            self.update_launch_at = time.monotonic() + 60
                            await asyncio.to_thread(launch_safely, self.directory, self.update_installation)
                    elif message.get("type") == "request":
                        if len(self.tasks) >= 8:
                            raise ProtocolError("Too many connector operations")
                        task = asyncio.create_task(self._respond(message, websocket))
                        self.tasks.add(task)
                        task.add_done_callback(self.tasks.discard)
                    else:
                        raise ProtocolError("Unknown cloud message")
            finally:
                self.websocket = None
                sender.cancel()
                heartbeat.cancel()
                if media_sender:
                    media_sender.cancel()
                await asyncio.gather(sender, heartbeat, *([media_sender] if media_sender else []), return_exceptions=True)
                self.media_pending.clear()
                # Deliberately keep local providers and dispatched operations alive.

    async def run(self):
        status = asyncio.create_task(self._status_loop())
        attempt = 0
        try:
            while not self.closed:
                try:
                    await self._connection()
                    self.connection_error = "CLOUD_DISCONNECTED"
                    attempt = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.connection_error = connection_error_code(exc)
                    attempt += 1
                await asyncio.sleep(min(2 ** min(attempt, 5), 30) + random.random())
        finally:
            self.closed = True
            self.websocket = None
            status.cancel()
            await asyncio.gather(status, return_exceptions=True)
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            for provider in tuple(self.providers.values()):
                await provider.close()
            self.ledger.close()
            self.temporary_ledger.close()
            atomic_json(self.directory / "status.json", {"activeWork": None, "fresh": False,
                "observedAt": datetime.now(timezone.utc).isoformat(), "connected": False,
                "connectionError": self.connection_error, "version": __version__, "maintenance": False})
