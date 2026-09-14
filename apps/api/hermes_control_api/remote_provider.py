"""Cloud side of the personal connector. Never opens a user's network endpoint."""
from __future__ import annotations

import asyncio
import contextlib
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from hermes_client import ProviderConnection
from hermes_client.connector_protocol import OPERATIONS, VERSION, WRITE_OPERATIONS, ProtocolError, send_message, type_hints, value_matches_type
from hermes_client.provider import HermesProvider, RuntimeGenerationChanged, SessionHistoryNotFound
from hermes_client.types import CapabilitySet, NormalizedEvent


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
        self.online = True

    async def call(self, profile: str, operation: str, args: tuple, kwargs: dict):
        if not self.online or profile not in self.profiles:
            raise ConnectionError("Connector is offline or profile is not shared")
        if operation not in OPERATIONS or len(self.pending) >= 8:
            raise ConnectionError("Connector operation unavailable")
        if operation == "create_profile" and self.prepare_create is not None:
            await self.prepare_create(kwargs.get("name"))
        request_id = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        self.request_profiles[request_id] = profile
        try:
            await send_message(self.send_bytes, self.lock, {
                "v": VERSION, "type": "request", "id": request_id,
                "profile": profile, "operation": operation, "args": args, "kwargs": kwargs,
                "operationId": str(kwargs.get("operation_id") or request_id),
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
            return
        if message.get("type") != "event":
            raise ProtocolError("Unknown connector message")
        event = message.get("event")
        if not isinstance(event, NormalizedEvent) or event.profile_name not in link.profiles:
            raise ProtocolError("Unshared connector event")
        # A authenticated device may only publish into its assigned gateway.
        event.gateway_id = link.gateway_id
        link.generations[event.profile_name] = event.runtime_generation or "unknown"
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
        return CapabilitySet(protocol=value.protocol, version=value.version, source_sha=value.source_sha,
                             methods=value.methods - {"profiles.transfer", "profiles.export", "profiles.import"},
                             features=value.features - {"profiles.transfer"})

    async def transfer_profile_to(self, destination, *, name):
        raise ValueError("Profile transfers are unavailable for cloud connectors")

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
