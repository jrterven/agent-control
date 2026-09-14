from __future__ import annotations

import asyncio
from collections import deque
import contextlib
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import inspect
from pathlib import Path
import random
import re
from typing import Any
from uuid import uuid4

from websockets.asyncio.client import connect
from hermes_client import HermesGatewayProvider, ProviderConnection
from hermes_client.connector_protocol import (FrameReader, MAX_FRAME_BYTES, OPERATIONS, VERSION, WRITE_OPERATIONS,
                                             ProtocolError, decode_message, encode_message, send_message, validate_arguments)
from hermes_client.provider import HermesProvider, RuntimeGenerationChanged, SessionHistoryNotFound
from hermes_client.types import CapabilitySet, NormalizedEvent, PromptAttachment, SessionRoute
from . import __version__
from .media import project_media, read_media
from .storage import OperationLedger, atomic_json

ACTIVE = {"pending", "queued", "accepted", "starting", "streaming", "running", "working", "waiting"}
SAFE_PROFILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")


class ConnectorRuntime:
    def __init__(self, directory: Path, config: dict, secrets: dict, provider_factory=HermesGatewayProvider):
        self.directory, self.config, self.secrets = directory, config, secrets
        self.gateway_id = config["gatewayId"]
        self.provider_factory = provider_factory
        profiles = config["profiles"]
        if not 1 <= len(profiles) <= 64 or any(not isinstance(p, str) or not SAFE_PROFILE.fullmatch(p) for p in profiles):
            raise ValueError("Invalid locally approved profiles")
        self.providers = {}
        for profile in profiles:
            connection = ProviderConnection(gateway_id=self.gateway_id, profile_name=profile,
                rest_url=config["restUrl"], ws_url=config["wsUrl"],
                dashboard_token=secrets["hermesToken"], trusted_source_sha=config["sourceSha"])
            self.providers[profile] = provider_factory(connection, self.on_event)
        self.ledger = OperationLedger(directory)
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

    async def on_event(self, event: NormalizedEvent):
        if event.profile_name not in self.providers:
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
                if sequence > sent:
                    await send_message(websocket.send, self.send_lock, message)
                    sent = sequence
            await self.event_changed.wait()

    async def _heartbeat(self, websocket):
        while self.websocket is websocket:
            await send_message(websocket.send, self.send_lock, {"v": VERSION, "type": "heartbeat", "version": __version__,
                "profiles": {name: {"generation": provider.runtime_generation,
                    "inventoryComplete": provider.session_inventory_complete} for name, provider in self.providers.items()}})
            await asyncio.sleep(15)

    async def _status_loop(self):
        while not self.closed:
            # A maintenance acknowledgement describes a scan performed after
            # this exact request, never an earlier idle snapshot.
            marker = self.directory / "maintenance.request"
            try:
                maintenance_request_id = marker.read_text() if not marker.is_symlink() and marker.stat().st_size <= 128 else None
            except OSError:
                maintenance_request_id = None
            active: bool | None = False
            for provider in tuple(self.providers.values()):
                try:
                    available_profiles = await asyncio.wait_for(provider.list_profiles(), 10)
                    if provider.connection.profile_name not in {item.name for item in available_profiles}:
                        continue
                    sessions = await asyncio.wait_for(provider.list_sessions(), 10)
                    if any(session.status in ACTIVE for session in sessions):
                        active = True
                    elif not provider.session_inventory_complete and active is not True:
                        active = None
                except Exception:
                    if active is not True:
                        active = None
            if self.tasks:
                active = True
            self.active_work = active
            atomic_json(self.directory / "status.json", {"activeWork": active, "fresh": True,
                "observedAt": datetime.now(timezone.utc).isoformat(), "connected": self.websocket is not None,
                "version": __version__, "maintenance": maintenance_request_id is not None,
                "maintenanceRequestId": maintenance_request_id})
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
        provider = self.providers.get(profile)
        try:
            if provider is None or operation not in OPERATIONS:
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
            for arg in (*args, *kwargs.values()):
                if isinstance(arg, SessionRoute) and (arg.gateway_id != self.gateway_id or arg.profile_name != profile):
                    raise ValueError("INVALID_OPERATION")
                if isinstance(arg, PromptAttachment) and (len(arg.content) > 8 * 1024 * 1024 or arg.kind not in {"image", "file"}):
                    raise ValueError("INVALID_OPERATION")
            if operation == "delete_profile" and (len(args) != 1 or args[0] not in self.providers):
                raise ValueError("INVALID_OPERATION")
            if operation == "create_profile":
                name = kwargs.get("name")
                if not isinstance(name, str) or not SAFE_PROFILE.fullmatch(name) or len(self.providers) >= 64:
                    raise ValueError("INVALID_OPERATION")
            if operation in WRITE_OPERATIONS:
                if (self.directory / "maintenance.request").exists():
                    raise ValueError("CONNECTOR_MAINTENANCE")
                ledger_key = f"{profile}:{operation}:{operation_id}"
                digest = hashlib.sha256(encode_message({"v": VERSION, "profile": profile, "operation": operation, "args": args, "kwargs": kwargs})).hexdigest()
                state, previous = self.ledger.reserve(ledger_key, digest)
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
            if operation == "media":
                history = await provider.history_readonly(args[0])
                result = read_media(history, Path(self.config["hermesHome"]), profile, args[0], args[1])
            else:
                result = await getattr(provider, operation)(*args, **kwargs)
            if operation == "create_profile":
                name = kwargs["name"]
                if result.name != name:
                    raise ValueError("INVALID_OPERATION")
                self.config["profiles"] = list(dict.fromkeys([*self.config["profiles"], name]))
                atomic_json(self.directory / "config.json", self.config)
                self.providers[name] = self.provider_factory(replace(provider.connection, profile_name=name), self.on_event)
            if operation in {"history", "history_readonly"}:
                session_id = args[0].stored_session_id if operation == "history" else args[0]
                result = project_media(result, Path(self.config["hermesHome"]), profile, session_id)
            if operation == "list_profiles":
                result = [item for item in result if item.name in self.providers]
            if operation == "capabilities":
                result = replace(result, methods=result.methods - {"profiles.transfer", "profiles.export", "profiles.import"},
                                 features=result.features - {"profiles.transfer"})
            response["result"] = result
        except RuntimeGenerationChanged:
            response["error"] = "RUNTIME_GENERATION_CHANGED"
        except SessionHistoryNotFound:
            response["error"] = "SESSION_HISTORY_NOT_FOUND"
        except (ConnectionError, OSError, TimeoutError):
            response["error"] = "PROMPT_DELIVERY_UNKNOWN" if operation == "submit_prompt" and ledger_key else "CONNECTOR_DELIVERY_UNKNOWN" if ledger_key else "CONNECTOR_OFFLINE"
        except (ValueError, TypeError, LookupError):
            response["error"] = "INVALID_OPERATION" if ledger_key is None else "CONNECTOR_DELIVERY_UNKNOWN"
        except Exception:
            # Do not expose Hermes messages, tokens, or host paths over errors.
            response["error"] = "PROMPT_DELIVERY_UNKNOWN" if operation == "submit_prompt" else "CONNECTOR_OPERATION_FAILED"
        finally:
            response["generation"] = provider.runtime_generation if provider else "unknown"
            response["inventoryComplete"] = bool(provider and provider.session_inventory_complete)
        if ledger_key:
            self.ledger.finish(ledger_key, encode_message(response))
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
                           open_timeout=15, close_timeout=5, compression=None, proxy=None) as websocket:
            reader = FrameReader()
            welcome = None
            while welcome is None:
                welcome = reader.feed(await asyncio.wait_for(websocket.recv(), 20))
            if welcome.get("type") != "welcome" or welcome.get("gatewayId") != self.gateway_id or not set(self.providers) <= set(welcome.get("profiles", [])):
                raise ProtocolError("Cloud identity or approved profiles changed; pair again")
            self.websocket = websocket
            # New cloud process may have lost its replay cursor; always request an
            # authoritative reconciliation after reconnect, then replay bounded events.
            for name, provider in self.providers.items():
                await self.on_event(NormalizedEvent.create(type="control.reconcile", gateway_id=self.gateway_id,
                    profile_name=name, runtime_generation=provider.runtime_generation,
                    data={"reason": "connector_reconnected", "historyRequired": True, "gap": self.replay_lost}))
            self.replay_lost = False
            sender = asyncio.create_task(self._event_sender(websocket))
            heartbeat = asyncio.create_task(self._heartbeat(websocket))
            try:
                async for raw in websocket:
                    message = reader.feed(raw)
                    if message is None:
                        continue
                    if message.get("type") == "ack":
                        self._acknowledge(message.get("sequence"))
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
                await asyncio.gather(sender, heartbeat, return_exceptions=True)
                # Deliberately keep local providers and dispatched operations alive.

    async def run(self):
        status = asyncio.create_task(self._status_loop())
        attempt = 0
        try:
            while not self.closed:
                try:
                    await self._connection()
                    attempt = 0
                except asyncio.CancelledError:
                    raise
                except Exception:
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
            atomic_json(self.directory / "status.json", {"activeWork": None, "fresh": False,
                "observedAt": datetime.now(timezone.utc).isoformat(), "connected": False, "version": __version__, "maintenance": False})
