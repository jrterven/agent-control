"""Cloud side of the personal connector. Never opens a user's network endpoint."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from hermes_client import ProviderConnection
from hermes_client.connector_protocol import OPERATIONS, VERSION, WRITE_OPERATIONS, ProtocolError, send_message, type_hints, value_matches_type
from hermes_client.provider import HermesProvider, RuntimeGenerationChanged, SessionHistoryNotFound
from hermes_client.types import CapabilitySet, NormalizedEvent


class BackgroundTasksBusyError(RuntimeError):
    """A rejected destructive operation was not dispatched to Hermes."""


class ProfileTransferNotImported(RuntimeError):
    """The transfer did not create a native destination profile."""


class ProfileTransferOutcomeUnknown(RuntimeError):
    """Import may have happened; retain the source and never delete an unowned copy."""


class ConnectorLink:
    def __init__(self, gateway_id: str, profiles: frozenset[str], send_bytes, close_socket):
        self.gateway_id = gateway_id
        self.profiles = profiles
        self.send_bytes = send_bytes
        self.close_socket = close_socket
        self.lock = asyncio.Lock()
        self.pending: dict[str, asyncio.Future] = {}
        self.request_profiles: dict[str, str] = {}
        self.prepare_create = None
        self.generations: dict[str, str] = {}
        self.inventory_complete: dict[str, bool] = {}
        self.background_task_profiles: set[str] = set()
        self.online = True

    async def call(self, profile: str, operation: str, args: tuple, kwargs: dict, *, operation_id: str | None = None):
        if not self.online or profile not in self.profiles:
            raise ConnectionError("Connector is offline or profile is not shared")
        if operation not in OPERATIONS or len(self.pending) >= 8:
            raise ConnectionError("Connector operation unavailable")
        if operation in {"create_profile", "profile_import_begin"} and self.prepare_create is not None:
            await self.prepare_create(kwargs.get("name"))
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.request_profiles[request_id] = profile
        try:
            await send_message(self.send_bytes, self.lock, {
                "v": VERSION, "type": "request", "id": request_id,
                "profile": profile, "operation": operation, "args": args, "kwargs": kwargs,
                "operationId": operation_id or str(kwargs.get("operation_id") or request_id),
            })
            return await asyncio.wait_for(future, 180 if operation in WRITE_OPERATIONS else 60)
        except (ConnectionError, OSError, TimeoutError, asyncio.CancelledError):
            if operation == "submit_prompt":
                raise RuntimeError("PROMPT_DELIVERY_UNKNOWN") from None
            if operation in WRITE_OPERATIONS:
                raise RuntimeError("CONNECTOR_DELIVERY_UNKNOWN") from None
            raise
        finally:
            self.pending.pop(request_id, None)
            self.request_profiles.pop(request_id, None)

    def response(self, message: dict):
        future = self.pending.get(message.get("id"))
        if future is None or future.done():
            return
        profile = message.get("profile")
        if profile != self.request_profiles.get(message.get("id")):
            raise ProtocolError("Response profile does not match the request")
        self.generations[profile] = str(message.get("generation") or "unknown")
        self.inventory_complete[profile] = message.get("inventoryComplete") is True
        error = message.get("error")
        if error:
            exceptions = {
                "RUNTIME_GENERATION_CHANGED": RuntimeGenerationChanged,
                "SESSION_HISTORY_NOT_FOUND": SessionHistoryNotFound,
                "CONNECTOR_OFFLINE": ConnectionError,
                "INVALID_OPERATION": ValueError,
                "CONNECTOR_BACKGROUND_BUSY": BackgroundTasksBusyError,
                "PROFILE_TRANSFER_IMPORT_REFUSED": ProfileTransferNotImported,
            }
            future.set_exception(exceptions.get(str(error), RuntimeError)(str(error)))
        else:
            future.set_result(message.get("result"))

    async def close(self):
        self.online = False
        for future in tuple(self.pending.values()):
            if not future.done():
                future.set_exception(ConnectionError("Connector disconnected"))
        with contextlib.suppress(RuntimeError, OSError):
            await self.close_socket()


class ConnectorRegistry:
    """One API worker owns links; the database remains the revocation authority."""
    def __init__(self, event_sink: Callable[[NormalizedEvent], Awaitable[None]]):
        self.event_sink = event_sink
        self.links: dict[str, ConnectorLink] = {}
        self.event_ids: OrderedDict[str, None] = OrderedDict()

    def get(self, gateway_id: str) -> ConnectorLink:
        link = self.links.get(gateway_id)
        if link is None or not link.online:
            raise ConnectionError("Connector is offline")
        return link

    def online(self, gateway_id: str) -> bool:
        return bool((link := self.links.get(gateway_id)) and link.online)

    async def register(self, link: ConnectorLink):
        previous = self.links.get(link.gateway_id)
        if previous:
            await previous.close()
        self.links[link.gateway_id] = link

    async def disconnect(self, gateway_id: str, link: ConnectorLink | None = None):
        current = self.links.get(gateway_id)
        if current is not None and (link is None or current is link):
            del self.links[gateway_id]
            try:
                await current.close()
            finally:
                for profile in current.profiles:
                    await self.event_sink(NormalizedEvent.create(type="control.connection", gateway_id=gateway_id,
                        profile_name=profile, data={"state": "offline"}))

    async def receive(self, link: ConnectorLink, message: dict):
        if message.get("type") == "response":
            link.response(message)
            return
        if message.get("type") == "heartbeat":
            states = message.get("profiles", {})
            if not isinstance(states, dict) or len(states) > 64:
                raise ProtocolError("Invalid connector heartbeat")
            for profile, state in states.items():
                if profile not in link.profiles or not isinstance(state, dict):
                    raise ProtocolError("Invalid connector profile state")
                link.generations[profile] = str(state.get("generation") or "unknown")
                link.inventory_complete[profile] = state.get("inventoryComplete") is True
                if isinstance(state.get("backgroundTasks"), dict):
                    link.background_task_profiles.add(profile)
            return
        if message.get("type") != "event":
            raise ProtocolError("Unknown connector message")
        event = message.get("event")
        if not isinstance(event, NormalizedEvent) or event.profile_name not in link.profiles:
            raise ProtocolError("Unshared connector event")
        # A authenticated device may only publish into its assigned gateway.
        event.gateway_id = link.gateway_id
        if event.runtime_generation:
            link.generations[event.profile_name] = event.runtime_generation
        if event.type == "background.tasks":
            from .background_tasks import project_snapshot
            if not event.stored_session_id or event.runtime_session_id or event.sequence is not None:
                raise ProtocolError("Invalid background task route")
            event.data = project_snapshot(event.data, event.stored_session_id, observed_at=event.timestamp.isoformat())
        key = f"{link.gateway_id}:{event.profile_name}:{event.event_id}"
        if key not in self.event_ids:
            await self.event_sink(event)
            self.event_ids[key] = None
            if len(self.event_ids) > 20_000:
                self.event_ids.popitem(last=False)
        await send_message(link.send_bytes, link.lock, {"v": VERSION, "type": "ack", "sequence": message.get("sequence")})

    async def close(self):
        for gateway_id in list(self.links):
            await self.disconnect(gateway_id)


class RemoteProvider:
    def __init__(self, connection: ProviderConnection, registry: ConnectorRegistry):
        self.connection = connection
        self.registry = registry

    @property
    def runtime_generation(self) -> str:
        try:
            return self.registry.get(self.connection.gateway_id).generations.get(self.connection.profile_name, "connector-unknown")
        except ConnectionError:
            return "connector-offline"

    @property
    def session_inventory_complete(self) -> bool:
        try:
            return self.registry.get(self.connection.gateway_id).inventory_complete.get(self.connection.profile_name, False)
        except ConnectionError:
            return False

    async def _call(self, operation: str, *args, **kwargs):
        link = self.registry.get(self.connection.gateway_id)
        result = await link.call(self.connection.profile_name, operation, args, kwargs)
        if operation != "media" and not value_matches_type(result, type_hints(getattr(HermesProvider, operation))["return"]):
            raise ProtocolError("Invalid typed connector result")
        return result

    async def capabilities(self):
        value = await self._call("capabilities")
        if not isinstance(value, CapabilitySet):
            raise ProtocolError("Invalid connector capabilities")
        if "connector.profileTransferV2" in value.features:
            return value
        return CapabilitySet(protocol=value.protocol, version=value.version, source_sha=value.source_sha,
                             methods=value.methods - {"profiles.delete", "profiles.transfer", "profiles.export", "profiles.import"},
                             features=value.features - {"profiles.transfer"})

    async def transfer_profile_to(self, destination, *, name):
        if not isinstance(destination, RemoteProvider) or destination.registry is not self.registry:
            raise ProfileTransferNotImported("Cloud profile transfer requires two personal connectors")
        if destination.connection.gateway_id == self.connection.gateway_id or name == "default":
            raise ProfileTransferNotImported("Choose another gateway and a named profile")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name):
            raise ProfileTransferNotImported("Invalid profile name")
        try:
            source_caps, destination_caps = await self.capabilities(), await destination.capabilities()
        except BaseException as error:
            raise ProfileTransferNotImported("Could not verify transfer capabilities") from error
        if any("connector.profileTransferV2" not in caps.features for caps in (source_caps, destination_caps)):
            raise ProfileTransferNotImported("Update both connectors before moving an agent")

        # Only one bounded chunk is held in cloud memory. The independent local
        # stages and operation ledger retain uncertain outcomes across reconnects;
        # an interrupted transfer is never automatically re-imported.
        transfer_id = uuid4().hex
        maximum, chunk_size = 100 * 1024 * 1024, 1024 * 1024
        import_dispatched = False

        async def call(provider, operation, phase, **kwargs):
            link = provider.registry.get(provider.connection.gateway_id)
            result = await link.call(provider.connection.profile_name, operation, (), kwargs,
                                     operation_id=f"{transfer_id}:{phase}")
            if not value_matches_type(result, type_hints(getattr(HermesProvider, operation))["return"]):
                raise ProtocolError("Invalid typed profile transfer result")
            return result

        def receipt(value, *, size=None, checksum=None, offset=0):
            if (not isinstance(value, dict) or value.get("transferId") != transfer_id
                    or type(value.get("size")) is not int or not 0 < value["size"] <= maximum
                    or type(value.get("offset")) is not int or value["offset"] != offset
                    or not isinstance(value.get("sha256"), str)
                    or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
                    or (size is not None and value["size"] != size)
                    or (checksum is not None and value["sha256"] != checksum)):
                raise ProtocolError("Invalid profile archive receipt")
            return value

        try:
            exported = receipt(await call(self, "profile_export", "export", name=name, transfer_id=transfer_id))
            size, checksum = exported["size"], exported["sha256"]
            receipt(await call(destination, "profile_import_begin", "begin", name=name,
                               transfer_id=transfer_id, size=size, sha256=checksum), size=size, checksum=checksum)
            digest = hashlib.sha256()
            offset = 0
            while offset < size:
                length = min(chunk_size, size - offset)
                chunk = await call(self, "profile_archive_read", f"read:{offset}",
                                   transfer_id=transfer_id, offset=offset, length=length)
                if len(chunk) != length:
                    raise ProtocolError("Incomplete profile archive chunk")
                digest.update(chunk)
                receipt(await call(destination, "profile_archive_write", f"write:{offset}",
                                   transfer_id=transfer_id, offset=offset, chunk=chunk),
                        size=size, checksum=checksum, offset=offset + length)
                offset += length
            if digest.hexdigest() != checksum:
                raise ProtocolError("Profile archive checksum mismatch")
            import_dispatched = True
            result = await call(destination, "profile_import_finish", "import", transfer_id=transfer_id)
            if result.name != name:
                raise ProtocolError("Imported profile identity mismatch")
            return result
        except ProfileTransferNotImported:
            raise
        except BaseException as error:
            if import_dispatched:
                raise ProfileTransferOutcomeUnknown("Profile import needs reconciliation; source retained") from error
            raise ProfileTransferNotImported("Profile archive transfer stopped before import") from error
        finally:
            for provider in (self, destination):
                # Best-effort staging cleanup must not replace the proven
                # ownership/uncertainty outcome with a cancellation exception.
                with contextlib.suppress(Exception, asyncio.CancelledError):
                    await asyncio.wait_for(call(provider, "profile_archive_cleanup", "cleanup",
                                                transfer_id=transfer_id), timeout=15)

    async def close(self):
        # Profile cache invalidation must not disconnect the independently owned device.
        return None


def _remote_operation(name: str):
    async def operation(self, *args, **kwargs):
        return await self._call(name, *args, **kwargs)
    operation.__name__ = name
    return operation


# Explicit protocol allowlist only; no arbitrary __getattr__ network invocation.
for _name in OPERATIONS - {"capabilities"}:
    setattr(RemoteProvider, _name, _remote_operation(_name))
