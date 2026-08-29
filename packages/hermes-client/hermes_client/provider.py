from __future__ import annotations

import asyncio
import logging
import random
from collections import OrderedDict, defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import quote
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from croniter import croniter

from .admin import (
    AdminResourceSnapshot,
    admin_snapshot,
    contains_secret_fields,
    sanitize_admin_payload,
    writable_config_projection,
)
from .limits import (
    UpstreamPayloadError,
    bounded_empty_request,
    bounded_json_request,
)
from .normalization import EventNormalizer
from .transport import (
    JsonRpcClient,
    JsonRpcDisconnected,
    JsonRpcError,
    JsonRpcGenerationChanged,
    reconnecting_call,
)
from .types import (
    CapabilitySet,
    HermesAutomation,
    HermesProfile,
    HermesRunReceipt,
    HermesSearchResult,
    HermesSession,
    NormalizedEvent,
    PromptReceipt,
    SessionRoute,
)


EventSink = Callable[[NormalizedEvent], Awaitable[None]]
_LOGGER = logging.getLogger("hermes_control.provider")


class RuntimeGenerationChanged(ConnectionError):
    """The route was validated on a different Hermes connection generation."""


class SessionHistoryNotFound(LookupError):
    """Hermes has no durable transcript row for the requested stored session."""

_MAX_PROFILES = 64
_MAX_SESSIONS = 2_000
_MAX_MESSAGES = 5_000
_MAX_AUTOMATIONS = 1_000
_MAX_ADMIN_ROWS = 2_000
_MAX_SEARCH_RESULTS = 100


def _bounded_text(value: Any, *, label: str, max_length: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length:
        raise UpstreamPayloadError(f"Hermes {label} is missing or too long")
    return text


def _bounded_rows(
    value: Any,
    *,
    key: str,
    label: str,
    max_items: int,
) -> list[Mapping[str, Any]]:
    rows = value.get(key, value) if isinstance(value, Mapping) else value
    if rows is None:
        return []
    if not isinstance(rows, list):
        raise UpstreamPayloadError(f"Hermes {label} must be a list")
    if len(rows) > max_items:
        raise UpstreamPayloadError(f"Hermes returned too many {label}")
    if any(not isinstance(item, Mapping) for item in rows):
        raise UpstreamPayloadError(f"Hermes {label} contain an invalid item")
    return list(rows)


_AUDITED_REVISIONS: dict[str, tuple[str, frozenset[str], frozenset[str]]] = {
    "791e2ae3257e211d14ca77e654dfe10ee1976a1c": (
        "0.20.5",
        frozenset(
            {
                "session.create",
                "session.resume",
                "session.status",
                "session.history",
                "prompt.submit",
                "session.interrupt",
                "approval.respond",
                "clarify.respond",
                "session.events.since",
                "session.delete",
            }
        ),
        frozenset({"cron.create", "cron.update", "cron.delete", "cron.trigger"}),
    ),
    "9978706e9303dbf990d90e744b131361449d73b9": (
        "0.20.6",
        frozenset(
            {
                "session.create",
                "session.resume",
                "session.status",
                "session.history",
                "prompt.submit",
                "session.interrupt",
                "approval.respond",
                "clarify.respond",
                "session.events.since",
                "session.delete",
            }
        ),
        frozenset({"cron.create", "cron.update", "cron.delete", "cron.trigger"}),
    ),
}

# Every read probe is a side-effect-free GET. A write is advertised only when
# its related read succeeded and the full Hermes revision is one we audited.
# Never probe a mutation to discover whether it exists.
_ADMIN_READ_PROBES: dict[str, str] = {
    "models.list": "/api/model/options",
    "config.get": "/api/config",
    "soul.get": "/api/profiles/{profile}/soul",
    "memory.get": "/api/memory",
    "skills.list": "/api/skills",
    "toolsets.list": "/api/tools/toolsets",
    "mcp.list": "/api/mcp/servers",
    "channels.list": "/api/messaging/platforms",
    "usage.get": "/api/analytics/usage",
    "secrets.list": "/api/env",
}
_ADMIN_WRITES_BY_READ: dict[str, frozenset[str]] = {
    "models.list": frozenset({"models.set"}),
    "config.get": frozenset({"config.set"}),
    "soul.get": frozenset({"soul.set"}),
    "memory.get": frozenset({"memory.provider.set", "memory.reset"}),
    "skills.list": frozenset({"skills.toggle"}),
    "toolsets.list": frozenset({"toolsets.toggle"}),
    "mcp.list": frozenset(
        {"mcp.create", "mcp.delete", "mcp.toggle", "mcp.test"}
    ),
    "channels.list": frozenset({"channels.update", "channels.test"}),
    "secrets.list": frozenset({"secrets.set", "secrets.delete"}),
}

# Neither audited Hermes target scopes /api/memory to the selected profile:
# the query parameter is accepted but ignored. Keep the real provider fail
# closed until a future exact revision is audited as profile-aware.
_PROFILE_AWARE_MEMORY_REVISIONS: frozenset[str] = frozenset()

_OFFICIAL_API_SESSION_ENDPOINTS: dict[str, tuple[str, str, str]] = {
    "sessions": ("GET", "/api/sessions", "session.list"),
    "session_create": ("POST", "/api/sessions", "session.create"),
    "session_delete": (
        "DELETE",
        "/api/sessions/{session_id}",
        "session.delete",
    ),
    "session_messages": (
        "GET",
        "/api/sessions/{session_id}/messages",
        "session.history",
    ),
    "session_chat": (
        "POST",
        "/api/sessions/{session_id}/chat",
        "prompt.submit",
    ),
}


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    gateway_id: str
    profile_name: str
    rest_url: str
    ws_url: str
    api_url: str | None = None
    dashboard_token: str | None = None
    api_key: str | None = None
    rest_connect_host: str | None = None
    ws_connect_host: str | None = None
    api_connect_host: str | None = None
    trusted_source_sha: str | None = None


class _PinnedNetworkBackend:
    """Pin HTTP TCP dials to the address approved by endpoint validation."""

    def __init__(self, delegate: Any, *, expected_host: str, connect_host: str) -> None:
        self.delegate = delegate
        self.expected_host = expected_host.casefold()
        self.connect_host = connect_host

    async def connect_tcp(self, host: str, port: int, **kwargs):
        if host.casefold() != self.expected_host:
            raise OSError("Unvalidated HTTP destination")
        return await self.delegate.connect_tcp(self.connect_host, port, **kwargs)

    async def connect_unix_socket(self, path: str, **kwargs):
        return await self.delegate.connect_unix_socket(path, **kwargs)

    async def sleep(self, seconds: float) -> None:
        await self.delegate.sleep(seconds)


def _pinned_http_transport(url: str, connect_host: str | None) -> httpx.AsyncHTTPTransport:
    transport = httpx.AsyncHTTPTransport(trust_env=False, retries=0)
    if connect_host:
        hostname = urlsplit(url).hostname
        if not hostname:
            raise ValueError("Pinned endpoint must include a hostname")
        pool = transport._pool  # httpx 0.28/httpcore 1.x, pinned by project constraints.
        pool._network_backend = _PinnedNetworkBackend(
            pool._network_backend,
            expected_host=hostname,
            connect_host=connect_host,
        )
    return transport


class HermesProvider(Protocol):
    connection: ProviderConnection

    @property
    def runtime_generation(self) -> str: ...
    @property
    def session_inventory_complete(self) -> bool: ...

    async def capabilities(self) -> CapabilitySet: ...
    async def list_profiles(self) -> list[HermesProfile]: ...
    async def create_profile(
        self,
        *,
        name: str,
        display_name: str,
        description: str,
    ) -> HermesProfile: ...
    async def list_sessions(self) -> list[HermesSession]: ...
    async def search_sessions(
        self, query: str, *, limit: int = 20
    ) -> list[HermesSearchResult]: ...
    async def create_session(self, *, title: str | None = None) -> HermesSession: ...
    async def resume_session(self, stored_session_id: str) -> HermesSession: ...
    async def history(
        self, route: SessionRoute, *, expected_runtime_generation: str | None = None
    ) -> list[dict[str, Any]]: ...
    async def history_readonly(self, stored_session_id: str) -> list[dict[str, Any]]: ...
    async def submit_prompt(
        self,
        route: SessionRoute,
        prompt: str,
        *,
        operation_id: str,
        expected_runtime_generation: str | None = None,
    ) -> PromptReceipt: ...
    async def interrupt(
        self, route: SessionRoute, *, expected_runtime_generation: str | None = None
    ) -> None: ...
    async def respond_approval(
        self,
        route: SessionRoute,
        request_id: str,
        choice: str,
        *,
        expected_runtime_generation: str | None = None,
    ) -> dict[str, Any]: ...
    async def respond_clarification(
        self,
        route: SessionRoute,
        request_id: str,
        answer: str | list[str],
        *,
        question_id: str | None = None,
        expected_runtime_generation: str | None = None,
    ) -> dict[str, Any]: ...
    async def delete_session(self, route: SessionRoute) -> None: ...
    async def list_automations(self) -> list[HermesAutomation]: ...
    async def create_automation(self, automation: HermesAutomation) -> HermesAutomation: ...
    async def update_automation(
        self, automation_id: str, changes: dict[str, Any]
    ) -> HermesAutomation: ...
    async def delete_automation(self, automation_id: str) -> None: ...
    async def trigger_automation(self, automation_id: str) -> HermesRunReceipt: ...
    async def list_automation_runs(
        self, automation_id: str, *, limit: int = 100
    ) -> list[HermesRunReceipt]: ...
    async def list_models(self) -> AdminResourceSnapshot: ...
    async def set_model(
        self,
        provider: str,
        model: str,
        *,
        confirm_expensive_model: bool = False,
    ) -> AdminResourceSnapshot: ...
    async def get_config(self) -> AdminResourceSnapshot: ...
    async def update_config(self, config: dict[str, Any]) -> AdminResourceSnapshot: ...
    async def get_soul(self) -> AdminResourceSnapshot: ...
    async def update_soul(self, content: str) -> AdminResourceSnapshot: ...
    async def get_memory(self) -> AdminResourceSnapshot: ...
    async def set_memory_provider(self, name: str) -> AdminResourceSnapshot: ...
    async def reset_memory(self, target: str) -> AdminResourceSnapshot: ...
    async def list_skills(self) -> AdminResourceSnapshot: ...
    async def toggle_skill(self, name: str, enabled: bool) -> AdminResourceSnapshot: ...
    async def list_toolsets(self) -> AdminResourceSnapshot: ...
    async def toggle_toolset(self, name: str, enabled: bool) -> AdminResourceSnapshot: ...
    async def list_mcp_servers(self) -> AdminResourceSnapshot: ...
    async def create_mcp_server(self, server: dict[str, Any]) -> AdminResourceSnapshot: ...
    async def delete_mcp_server(self, name: str) -> AdminResourceSnapshot: ...
    async def toggle_mcp_server(self, name: str, enabled: bool) -> AdminResourceSnapshot: ...
    async def test_mcp_server(self, name: str) -> AdminResourceSnapshot: ...
    async def list_channels(self) -> AdminResourceSnapshot: ...
    async def update_channel(self, name: str, changes: dict[str, Any]) -> AdminResourceSnapshot: ...
    async def test_channel(self, name: str) -> AdminResourceSnapshot: ...
    async def get_usage(self, *, days: int = 30) -> AdminResourceSnapshot: ...
    async def list_secrets(self) -> AdminResourceSnapshot: ...
    async def set_secret(self, name: str, value: str) -> AdminResourceSnapshot: ...
    async def delete_secret(self, name: str) -> AdminResourceSnapshot: ...
    async def close(self) -> None: ...


class HermesGatewayProvider:
    """Adapter for Hermes dashboard JSON-RPC with 8642 capability fallback."""

    def __init__(self, connection: ProviderConnection, event_sink: EventSink | None = None) -> None:
        self.connection = connection
        self.event_sink = event_sink
        self.rpc = JsonRpcClient(
            url=connection.ws_url,
            gateway_id=connection.gateway_id,
            profile_name=connection.profile_name,
            token=connection.dashboard_token,
            connect_host=connection.ws_connect_host,
            event_callback=self._on_event,
        )
        headers = (
            {"X-Hermes-Session-Token": connection.dashboard_token}
            if connection.dashboard_token
            else {}
        )
        self.http = httpx.AsyncClient(
            base_url=connection.rest_url,
            headers=headers,
            timeout=15,
            follow_redirects=False,
            trust_env=False,
            transport=_pinned_http_transport(
                connection.rest_url,
                connection.rest_connect_host,
            ),
        )
        self.api = (
            httpx.AsyncClient(
                base_url=connection.api_url,
                headers={"Authorization": f"Bearer {connection.api_key}"},
                timeout=15,
                follow_redirects=False,
                trust_env=False,
                transport=_pinned_http_transport(
                    connection.api_url,
                    connection.api_connect_host,
                ),
            )
            if connection.api_url and connection.api_key
            else None
        )
        self._connect_lock = asyncio.Lock()
        self._ever_connected = False
        self._routes: OrderedDict[str, SessionRoute] = OrderedDict()
        self._route_generations: OrderedDict[str, str] = OrderedDict()
        self._cursors: OrderedDict[str, tuple[int, str | None]] = OrderedDict()
        self._max_remembered_routes = 2_048
        self._gateway_epoch: str | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._connection_state_interval = 15.0
        self._closed = False
        self._instance_epoch = uuid4().hex
        self._session_inventory_complete = False

    @property
    def runtime_generation(self) -> str:
        state = self.rpc.generation if self.rpc.connected else f"disconnected-{self.rpc.generation}"
        return f"{self._instance_epoch}:{state}"

    @property
    def session_inventory_complete(self) -> bool:
        return self._session_inventory_complete

    async def _ensure_connected(self) -> None:
        if self._closed:
            raise ConnectionError("Hermes provider is closed")
        if self.rpc.connected:
            self._ensure_supervisor()
            return
        async with self._connect_lock:
            if not self.rpc.connected:
                recovering = self._ever_connected
                await self.rpc.connect()
                self._ever_connected = True
                if recovering:
                    await self._recover_after_reconnect()
        self._ensure_supervisor()

    def _ensure_supervisor(self) -> None:
        if self._closed or (self._supervisor is not None and not self._supervisor.done()):
            return
        self._supervisor = asyncio.create_task(
            self._supervise_connection(),
            name=f"hermes-reconnect-{self.connection.gateway_id}-{self.connection.profile_name}",
        )

    async def _emit_connection_state(self, state: str, *, attempt: int = 0) -> bool:
        if self.event_sink is None:
            return True
        try:
            await self.event_sink(
                NormalizedEvent.create(
                    type="control.connection",
                    gateway_id=self.connection.gateway_id,
                    profile_name=self.connection.profile_name,
                    replay_epoch=self._gateway_epoch,
                    data={"state": state, "attempt": attempt},
                )
            )
            return True
        except asyncio.CancelledError:
            raise
        except Exception:
            # SQLite may be temporarily locked/unavailable while the Hermes
            # transport itself is recovering. Never include the exception or
            # connection values in logs: either can contain local paths or
            # credentials. The next state transition will retry persistence.
            _LOGGER.warning(
                "Hermes connection-state delivery failed; reconnect supervisor continues"
            )
            return False

    async def _supervise_connection(self) -> None:
        attempt = 0
        connected_state_delivered = False
        next_connected_report_at = 0.0
        try:
            while not self._closed:
                if self.rpc.connected:
                    attempt = 0
                    loop_now = asyncio.get_running_loop().time()
                    if (
                        not connected_state_delivered
                        or loop_now >= next_connected_report_at
                    ):
                        connected_state_delivered = await self._emit_connection_state(
                            "connected"
                        )
                        if connected_state_delivered:
                            # Mirror the audited Hermes heartbeat cadence. This
                            # keeps the gateway cache fresh without treating a
                            # one-time connect event as proof forever.
                            next_connected_report_at = (
                                loop_now + self._connection_state_interval
                            )
                    await asyncio.sleep(1)
                    continue
                connected_state_delivered = False
                next_connected_report_at = 0.0
                attempt += 1
                await self._emit_connection_state("reconnecting", attempt=attempt)
                try:
                    async with self._connect_lock:
                        if not self.rpc.connected and not self._closed:
                            await self.rpc.connect()
                            recovering = self._ever_connected
                            self._ever_connected = True
                            if recovering:
                                await self._recover_after_reconnect()
                    if self.rpc.connected:
                        attempt = 0
                        connected_state_delivered = await self._emit_connection_state(
                            "connected"
                        )
                        if connected_state_delivered:
                            next_connected_report_at = (
                                asyncio.get_running_loop().time()
                                + self._connection_state_interval
                            )
                        continue
                except Exception:
                    await self._emit_connection_state("offline", attempt=attempt)
                delay = min(0.8 * (2 ** min(attempt, 6)), 30.0) + random.uniform(0, 0.45)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise

    async def _on_event(self, event: NormalizedEvent) -> None:
        # A runtime id is only meaningful inside this exact provider
        # connection generation.  Persist the opaque generation alongside the
        # route so an eight-character id reused after restart cannot bind to a
        # stale Control session.
        event.runtime_generation = self.runtime_generation
        previous_gateway_epoch = self._gateway_epoch
        if (
            event.replay_epoch
            and previous_gateway_epoch
            and event.replay_epoch != previous_gateway_epoch
        ):
            stale_routes = list(self._routes.values())
            self._routes.clear()
            self._route_generations.clear()
            self._cursors.clear()
            if self.event_sink is not None:
                for stale_route in stale_routes:
                    await self.event_sink(
                        NormalizedEvent.create(
                            type="control.reconcile",
                            gateway_id=stale_route.gateway_id,
                            profile_name=stale_route.profile_name,
                            stored_session_id=stale_route.stored_session_id,
                            replay_epoch=event.replay_epoch,
                            runtime_generation=self.runtime_generation,
                            data={"reason": "epoch_changed", "historyRequired": True},
                        )
                    )
        if event.replay_epoch:
            self._gateway_epoch = event.replay_epoch
        elif self._gateway_epoch and event.sequence is not None:
            # Hermes session events carry seq but the gateway epoch is
            # announced separately. Attach it before publishing so every
            # downstream replay layer can reset correctly after a restart.
            event.replay_epoch = self._gateway_epoch
        route = None
        if event.runtime_session_id:
            if (
                self._route_generations.get(event.runtime_session_id)
                == self.runtime_generation
            ):
                route = self._routes.get(event.runtime_session_id)
        if route is None and event.stored_session_id:
            route = next(
                (item for item in self._routes.values() if item.stored_session_id == event.stored_session_id),
                None,
            )
        if route is not None and not event.stored_session_id:
            # Official dashboard events often expose only the ephemeral
            # session_id.  Enrich them from the route that Control itself
            # created/resumed so reconnect replay can bind by durable identity.
            event.stored_session_id = route.stored_session_id
        if route is not None and event.sequence is not None:
            cursor_key = route.runtime_session_id or route.stored_session_id
            self._cursors[cursor_key] = (
                event.sequence,
                event.replay_epoch or self._gateway_epoch,
            )
            self._cursors.move_to_end(cursor_key)
        if self.event_sink is not None:
            await self.event_sink(event)

    def _remember_route(
        self, route: SessionRoute, *, generation: str | None = None
    ) -> None:
        if route.runtime_session_id:
            stale_runtime_ids = [
                runtime_id
                for runtime_id, remembered in self._routes.items()
                if remembered.stored_session_id == route.stored_session_id
                and runtime_id != route.runtime_session_id
            ]
            for runtime_id in stale_runtime_ids:
                self._routes.pop(runtime_id, None)
                self._route_generations.pop(runtime_id, None)
                self._cursors.pop(runtime_id, None)
            self._routes[route.runtime_session_id] = route
            self._route_generations[route.runtime_session_id] = (
                generation or self.runtime_generation
            )
            self._routes.move_to_end(route.runtime_session_id)
            self._route_generations.move_to_end(route.runtime_session_id)
            while len(self._routes) > self._max_remembered_routes:
                old_runtime, _ = self._routes.popitem(last=False)
                self._route_generations.pop(old_runtime, None)
                self._cursors.pop(old_runtime, None)
            while len(self._cursors) > self._max_remembered_routes:
                self._cursors.popitem(last=False)

    async def _recover_after_reconnect(self) -> None:
        normalizer = EventNormalizer(
            gateway_id=self.connection.gateway_id,
            profile_name=self.connection.profile_name,
        )
        for route in list(self._routes.values()):
            if not route.runtime_session_id:
                continue
            last_seq, previous_epoch = self._cursors.get(route.runtime_session_id, (0, None))
            if not last_seq:
                self._routes.pop(route.runtime_session_id, None)
                self._route_generations.pop(route.runtime_session_id, None)
                self._cursors.pop(route.runtime_session_id, None)
                if self.event_sink is not None:
                    await self.event_sink(
                        NormalizedEvent.create(
                            type="control.reconcile",
                            gateway_id=route.gateway_id,
                            profile_name=route.profile_name,
                            stored_session_id=route.stored_session_id,
                            runtime_generation=self.runtime_generation,
                            data={
                                "reason": "connection_generation_changed",
                                "historyRequired": True,
                            },
                        )
                    )
                continue
            reconciliation_reason: str | None = None
            try:
                raw = await self.rpc.request(
                    "session.events.since",
                    {"session_id": route.runtime_session_id, "last_seen": last_seq},
                    timeout=15,
                )
                if not isinstance(raw, Mapping):
                    reconciliation_reason = "invalid_replay"
                else:
                    epoch = str(raw.get("epoch")) if raw.get("epoch") is not None else None
                    if bool(raw.get("truncated")):
                        reconciliation_reason = "buffer_truncated"
                    elif epoch and previous_epoch != epoch:
                        reconciliation_reason = "epoch_changed"
                    if epoch:
                        self._gateway_epoch = epoch
                    if reconciliation_reason != "epoch_changed":
                        self._route_generations[
                            route.runtime_session_id
                        ] = self.runtime_generation
                        for item in raw.get("events", []) if isinstance(raw.get("events"), list) else []:
                            if isinstance(item, Mapping):
                                await self._on_event(normalizer.normalize(item))
            except (JsonRpcError, JsonRpcDisconnected, OSError, TimeoutError):
                reconciliation_reason = "runtime_stale"
            if reconciliation_reason and self.event_sink is not None:
                runtime_is_valid = reconciliation_reason == "buffer_truncated"
                if not runtime_is_valid and route.runtime_session_id:
                    self._routes.pop(route.runtime_session_id, None)
                    self._route_generations.pop(route.runtime_session_id, None)
                    self._cursors.pop(route.runtime_session_id, None)
                await self.event_sink(
                    NormalizedEvent.create(
                        type="control.reconcile",
                        gateway_id=route.gateway_id,
                        profile_name=route.profile_name,
                        stored_session_id=route.stored_session_id,
                        runtime_session_id=(
                            route.runtime_session_id if runtime_is_valid else None
                        ),
                        runtime_generation=self.runtime_generation,
                        data={
                            "reason": reconciliation_reason,
                            "historyRequired": True,
                        },
                    )
                )

    async def _read(self, method: str, params: dict[str, Any] | None = None) -> Any:
        await self._ensure_connected()
        return await reconnecting_call(
            lambda: self.rpc.request(method, params), self._reconnect
        )

    async def _reconnect(self) -> None:
        await self.rpc.close()
        await self._ensure_connected()

    async def capabilities(self) -> CapabilitySet:
        methods: set[str] = set()
        version = reported_source_sha = None
        reported_source_sha_observed = False
        trusted_source_sha = self.connection.trusted_source_sha
        # A REST route can be cached or served by another process. Confirm the
        # profile-scoped realtime gateway is actually reachable before marking
        # the connection healthy.
        rpc_available = True
        try:
            await self._read("gateway.ping")
            methods.add("gateway.ping")
        except (ConnectionError, OSError, TimeoutError):
            rpc_available = False
            if self.api is None:
                raise
        try:
            body = await bounded_json_request(self.http, "GET", "/api/status")
            if not isinstance(body, Mapping):
                raise UpstreamPayloadError("Hermes status must be an object")
            version = body.get("version")
            reported_marker = object()
            reported_value = next(
                (
                    body[name]
                    for name in ("source_sha", "git_sha", "sha")
                    if name in body
                ),
                reported_marker,
            )
            reported_source_sha_observed = reported_value is not reported_marker
            reported_source_sha = (
                None if reported_value is reported_marker else reported_value
            )
            if (
                not isinstance(reported_source_sha, str)
                or not reported_source_sha.strip()
                or len(reported_source_sha) > 80
            ):
                reported_source_sha = None
            else:
                reported_source_sha = reported_source_sha.strip()
        except (httpx.HTTPError, ValueError):
            pass
        try:
            search_probe = await bounded_json_request(
                self.http,
                "GET",
                "/api/sessions/search",
                params={
                    "q": "__hermes_control_read_probe__",
                    "limit": 1,
                    "profile": self.connection.profile_name,
                },
            )
            _bounded_rows(
                search_probe,
                key="results",
                label="session search results",
                max_items=1,
            )
            methods.add("session.search")
        except (httpx.HTTPError, ValueError):
            # Search is an optional, read-only dashboard module. Its absence
            # must not disable chat or be inferred from a matching version.
            pass
        audited = _AUDITED_REVISIONS.get(str(trusted_source_sha or "").lower())
        reported_revision_consistent = (
            not reported_source_sha_observed
            or (
                reported_source_sha is not None
                and str(reported_source_sha).lower()
                == str(trusted_source_sha or "").lower()
            )
        )
        audited_contract = (
            audited is not None
            and str(version or "") == audited[0]
            and reported_revision_consistent
        )
        if rpc_available:
            for method in ("profiles.list", "session.list"):
                try:
                    await self._read(method)
                    methods.add(method)
                except JsonRpcError as exc:
                    # A non-method-not-found error can still demonstrate that
                    # a read method exists, but it is not a successful safety
                    # probe. In particular, never certify audited session or
                    # human-gate writes unless session.list actually returned.
                    if exc.code != -32601 and method != "session.list":
                        methods.add(method)
                except (ConnectionError, OSError, TimeoutError):
                    raise
            try:
                cron_jobs = await bounded_json_request(
                    self.http,
                    "GET",
                    "/api/cron/jobs",
                    params={"profile": self.connection.profile_name},
                )
                _bounded_rows(
                    cron_jobs,
                    key="jobs",
                    label="automations",
                    max_items=_MAX_AUTOMATIONS,
                )
                methods.add("cron.list")
            except (httpx.HTTPError, ValueError):
                pass
            for method, template in _ADMIN_READ_PROBES.items():
                if (
                    method == "memory.get"
                    and trusted_source_sha not in _PROFILE_AWARE_MEMORY_REVISIONS
                ):
                    continue
                path = template.format(
                    profile=quote(self.connection.profile_name, safe="")
                )
                params: dict[str, Any] = {
                    "profile": self.connection.profile_name
                }
                if method == "usage.get":
                    params["days"] = 30
                try:
                    resource = await bounded_json_request(
                        self.http,
                        "GET",
                        path,
                        params=params,
                    )
                    methods.add(method)
                except (httpx.HTTPError, ValueError):
                    # Administration modules are optional and independent. A
                    # broken/absent module must not disable chat or certify a
                    # related write capability.
                    continue
            # Mutations cannot be probed safely on Newton or Jarvis.  Enable
            # them only for an exact audited source revision *and* its matching
            # version after the related harmless list operation succeeds. A
            # same-version fork therefore remains read-only by default.
            if audited_contract and audited is not None:
                if "session.list" in methods:
                    methods.update(audited[1])
                if "profiles.list" in methods:
                    methods.add("profiles.create")
                # The official Hermes cron contract uses the profile's
                # configured timezone and falls back to the host's local zone
                # when that value is empty. Its dashboard and Telegram clients
                # can therefore create jobs without an explicit config value.
                # A successful bounded cron inventory is the harmless probe for
                # the audited CRUD routes; requiring a non-empty /api/config
                # timezone here would impose a Control-only restriction.
                if "cron.list" in methods:
                    methods.update(audited[2])
                for read_method, write_methods in _ADMIN_WRITES_BY_READ.items():
                    if read_method in methods:
                        methods.update(write_methods)
        if self.api is not None and (not rpc_available or version is None):
            try:
                body = await bounded_json_request(
                    self.api, "GET", self._api_path("/v1/capabilities")
                )
                if not isinstance(body, Mapping):
                    raise UpstreamPayloadError("Hermes capabilities must be an object")
                advertised_methods = body.get("methods")
                if advertised_methods is None:
                    advertised_methods = []
                if not isinstance(advertised_methods, list) or len(advertised_methods) > 256:
                    raise UpstreamPayloadError("Hermes capability methods are invalid")
                if any(not isinstance(item, str) or len(item) > 200 for item in advertised_methods):
                    raise UpstreamPayloadError("Hermes capability method is invalid")
                features = body.get("features") or {}
                endpoints = body.get("endpoints") or {}
                if not isinstance(features, Mapping) or len(features) > 256:
                    raise UpstreamPayloadError("Hermes capability features are invalid")
                if not isinstance(endpoints, Mapping) or len(endpoints) > 256:
                    raise UpstreamPayloadError("Hermes capability endpoints are invalid")
                if any(
                    not isinstance(name, str) or len(name) > 200
                    for name in (*features.keys(), *endpoints.keys())
                ):
                    raise UpstreamPayloadError("Hermes capability name is invalid")
                api_version = body.get("version") or version
                api_audited_contract = (
                    audited is not None
                    and str(api_version or "") == audited[0]
                    and reported_revision_consistent
                )
                methods.update(
                    set(advertised_methods)
                    & {"session.list", "session.history"}
                )
                if api_audited_contract:
                    methods.update(
                        set(advertised_methods)
                        & {
                            "session.create",
                            "session.resume",
                            "session.delete",
                            "prompt.submit",
                        }
                    )
                for name, (expected_method, expected_path, capability) in (
                    _OFFICIAL_API_SESSION_ENDPOINTS.items()
                ):
                    endpoint = endpoints.get(name)
                    if endpoint is None:
                        continue
                    if not isinstance(endpoint, Mapping) or len(endpoint) > 16:
                        raise UpstreamPayloadError("Hermes capability endpoint is invalid")
                    if (
                        endpoint.get("method") == expected_method
                        and endpoint.get("path") == expected_path
                        and (
                            capability in {"session.list", "session.history"}
                            or api_audited_contract
                        )
                    ):
                        methods.add(capability)
                if {
                    "session.list",
                    "session.history",
                }.issubset(methods) and api_audited_contract:
                    # Reattachment in the API fallback is a local route
                    # reconstruction backed by durable session history.
                    methods.add("session.resume")
                version = api_version
            except (httpx.HTTPError, ValueError):
                if not rpc_available:
                    raise
        return CapabilitySet(
            protocol="dashboard-jsonrpc" if rpc_available else "openai-compatible",
            version=version,
            # The operator-supplied SHA is a write-only trust anchor. It may
            # select an audited contract internally, but it must never be
            # reflected through capabilities or copied into public gateway
            # diagnostics. Only Hermes' independently reported value belongs
            # in this projection (and official 0.20.5/0.20.6 commonly omit it).
            source_sha=(
                str(reported_source_sha)
                if reported_source_sha is not None
                else None
            ),
            methods=frozenset(methods),
            features=frozenset(
                ({"streaming"} if "prompt.submit" in methods else set())
                | ({"profiles"} if "profiles.list" in methods else set())
                | ({"replay"} if "session.events.since" in methods else set())
                | ({"api-fallback"} if not rpc_available else set())
            ),
        )

    async def list_profiles(self) -> list[HermesProfile]:
        raw = await self._read("profiles.list")
        rows = _bounded_rows(
            raw, key="profiles", label="profiles", max_items=_MAX_PROFILES
        )
        return [
            HermesProfile(
                name=_bounded_text(item.get("name"), label="profile name", max_length=120),
                display_name=_bounded_text(
                    item.get("display_name") or item.get("name"),
                    label="profile display name",
                    max_length=120,
                ),
                status=str(item.get("status") or "unknown")[:30],
                model=str(item["model"])[:200] if item.get("model") is not None else None,
            )
            for item in rows
        ]

    async def create_profile(
        self,
        *,
        name: str,
        display_name: str,
        description: str,
    ) -> HermesProfile:
        """Create one official Hermes profile without exposing its host path.

        This mutation is deliberately sent once. A missing reply is ambiguous
        and must be reconciled by the caller through ``profiles.list`` rather
        than retried automatically.
        """

        await self._ensure_connected()
        soul = f"# {display_name}\n\nYou are {display_name}.\n\n{description.strip()}\n"
        try:
            raw = await self.rpc.request(
                "profiles.create",
                {
                    "name": name,
                    "description": description,
                    "soul": soul,
                    "mirror_credentials": True,
                    # Hermes' shared global auth fallback avoids forking
                    # renewable OAuth token state into another auth.json.
                    "share_auth": True,
                    "clone_all": False,
                    "no_skills": False,
                },
            )
        except (JsonRpcDisconnected, ConnectionError, OSError, TimeoutError) as exc:
            raise RuntimeError("MUTATION_DELIVERY_UNKNOWN") from exc
        if not isinstance(raw, Mapping):
            raise UpstreamPayloadError("Hermes profile response must be an object")
        returned_name = _bounded_text(
            raw.get("name"), label="profile name", max_length=64
        )
        if returned_name != name:
            raise UpstreamPayloadError("Hermes returned a different profile identity")
        mirrored = raw.get("mirrored")
        setup_ready = bool(raw.get("soul_written")) and isinstance(
            mirrored, Mapping
        ) and mirrored.get("auth") == "shared" and bool(
            raw.get("model_set") or mirrored.get("model_inherited")
        )
        # Hermes also returns filesystem and credential-mirroring diagnostics.
        # They are intentionally discarded at this trust boundary.
        return HermesProfile(
            name=returned_name,
            display_name=display_name,
            status="unknown" if setup_ready else "degraded",
        )

    async def list_sessions(self) -> list[HermesSession]:
        self._session_inventory_complete = False
        try:
            # Ask for one row beyond Control's bound. Fewer rows prove this is
            # a complete inventory; the sentinel row makes an oversized
            # inventory fail closed instead of treating a truncated window as
            # evidence that older Hermes sessions were deleted.
            raw = await self._read(
                "session.list", {"limit": _MAX_SESSIONS + 1}
            )
        except (ConnectionError, OSError, TimeoutError):
            if self.api is None:
                raise
            body = await bounded_json_request(
                self.api, "GET", self._api_path("/api/sessions")
            )
            raw = body.get("data", []) if isinstance(body, Mapping) else []
        rows = _bounded_rows(
            raw,
            key="sessions",
            label="sessions",
            max_items=_MAX_SESSIONS + 1,
        )
        if len(rows) > _MAX_SESSIONS:
            raise UpstreamPayloadError(
                "Hermes session inventory exceeds Control's safe sync bound"
            )
        # The 8642 fallback does not expose a proven total. Only the bounded
        # dashboard RPC request above is safe for negative reconciliation.
        self._session_inventory_complete = self.rpc.connected
        sessions = [self._session(dict(item)) for item in rows]
        for session in sessions:
            self._remember_route(
                SessionRoute(
                    self.connection.gateway_id,
                    self.connection.profile_name,
                    session.stored_session_id,
                    session.runtime_session_id,
                )
            )
        return sessions

    async def search_sessions(
        self, query: str, *, limit: int = 20
    ) -> list[HermesSearchResult]:
        normalized = query.strip()
        if not normalized:
            return []
        safe_limit = max(1, min(int(limit), _MAX_SEARCH_RESULTS))
        raw = await bounded_json_request(
            self.http,
            "GET",
            "/api/sessions/search",
            params={
                "q": normalized[:200],
                "limit": safe_limit,
                "profile": self.connection.profile_name,
            },
        )
        rows = _bounded_rows(
            raw,
            key="results",
            label="session search results",
            max_items=_MAX_SEARCH_RESULTS,
        )
        results: list[HermesSearchResult] = []
        for item in rows[:safe_limit]:
            stored_session_id = _bounded_text(
                item.get("session_id") or item.get("id"),
                label="search session id",
                max_length=512,
            )

            def optional_text(name: str, maximum: int) -> str | None:
                value = item.get(name)
                if value in (None, ""):
                    return None
                rendered = str(value)
                if len(rendered) > maximum:
                    raise UpstreamPayloadError(
                        f"Hermes search {name} is too long"
                    )
                return rendered

            results.append(
                HermesSearchResult(
                    stored_session_id=stored_session_id,
                    snippet=optional_text("snippet", 4_000) or "",
                    title=optional_text("title", 500),
                    role=optional_text("role", 80),
                    lineage_root=optional_text("lineage_root", 512),
                )
            )
        return results

    async def create_session(self, *, title: str | None = None) -> HermesSession:
        try:
            await self._ensure_connected()
        except (ConnectionError, OSError, TimeoutError):
            if self.api is None:
                raise
            try:
                body = await bounded_json_request(
                    self.api,
                    "POST",
                    self._api_path("/api/sessions"),
                    json={"title": title} if title else {},
                )
                if not isinstance(body, Mapping):
                    raise UpstreamPayloadError("Hermes session response must be an object")
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code < 500:
                    raise
                raise RuntimeError("MUTATION_DELIVERY_UNKNOWN") from exc
            except (httpx.TransportError, httpx.TimeoutException, UpstreamPayloadError) as exc:
                raise RuntimeError("MUTATION_DELIVERY_UNKNOWN") from exc
            session_body = body.get("session")
            if session_body is None:
                session_body = body
            if not isinstance(session_body, Mapping):
                raise RuntimeError("MUTATION_DELIVERY_UNKNOWN")
            stored_id = _bounded_text(
                session_body.get("id") or session_body.get("stored_session_id"),
                label="stored session id",
                max_length=512,
            )
            title = str(session_body.get("title") or "")[:500] or None
            return HermesSession(stored_id, f"api:{stored_id}", title, "idle")
        try:
            raw = await self.rpc.request("session.create", {"title": title} if title else {})
        except (JsonRpcDisconnected, ConnectionError, OSError, TimeoutError) as exc:
            raise RuntimeError("MUTATION_DELIVERY_UNKNOWN") from exc
        session = self._session(raw)
        self._remember_route(
            SessionRoute(self.connection.gateway_id, self.connection.profile_name, session.stored_session_id, session.runtime_session_id)
        )
        return session

    async def resume_session(self, stored_session_id: str) -> HermesSession:
        try:
            raw = await self._read(
                "session.resume",
                {
                    # Official 0.20.5/0.20.6 contract uses session_id for the
                    # persistent id. The explicit alias remains for tolerant builds.
                    "session_id": stored_session_id,
                    "stored_session_id": stored_session_id,
                },
            )
            session = self._session(raw)
            self._remember_route(
                SessionRoute(self.connection.gateway_id, self.connection.profile_name, session.stored_session_id, session.runtime_session_id)
            )
            await self._emit_resumed_interactions(raw, session)
            return session
        except (ConnectionError, OSError, TimeoutError):
            if self.api is None:
                raise
            body = await bounded_json_request(
                self.api, "GET", self._api_path("/api/sessions")
            )
            rows = _bounded_rows(
                body, key="data", label="sessions", max_items=_MAX_SESSIONS
            )
            match = next(
                (
                    item
                    for item in rows
                    if str(item.get("stored_session_id") or item.get("id")) == stored_session_id
                ),
                None,
            )
            if match is None:
                raise KeyError(stored_session_id)
            return HermesSession(
                stored_session_id,
                f"api:{stored_session_id}",
                match.get("title"),
                str(match.get("status") or "idle"),
            )

    async def _emit_resumed_interactions(
        self, raw: Any, session: HermesSession
    ) -> None:
        """Replay official pending gates returned by ``session.resume``.

        Hermes deliberately includes these snapshots so a reconnecting client
        can restore blocked UI. Feeding them through the normalizer applies the
        same redaction and bounded contract as live websocket events.
        """

        if self.event_sink is None or not isinstance(raw, Mapping):
            return
        normalizer = EventNormalizer(
            gateway_id=self.connection.gateway_id,
            profile_name=self.connection.profile_name,
        )
        for field, event_type in (
            ("pending_approval", "approval.request"),
            ("pending_clarify", "clarify.request"),
        ):
            payload = raw.get(field)
            if not isinstance(payload, Mapping):
                continue
            event = normalizer.normalize(
                {
                    "method": "event",
                    "params": {
                        "type": event_type,
                        "session_id": session.runtime_session_id,
                        "payload": dict(payload),
                    },
                }
            )
            event.stored_session_id = session.stored_session_id
            await self._on_event(event)

    async def history(
        self, route: SessionRoute, *, expected_runtime_generation: str | None = None
    ) -> list[dict[str, Any]]:
        if route.runtime_session_id and route.runtime_session_id.startswith("api:"):
            return await self._api_history(route.stored_session_id)
        try:
            await self._ensure_connected()
            if (
                expected_runtime_generation is not None
                and self.runtime_generation != expected_runtime_generation
            ):
                raise RuntimeGenerationChanged(
                    "Hermes reconnected before history dispatch"
                )
            route_generation = expected_runtime_generation or self.runtime_generation
            rpc_generation = self._rpc_generation_for(route_generation)
            self._remember_route(route, generation=route_generation)
            raw = await self.rpc.request(
                "session.history",
                {
                    "session_id": route.runtime_session_id,
                    "stored_session_id": route.stored_session_id,
                },
                expected_generation=rpc_generation,
            )
        except JsonRpcGenerationChanged as exc:
            raise RuntimeGenerationChanged(
                "Hermes reconnected before history dispatch"
            ) from exc
        except RuntimeGenerationChanged:
            raise
        except (JsonRpcDisconnected, ConnectionError, OSError, TimeoutError):
            if self.api is None:
                raise RuntimeGenerationChanged(
                    "Hermes disconnected during safe history read"
                )
            return await self._api_history(route.stored_session_id)
        return [
            dict(item)
            for item in _bounded_rows(
                raw, key="messages", label="messages", max_items=_MAX_MESSAGES
            )
        ]

    async def history_readonly(self, stored_session_id: str) -> list[dict[str, Any]]:
        """Read durable transcript pages without creating a Hermes runtime."""

        safe_id = quote(stored_session_id, safe="")
        messages: list[dict[str, Any]] = []
        offset = 0
        try:
            while len(messages) < _MAX_MESSAGES:
                page_limit = min(500, _MAX_MESSAGES - len(messages))
                body = await bounded_json_request(
                    self.http,
                    "GET",
                    f"/api/sessions/{safe_id}/messages",
                    params={
                        "profile": self.connection.profile_name,
                        "limit": page_limit,
                        "offset": offset,
                        "order": "oldest",
                    },
                )
                page = _bounded_rows(
                    body,
                    key="messages",
                    label="messages",
                    max_items=page_limit,
                )
                messages.extend(dict(item) for item in page)
                if len(page) < page_limit:
                    break
                offset += len(page)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise SessionHistoryNotFound(stored_session_id) from exc
            raise
        except (httpx.TransportError, httpx.TimeoutException):
            # The dedicated 8642 fallback belongs exclusively to control-dev.
            # Reusing it for Newton/Jarvis would cross profile identity.
            if self.connection.profile_name != "control-dev" or self.api is None:
                raise
            return await self._api_history(stored_session_id)
        return messages

    async def submit_prompt(
        self,
        route: SessionRoute,
        prompt: str,
        *,
        operation_id: str,
        expected_runtime_generation: str | None = None,
    ) -> PromptReceipt:
        # Deliberately no reconnecting_call: a lost response is ambiguous and the
        # same user message must never be submitted twice automatically.
        if route.runtime_session_id and route.runtime_session_id.startswith("api:"):
            return await self._api_prompt(route, prompt, operation_id)
        try:
            await self._ensure_connected()
        except (ConnectionError, OSError, TimeoutError):
            if self.api is None:
                raise
            return await self._api_prompt(route, prompt, operation_id)
        if (
            expected_runtime_generation is not None
            and self.runtime_generation != expected_runtime_generation
        ):
            raise RuntimeGenerationChanged(
                "Hermes reconnected before prompt dispatch"
            )
        route_generation = expected_runtime_generation or self.runtime_generation
        rpc_generation = self._rpc_generation_for(route_generation)
        self._remember_route(route, generation=route_generation)
        try:
            raw = await self.rpc.request(
                "prompt.submit",
                {
                    "session_id": route.runtime_session_id,
                    "stored_session_id": route.stored_session_id,
                    "text": prompt,
                    "prompt": prompt,
                    "request_id": operation_id,
                },
                expected_generation=rpc_generation,
            )
        except JsonRpcGenerationChanged as exc:
            raise RuntimeGenerationChanged(
                "Hermes reconnected before prompt dispatch"
            ) from exc
        except (JsonRpcDisconnected, ConnectionError, OSError, TimeoutError) as exc:
            # Once rpc.request starts, both a failed send and a missing reply
            # are ambiguous: Hermes may already have accepted the prompt.
            # Never downgrade this to a normal failure or retry it.
            raise RuntimeError("PROMPT_DELIVERY_UNKNOWN") from exc
        status = raw.get("status", "streaming") if isinstance(raw, dict) else "streaming"
        return PromptReceipt(operation_id=operation_id, status=status)

    async def interrupt(
        self, route: SessionRoute, *, expected_runtime_generation: str | None = None
    ) -> None:
        if route.runtime_session_id and route.runtime_session_id.startswith("api:"):
            raise RuntimeError("Interrupt is unavailable through the 8642 fallback")
        await self._ensure_connected()
        if (
            expected_runtime_generation is not None
            and self.runtime_generation != expected_runtime_generation
        ):
            raise RuntimeGenerationChanged(
                "Hermes reconnected before interrupt dispatch"
            )
        route_generation = expected_runtime_generation or self.runtime_generation
        rpc_generation = self._rpc_generation_for(route_generation)
        self._remember_route(route, generation=route_generation)
        try:
            await self.rpc.request(
                "session.interrupt",
                {"session_id": route.runtime_session_id, "stored_session_id": route.stored_session_id},
                expected_generation=rpc_generation,
            )
        except JsonRpcGenerationChanged as exc:
            raise RuntimeGenerationChanged(
                "Hermes reconnected before interrupt dispatch"
            ) from exc
        except (JsonRpcDisconnected, ConnectionError, OSError, TimeoutError) as exc:
            raise RuntimeError("INTERRUPT_DELIVERY_UNKNOWN") from exc

    async def respond_approval(
        self,
        route: SessionRoute,
        request_id: str,
        choice: str,
        *,
        expected_runtime_generation: str | None = None,
    ) -> dict[str, Any]:
        if choice not in {"once", "session", "always", "deny"}:
            raise ValueError("Unsupported Hermes approval choice")
        if not request_id or len(request_id) > 200:
            raise ValueError("Invalid Hermes approval request id")
        raw = await self._interaction_request(
            route,
            "approval.respond",
            {
                "session_id": route.runtime_session_id,
                "request_id": request_id,
                "choice": choice,
            },
            expected_runtime_generation=expected_runtime_generation,
        )
        resolved = raw.get("resolved") if isinstance(raw, Mapping) else None
        if isinstance(resolved, bool) or not isinstance(resolved, int) or not 0 <= resolved <= 1_000:
            raise UpstreamPayloadError("Hermes approval response is invalid")
        return {"resolved": resolved}

    async def respond_clarification(
        self,
        route: SessionRoute,
        request_id: str,
        answer: str | list[str],
        *,
        question_id: str | None = None,
        expected_runtime_generation: str | None = None,
    ) -> dict[str, Any]:
        if not request_id or len(request_id) > 200:
            raise ValueError("Invalid Hermes clarification request id")
        params: dict[str, Any] = {
            "request_id": request_id,
            "answer": answer,
        }
        if question_id is not None:
            params["question_id"] = question_id
        raw = await self._interaction_request(
            route,
            "clarify.respond",
            params,
            expected_runtime_generation=expected_runtime_generation,
        )
        if not isinstance(raw, Mapping) or raw.get("status") not in {"ok", "expired"}:
            raise UpstreamPayloadError("Hermes clarification response is invalid")
        remaining = raw.get("remaining", [])
        if not isinstance(remaining, list) or len(remaining) > 50 or any(
            not isinstance(value, str) or not 0 < len(value) <= 100
            for value in remaining
        ):
            raise UpstreamPayloadError("Hermes clarification remaining list is invalid")
        return {"status": str(raw["status"]), "remaining": list(remaining)}

    async def _interaction_request(
        self,
        route: SessionRoute,
        method: str,
        params: dict[str, Any],
        *,
        expected_runtime_generation: str | None,
    ) -> Any:
        if route.runtime_session_id and route.runtime_session_id.startswith("api:"):
            raise RuntimeError("Human gates are unavailable through the 8642 fallback")
        await self._ensure_connected()
        if (
            expected_runtime_generation is not None
            and self.runtime_generation != expected_runtime_generation
        ):
            raise RuntimeGenerationChanged(
                "Hermes reconnected before human-gate response dispatch"
            )
        route_generation = expected_runtime_generation or self.runtime_generation
        rpc_generation = self._rpc_generation_for(route_generation)
        self._remember_route(route, generation=route_generation)
        try:
            return await self.rpc.request(
                method, params, expected_generation=rpc_generation
            )
        except JsonRpcGenerationChanged as exc:
            raise RuntimeGenerationChanged(
                "Hermes reconnected before human-gate response dispatch"
            ) from exc
        except (JsonRpcDisconnected, ConnectionError, OSError, TimeoutError) as exc:
            # Like prompts, a missing reply after a write is ambiguous and is
            # never retried or failed over to another identity.
            raise RuntimeError("MUTATION_DELIVERY_UNKNOWN") from exc

    def _rpc_generation_for(self, runtime_generation: str) -> int:
        prefix = f"{self._instance_epoch}:"
        if not runtime_generation.startswith(prefix):
            raise RuntimeGenerationChanged("Runtime generation belongs to another provider")
        raw_generation = runtime_generation[len(prefix):]
        if not raw_generation.isdigit():
            raise RuntimeGenerationChanged("Runtime generation is not connected")
        return int(raw_generation)

    async def delete_session(self, route: SessionRoute) -> None:
        try:
            await bounded_empty_request(
                self.http,
                "DELETE",
                f"/api/sessions/{quote(route.stored_session_id, safe='')}",
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            raise RuntimeError("MUTATION_DELIVERY_UNKNOWN") from exc
        except (httpx.TransportError, httpx.TimeoutException, UpstreamPayloadError) as exc:
            raise RuntimeError("MUTATION_DELIVERY_UNKNOWN") from exc
        stale_runtime_ids = [
            runtime_id
            for runtime_id, remembered in self._routes.items()
            if remembered.stored_session_id == route.stored_session_id
        ]
        for runtime_id in stale_runtime_ids:
            self._routes.pop(runtime_id, None)
            self._route_generations.pop(runtime_id, None)
            self._cursors.pop(runtime_id, None)

    async def list_automations(self) -> list[HermesAutomation]:
        timezone_name = await self._configured_timezone()
        raw = await bounded_json_request(
            self.http,
            "GET",
            "/api/cron/jobs",
            params={"profile": self.connection.profile_name},
        )
        rows = _bounded_rows(
            raw, key="jobs", label="automations", max_items=_MAX_AUTOMATIONS
        )
        return [
            self._automation(
                dict(row), timezone_name=timezone_name or "Hermes local"
            )
            for row in rows
        ]

    async def _configured_timezone(self) -> str | None:
        raw = await bounded_json_request(
            self.http,
            "GET",
            "/api/config",
            params={"profile": self.connection.profile_name},
        )
        if not isinstance(raw, Mapping):
            raise UpstreamPayloadError("Hermes profile config must be an object")
        timezone_value = raw.get("timezone")
        if not isinstance(timezone_value, str) or not timezone_value.strip():
            return None
        return _bounded_text(
            timezone_value, label="profile timezone", max_length=100
        )

    async def _assert_automation_timezone(self, requested: str) -> str:
        configured = await self._configured_timezone()
        if configured is None:
            if requested != "Hermes local":
                raise ValueError(
                    "Hermes uses its local timezone for cron; select Hermes local"
                )
            return requested
        if requested not in {configured, "Hermes local"}:
            raise ValueError(
                f"Hermes profile timezone is {configured}; per-job timezone {requested} is unsupported"
            )
        return configured

    async def _cron_mutation(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | httpx.Timeout | None = None,
    ) -> Mapping[str, Any]:
        request_args: dict[str, Any] = {
            "params": {"profile": self.connection.profile_name}
        }
        if json is not None:
            request_args["json"] = json
        if timeout is not None:
            request_args["timeout"] = timeout
        try:
            body = await bounded_json_request(
                self.http,
                method,
                path,
                **request_args,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            raise RuntimeError("MUTATION_DELIVERY_UNKNOWN") from exc
        except (httpx.TransportError, httpx.TimeoutException, UpstreamPayloadError) as exc:
            raise RuntimeError("MUTATION_DELIVERY_UNKNOWN") from exc
        if not isinstance(body, Mapping):
            raise UpstreamPayloadError("Hermes cron mutation must return an object")
        return body

    async def create_automation(self, automation: HermesAutomation) -> HermesAutomation:
        timezone_name = await self._assert_automation_timezone(automation.timezone)
        requested = {
            "name": automation.name,
            "schedule": automation.schedule,
            "prompt": automation.prompt,
            "deliver": "local",
        }
        if automation.enabled:
            raw = await self._cron_mutation("POST", "/api/cron/jobs", json=requested)
            return self._automation(dict(raw), timezone_name=timezone_name)

        # Hermes creates jobs enabled and has no atomic create-paused field.
        # Stage a harmless far-future one-shot, pause it, then install the
        # requested schedule. Any ambiguous partial failure therefore leaves
        # an inert job that the next authoritative sync can surface.
        staged_schedule = (
            datetime.now(timezone.utc) + timedelta(days=3650)
        ).isoformat()
        staged = await self._cron_mutation(
            "POST",
            "/api/cron/jobs",
            json={**requested, "schedule": staged_schedule},
        )
        staged_id = _bounded_text(
            staged.get("id") or staged.get("job_id"),
            label="automation id",
            max_length=512,
        )
        await self._cron_mutation(
            "POST", f"/api/cron/jobs/{quote(staged_id, safe='')}/pause"
        )
        raw = await self._cron_mutation(
            "PUT",
            f"/api/cron/jobs/{quote(staged_id, safe='')}",
            json={"updates": {"schedule": automation.schedule}},
        )
        return self._automation(dict(raw), timezone_name=timezone_name)

    async def update_automation(
        self, automation_id: str, changes: dict[str, Any]
    ) -> HermesAutomation:
        timezone_name = await self._configured_timezone() or "Hermes local"
        requested_timezone = str(changes.get("timezone") or timezone_name)
        if requested_timezone not in {timezone_name, "Hermes local"}:
            raise ValueError(
                f"Hermes profile timezone is {timezone_name}; per-job timezone "
                f"{requested_timezone} is unsupported"
            )
        path = f"/api/cron/jobs/{quote(automation_id, safe='')}"
        enabled = changes.get("enabled")
        updates = {
            key: value
            for key, value in changes.items()
            if key in {"name", "schedule", "prompt"}
        }
        raw: Mapping[str, Any] | None = None
        if enabled is False:
            raw = await self._cron_mutation("POST", f"{path}/pause")
        if updates:
            raw = await self._cron_mutation(
                "PUT", path, json={"updates": updates}
            )
        if enabled is True:
            raw = await self._cron_mutation("POST", f"{path}/resume")
        if raw is None:
            raw = await bounded_json_request(
                self.http,
                "GET",
                path,
                params={"profile": self.connection.profile_name},
            )
            if not isinstance(raw, Mapping):
                raise UpstreamPayloadError("Hermes cron job must be an object")
        return self._automation(dict(raw), timezone_name=timezone_name)

    async def delete_automation(self, automation_id: str) -> None:
        await self._cron_mutation(
            "DELETE", f"/api/cron/jobs/{quote(automation_id, safe='')}"
        )

    async def trigger_automation(self, automation_id: str) -> HermesRunReceipt:
        previous = {
            item.run_id
            for item in await self.list_automation_runs(automation_id, limit=100)
            if item.run_id
        }
        body = await self._cron_mutation(
            "POST",
            f"/api/cron/jobs/{quote(automation_id, safe='')}/trigger",
            # The official dashboard permits manual cron runs up to 24 hours.
            # Control dispatches this call in a background task, so the browser
            # still receives a 202 immediately while the loopback request stays
            # attached long enough to obtain the authoritative run session.
            timeout=httpx.Timeout(90_000.0, connect=15.0),
        )
        discovered: HermesRunReceipt | None = None
        for delay in (0.0, 0.1, 0.3):
            if delay:
                await asyncio.sleep(delay)
            current = await self.list_automation_runs(automation_id, limit=100)
            discovered = next(
                (item for item in current if item.run_id not in previous), None
            )
            if discovered is not None:
                break
        status = str(body.get("last_status") or body.get("state") or "completed")[:30]
        if status in {"success", "ok", "complete"}:
            status = "completed"
        elif status in {"error", "errored"}:
            status = "failed"
        if discovered is not None:
            return HermesRunReceipt(
                run_id=discovered.run_id,
                status=status or discovered.status,
                stored_session_id=discovered.stored_session_id,
                runtime_session_id=discovered.runtime_session_id,
                started_at=discovered.started_at,
                finished_at=discovered.finished_at,
            )
        return HermesRunReceipt(run_id=None, status=status)

    async def list_automation_runs(
        self, automation_id: str, *, limit: int = 100
    ) -> list[HermesRunReceipt]:
        raw = await bounded_json_request(
            self.http,
            "GET",
            f"/api/cron/jobs/{quote(automation_id, safe='')}/runs",
            params={
                "profile": self.connection.profile_name,
                "limit": max(1, min(limit, 100)),
            },
        )
        rows = _bounded_rows(raw, key="runs", label="automation runs", max_items=100)
        receipts: list[HermesRunReceipt] = []
        for row in rows:
            run_id = _bounded_text(
                row.get("id") or row.get("session_id"),
                label="automation run id",
                max_length=512,
            )
            if row.get("ended_at") is not None:
                status = "completed"
            elif bool(row.get("is_active")):
                status = "running"
            else:
                status = str(row.get("status") or "completed")[:30]
            receipts.append(
                HermesRunReceipt(
                    run_id=run_id,
                    status=status,
                    stored_session_id=run_id,
                    started_at=self._automation_datetime(
                        row.get("started_at") or row.get("created_at")
                    ),
                    finished_at=self._automation_datetime(row.get("ended_at")),
                )
            )
        return receipts

    async def _admin_read_snapshot(
        self,
        resource: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> AdminResourceSnapshot:
        query = {"profile": self.connection.profile_name, **(params or {})}
        raw = await bounded_json_request(self.http, "GET", path, params=query)
        return admin_snapshot(resource, raw)  # type: ignore[arg-type]

    async def _admin_mutation_snapshot(
        self,
        resource: str,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> AdminResourceSnapshot:
        query = {"profile": self.connection.profile_name, **(params or {})}
        try:
            raw = await bounded_json_request(
                self.http,
                method,
                path,
                params=query,
                json=payload,
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            raise RuntimeError("MUTATION_DELIVERY_UNKNOWN") from exc
        except (httpx.TransportError, httpx.TimeoutException, UpstreamPayloadError) as exc:
            raise RuntimeError("MUTATION_DELIVERY_UNKNOWN") from exc
        return admin_snapshot(resource, raw)  # type: ignore[arg-type]

    async def list_models(self) -> AdminResourceSnapshot:
        raw = await bounded_json_request(
            self.http,
            "GET",
            "/api/model/options",
            params={"profile": self.connection.profile_name},
        )
        if not isinstance(raw, Mapping):
            raise UpstreamPayloadError("Hermes model options must be an object")
        providers = _bounded_rows(
            raw.get("providers", []),
            key="providers",
            label="model providers",
            max_items=256,
        )
        normalized_providers = []
        for item in providers:
            provider_id = _bounded_text(
                item.get("slug") or item.get("id"),
                label="model provider id",
                max_length=120,
            )
            normalized_providers.append(
                {
                    **dict(item),
                    "id": provider_id,
                    "label": str(item.get("name") or item.get("label") or provider_id)[:200],
                }
            )
        return admin_snapshot(
            "models",
            {
                "current": {
                    "provider": raw.get("provider"),
                    "model": raw.get("model"),
                },
                "providers": normalized_providers,
            },
        )

    async def set_model(
        self,
        provider: str,
        model: str,
        *,
        confirm_expensive_model: bool = False,
    ) -> AdminResourceSnapshot:
        provider = _bounded_text(provider, label="model provider", max_length=120)
        model = _bounded_text(model, label="model name", max_length=300)
        return await self._admin_mutation_snapshot(
            "models",
            "POST",
            "/api/model/set",
            payload={
                "scope": "main",
                "provider": provider,
                "model": model,
                "confirm_expensive_model": confirm_expensive_model,
                "profile": self.connection.profile_name,
            },
        )

    async def get_config(self) -> AdminResourceSnapshot:
        raw = await bounded_json_request(
            self.http,
            "GET",
            "/api/config",
            params={"profile": self.connection.profile_name},
        )
        sanitized = sanitize_admin_payload(raw)
        if not isinstance(sanitized, Mapping):
            raise UpstreamPayloadError("Hermes config must be an object")
        return AdminResourceSnapshot(
            resource="config",
            data=writable_config_projection(sanitized),
        )

    async def update_config(self, config: dict[str, Any]) -> AdminResourceSnapshot:
        if contains_secret_fields(config):
            raise ValueError(
                "Secret-shaped config values must use the write-only secrets endpoint"
            )
        return await self._admin_mutation_snapshot(
            "config",
            "PUT",
            "/api/config",
            payload={"config": config, "profile": self.connection.profile_name},
        )

    async def get_soul(self) -> AdminResourceSnapshot:
        profile = quote(self.connection.profile_name, safe="")
        return await self._admin_read_snapshot(
            "soul", f"/api/profiles/{profile}/soul"
        )

    async def update_soul(self, content: str) -> AdminResourceSnapshot:
        if len(content.encode("utf-8")) > 256 * 1024:
            raise ValueError("SOUL content is too large")
        profile = quote(self.connection.profile_name, safe="")
        return await self._admin_mutation_snapshot(
            "soul",
            "PUT",
            f"/api/profiles/{profile}/soul",
            payload={"content": content},
        )

    async def get_memory(self) -> AdminResourceSnapshot:
        return await self._admin_read_snapshot("memory", "/api/memory")

    async def set_memory_provider(self, name: str) -> AdminResourceSnapshot:
        name = str(name).strip()
        if len(name) > 120:
            raise ValueError("Memory provider name is too long")
        return await self._admin_mutation_snapshot(
            "memory",
            "PUT",
            "/api/memory/provider",
            payload={"provider": name, "profile": self.connection.profile_name},
        )

    async def reset_memory(self, target: str) -> AdminResourceSnapshot:
        if target not in {"all", "memory", "user"}:
            raise ValueError("Memory reset target must be all, memory, or user")
        return await self._admin_mutation_snapshot(
            "memory",
            "POST",
            "/api/memory/reset",
            payload={"target": target, "profile": self.connection.profile_name},
        )

    async def list_skills(self) -> AdminResourceSnapshot:
        return await self._admin_read_snapshot("skills", "/api/skills")

    async def toggle_skill(self, name: str, enabled: bool) -> AdminResourceSnapshot:
        name = _bounded_text(name, label="skill name", max_length=200)
        return await self._admin_mutation_snapshot(
            "skills",
            "PUT",
            "/api/skills/toggle",
            payload={
                "name": name,
                "enabled": enabled,
                "profile": self.connection.profile_name,
            },
        )

    async def list_toolsets(self) -> AdminResourceSnapshot:
        return await self._admin_read_snapshot("toolsets", "/api/tools/toolsets")

    async def toggle_toolset(self, name: str, enabled: bool) -> AdminResourceSnapshot:
        name = _bounded_text(name, label="toolset name", max_length=200)
        return await self._admin_mutation_snapshot(
            "toolsets",
            "PUT",
            f"/api/tools/toolsets/{quote(name, safe='')}",
            payload={"enabled": enabled, "profile": self.connection.profile_name},
        )

    async def list_mcp_servers(self) -> AdminResourceSnapshot:
        return await self._admin_read_snapshot("mcp", "/api/mcp/servers")

    async def create_mcp_server(self, server: dict[str, Any]) -> AdminResourceSnapshot:
        name = _bounded_text(server.get("name"), label="MCP server name", max_length=120)
        payload = {**server, "name": name, "profile": self.connection.profile_name}
        return await self._admin_mutation_snapshot(
            "mcp", "POST", "/api/mcp/servers", payload=payload
        )

    async def delete_mcp_server(self, name: str) -> AdminResourceSnapshot:
        name = _bounded_text(name, label="MCP server name", max_length=120)
        return await self._admin_mutation_snapshot(
            "mcp", "DELETE", f"/api/mcp/servers/{quote(name, safe='')}"
        )

    async def toggle_mcp_server(
        self, name: str, enabled: bool
    ) -> AdminResourceSnapshot:
        name = _bounded_text(name, label="MCP server name", max_length=120)
        return await self._admin_mutation_snapshot(
            "mcp",
            "PUT",
            f"/api/mcp/servers/{quote(name, safe='')}/enabled",
            payload={"enabled": enabled, "profile": self.connection.profile_name},
        )

    async def test_mcp_server(self, name: str) -> AdminResourceSnapshot:
        name = _bounded_text(name, label="MCP server name", max_length=120)
        return await self._admin_mutation_snapshot(
            "mcp", "POST", f"/api/mcp/servers/{quote(name, safe='')}/test"
        )

    async def list_channels(self) -> AdminResourceSnapshot:
        return await self._admin_read_snapshot(
            "channels", "/api/messaging/platforms"
        )

    async def update_channel(
        self, name: str, changes: dict[str, Any]
    ) -> AdminResourceSnapshot:
        name = _bounded_text(name, label="channel name", max_length=120)
        return await self._admin_mutation_snapshot(
            "channels",
            "PUT",
            f"/api/messaging/platforms/{quote(name, safe='')}",
            payload={**changes, "profile": self.connection.profile_name},
        )

    async def test_channel(self, name: str) -> AdminResourceSnapshot:
        name = _bounded_text(name, label="channel name", max_length=120)
        return await self._admin_mutation_snapshot(
            "channels",
            "POST",
            f"/api/messaging/platforms/{quote(name, safe='')}/test",
        )

    async def get_usage(self, *, days: int = 30) -> AdminResourceSnapshot:
        if not 1 <= days <= 365:
            raise ValueError("Usage range must be between 1 and 365 days")
        return await self._admin_read_snapshot(
            "usage", "/api/analytics/usage", params={"days": days}
        )

    async def list_secrets(self) -> AdminResourceSnapshot:
        raw = await bounded_json_request(
            self.http,
            "GET",
            "/api/env",
            params={"profile": self.connection.profile_name},
        )
        if not isinstance(raw, Mapping) or len(raw) > _MAX_ADMIN_ROWS:
            raise UpstreamPayloadError("Hermes secret inventory is invalid")
        items: list[dict[str, Any]] = []
        for raw_name, raw_info in sorted(raw.items(), key=lambda item: str(item[0])):
            name = _bounded_text(raw_name, label="secret name", max_length=200)
            if not isinstance(raw_info, Mapping):
                raise UpstreamPayloadError("Hermes secret metadata is invalid")
            items.append(
                {
                    "name": name,
                    "configured": bool(raw_info.get("is_set")),
                    "description": str(raw_info.get("description") or "")[:2_000],
                    "category": str(raw_info.get("category") or "")[:120],
                    "advanced": bool(raw_info.get("advanced")),
                    "channelManaged": bool(raw_info.get("channel_managed")),
                }
            )
        return admin_snapshot("secrets", items)

    async def set_secret(self, name: str, value: str) -> AdminResourceSnapshot:
        name = _bounded_text(name, label="secret name", max_length=200)
        if not value or len(value) > 16_384:
            raise ValueError("Secret value is missing or too large")
        await self._admin_mutation_snapshot(
            "secrets",
            "PUT",
            "/api/env",
            payload={
                "key": name,
                "value": value,
                "profile": self.connection.profile_name,
            },
        )
        # Never trust an upstream write response to omit the submitted value.
        return admin_snapshot(
            "secrets", {"name": name, "configured": True, "status": "applied"}
        )

    async def delete_secret(self, name: str) -> AdminResourceSnapshot:
        name = _bounded_text(name, label="secret name", max_length=200)
        await self._admin_mutation_snapshot(
            "secrets",
            "DELETE",
            "/api/env",
            payload={"key": name, "profile": self.connection.profile_name},
        )
        return admin_snapshot(
            "secrets", {"name": name, "configured": False, "status": "applied"}
        )

    async def replay_since(self, route: SessionRoute, last_seen: int) -> dict[str, Any]:
        return await self._read(
            "session.events.since",
            {"session_id": route.runtime_session_id, "last_seen": last_seen},
        )

    async def close(self) -> None:
        self._closed = True
        if self._supervisor is not None:
            self._supervisor.cancel()
            try:
                await self._supervisor
            except asyncio.CancelledError:
                pass
            self._supervisor = None
        await self.rpc.close()
        await self.http.aclose()
        if self.api is not None:
            await self.api.aclose()

    def _api_path(self, path: str) -> str:
        # Initial deployment runs a dedicated 8642 process for control-dev;
        # multiplexed /p/<profile> routes are intentionally not used.
        return path

    async def _api_history(self, stored_session_id: str) -> list[dict[str, Any]]:
        if self.api is None:
            raise JsonRpcDisconnected("Hermes API fallback is not configured")
        try:
            body = await bounded_json_request(
                self.api,
                "GET",
                self._api_path(f"/api/sessions/{quote(stored_session_id, safe='')}/messages"),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise SessionHistoryNotFound(stored_session_id) from exc
            raise
        return [
            dict(item)
            for item in _bounded_rows(
                body, key="data", label="messages", max_items=_MAX_MESSAGES
            )
        ]

    async def _api_prompt(
        self, route: SessionRoute, prompt: str, operation_id: str
    ) -> PromptReceipt:
        if self.api is None:
            raise JsonRpcDisconnected("Hermes API fallback is not configured")
        # No transport retry: a disconnect after POST delivery is ambiguous.
        try:
            body = await bounded_json_request(
                self.api,
                "POST",
                self._api_path(
                    f"/api/sessions/{quote(route.stored_session_id, safe='')}/chat"
                ),
                json={"message": prompt, "request_id": operation_id},
            )
            if not isinstance(body, Mapping):
                raise UpstreamPayloadError("Hermes prompt response must be an object")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                raise
            raise RuntimeError("PROMPT_DELIVERY_UNKNOWN") from exc
        except (httpx.TransportError, httpx.TimeoutException, UpstreamPayloadError) as exc:
            raise RuntimeError("PROMPT_DELIVERY_UNKNOWN") from exc
        if self.event_sink is not None:
            message = body.get("message") if isinstance(body.get("message"), dict) else {}
            content = str(message.get("content") or "")
            try:
                await self.event_sink(
                    NormalizedEvent.create(
                        type="message.delta",
                        gateway_id=self.connection.gateway_id,
                        profile_name=self.connection.profile_name,
                        stored_session_id=route.stored_session_id,
                        runtime_session_id=route.runtime_session_id,
                        correlation_id=operation_id,
                        runtime_generation=self.runtime_generation,
                        data={"delta": content, "fallback": "api-server"},
                    )
                )
                await self.event_sink(
                    NormalizedEvent.create(
                        type="message.complete",
                        gateway_id=self.connection.gateway_id,
                        profile_name=self.connection.profile_name,
                        stored_session_id=route.stored_session_id,
                        runtime_session_id=route.runtime_session_id,
                        correlation_id=operation_id,
                        runtime_generation=self.runtime_generation,
                        data={"status": "complete", "fallback": "api-server"},
                    )
                )
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except Exception:
                # Hermes already accepted the prompt. A local fanout failure
                # must not turn that acceptance into a retryable submission.
                pass
        return PromptReceipt(operation_id=operation_id, status="completed")

    @staticmethod
    def _session(raw: dict[str, Any]) -> HermesSession:
        stored_id = _bounded_text(
            raw.get("stored_session_id") or raw.get("session_key") or raw.get("id"),
            label="stored session id",
            max_length=512,
        )
        runtime_value = raw.get("session_id")
        runtime_id = (
            _bounded_text(runtime_value, label="runtime session id", max_length=512)
            if runtime_value is not None
            else None
        )
        return HermesSession(
            stored_session_id=stored_id,
            runtime_session_id=runtime_id,
            title=str(raw["title"])[:500] if raw.get("title") is not None else None,
            status=str(raw.get("status") or "idle")[:30],
        )

    @staticmethod
    def _automation(
        raw: dict[str, Any], *, timezone_name: str | None = None
    ) -> HermesAutomation:
        wrapped = raw.get("job") if isinstance(raw.get("job"), dict) else raw
        next_values = (
            wrapped.get("next_runs")
            or wrapped.get("nextRuns")
            or wrapped.get("next_run")
            or []
        )
        if not isinstance(next_values, (list, tuple)):
            next_values = [next_values]
        next_runs: list[datetime] = []
        for value in next_values:
            if len(next_runs) == 5:
                break
            parsed = HermesGatewayProvider._automation_datetime(value)
            if parsed is not None:
                next_runs.append(parsed)
        next_run_at = HermesGatewayProvider._automation_datetime(
            wrapped.get("next_run_at") or wrapped.get("nextRunAt")
        )
        if next_run_at is not None and next_run_at not in next_runs:
            next_runs.insert(0, next_run_at)
        schedule_value = wrapped.get("schedule")
        schedule_kind = None
        if isinstance(schedule_value, Mapping):
            schedule_kind = schedule_value.get("kind")
            schedule_value = (
                schedule_value.get("expr")
                or schedule_value.get("run_at")
                or schedule_value.get("display")
            )
        if (
            len(next_runs) < 5
            and next_run_at is not None
            and schedule_kind == "cron"
            and isinstance(schedule_value, str)
        ):
            try:
                schedule_timezone = ZoneInfo(
                    timezone_name or str(wrapped.get("timezone") or "UTC")
                )
                iterator = croniter(
                    schedule_value,
                    next_run_at.astimezone(schedule_timezone),
                )
                while len(next_runs) < 5:
                    candidate = iterator.get_next(datetime)
                    if candidate.tzinfo is None:
                        candidate = candidate.replace(tzinfo=schedule_timezone)
                    next_runs.append(candidate)
            except (KeyError, ValueError, ZoneInfoNotFoundError):
                # The first timestamp remains authoritative. An unexpected
                # expression must not make the whole cron inventory vanish.
                pass
        return HermesAutomation(
            automation_id=_bounded_text(
                wrapped.get("id") or wrapped.get("job_id"),
                label="automation id",
                max_length=512,
            ),
            name=_bounded_text(
                wrapped.get("name") or "Automation",
                label="automation name",
                max_length=200,
            ),
            schedule=_bounded_text(
                schedule_value, label="automation schedule", max_length=200
            ),
            timezone=_bounded_text(
                timezone_name or wrapped.get("timezone") or "UTC",
                label="automation timezone",
                max_length=100,
            ),
            enabled=bool(wrapped.get("enabled", True)),
            prompt=str(wrapped.get("prompt") or wrapped.get("message") or "")[:200_000],
            next_runs=tuple(next_runs),
        )

    @staticmethod
    def _automation_datetime(value: Any) -> datetime | None:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                parsed = datetime.fromtimestamp(value, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return None
        else:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


class InMemoryHermesProvider:
    """Deterministic provider used offline and by integration tests."""

    _profiles_by_gateway: dict[str, dict[str, HermesProfile]] = defaultdict(dict)

    def __init__(self, connection: ProviderConnection, event_sink: EventSink | None = None) -> None:
        self.connection = connection
        self.event_sink = event_sink
        self._created_profiles = self._profiles_by_gateway[connection.gateway_id]
        self._sessions: dict[str, HermesSession] = {}
        self._messages: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._automations: dict[str, HermesAutomation] = {}
        self._automation_runs: dict[str, list[HermesRunReceipt]] = defaultdict(list)
        self._sequence = 0
        self._epoch = uuid4().hex
        self.fail_next_prompt_after_accept = False
        self._instance_epoch = uuid4().hex
        self._model = {
            "provider": "mock",
            "model": "mock-model",
        }
        self._config: dict[str, Any] = {
            "model": {"provider": "mock", "default": "mock-model"},
            "display": {"theme": "dark"},
            "memory": {"provider": ""},
        }
        self._soul = f"You are the deterministic {connection.profile_name} test profile."
        self._memory_provider = ""
        self._memory_sizes = {"memory": 128, "user": 64}
        self._skills: dict[str, dict[str, Any]] = {
            "planning": {
                "name": "planning",
                "description": "Deterministic planning skill",
                "category": "core",
                "enabled": True,
            },
            "research": {
                "name": "research",
                "description": "Deterministic research skill",
                "category": "core",
                "enabled": False,
            },
        }
        self._toolsets: dict[str, dict[str, Any]] = {
            "web": {
                "name": "web",
                "label": "Web",
                "description": "Deterministic web tools",
                "enabled": True,
                "configured": True,
                "tools": ["web_search", "web_extract"],
            },
            "terminal": {
                "name": "terminal",
                "label": "Terminal",
                "description": "Deterministic terminal tools",
                "enabled": False,
                "configured": True,
                "tools": ["terminal", "process"],
            },
        }
        self._mcp_servers: dict[str, dict[str, Any]] = {}
        self._channels: dict[str, dict[str, Any]] = {
            "telegram": {
                "id": "telegram",
                "name": "Telegram",
                "enabled": False,
                "configured": False,
                "state": "disabled",
                "env_vars": [
                    {"key": "TELEGRAM_BOT_TOKEN", "required": True, "is_set": False}
                ],
            }
        }
        self._secret_values: dict[str, str] = {}
        self._pending_approvals: dict[str, SessionRoute] = {}
        self._pending_clarifications: dict[str, SessionRoute] = {}

    @property
    def runtime_generation(self) -> str:
        return f"{self._instance_epoch}:1"

    @property
    def session_inventory_complete(self) -> bool:
        return True

    async def capabilities(self) -> CapabilitySet:
        return CapabilitySet(
            version="mock-1",
            source_sha="in-memory",
            methods=frozenset(
                {
                    "profiles.list",
                    "profiles.create",
                    "session.list",
                    "session.create",
                    "session.resume",
                    "session.history",
                    "session.search",
                    "session.interrupt",
                    "approval.respond",
                    "clarify.respond",
                    "session.delete",
                    "prompt.submit",
                    "cron.list",
                    "cron.create",
                    "cron.update",
                    "cron.delete",
                    "cron.trigger",
                    "models.list",
                    "models.set",
                    "config.get",
                    "config.set",
                    "soul.get",
                    "soul.set",
                    "memory.get",
                    "memory.provider.set",
                    "memory.reset",
                    "skills.list",
                    "skills.toggle",
                    "toolsets.list",
                    "toolsets.toggle",
                    "mcp.list",
                    "mcp.create",
                    "mcp.delete",
                    "mcp.toggle",
                    "mcp.test",
                    "channels.list",
                    "channels.update",
                    "channels.test",
                    "usage.get",
                    "secrets.list",
                    "secrets.set",
                    "secrets.delete",
                }
            ),
            features=frozenset(
                {"streaming", "replay", "automations", "administration"}
            ),
        )

    async def list_profiles(self) -> list[HermesProfile]:
        label = {"default": "Newton", "jarvis": "Jarvis"}.get(
            self.connection.profile_name, self.connection.profile_name
        )
        own = HermesProfile(
            self.connection.profile_name, label, "online", "mock-model"
        )
        return [own, *self._created_profiles.values()]

    async def create_profile(
        self,
        *,
        name: str,
        display_name: str,
        description: str,
    ) -> HermesProfile:
        if name == self.connection.profile_name or name in self._created_profiles:
            raise JsonRpcError(4062, "profile already exists")
        profile = HermesProfile(name, display_name, "online", "mock-model")
        self._created_profiles[name] = profile
        return profile

    async def list_sessions(self) -> list[HermesSession]:
        return sorted(self._sessions.values(), key=lambda row: row.updated_at, reverse=True)

    async def search_sessions(
        self, query: str, *, limit: int = 20
    ) -> list[HermesSearchResult]:
        needle = query.strip().casefold()
        if not needle:
            return []
        safe_limit = max(1, min(int(limit), _MAX_SEARCH_RESULTS))
        matches: list[HermesSearchResult] = []
        for session in sorted(
            self._sessions.values(), key=lambda row: row.updated_at, reverse=True
        ):
            messages = self._messages.get(session.stored_session_id, [])
            matching_message = next(
                (
                    message
                    for message in messages
                    if needle in str(message.get("content") or "").casefold()
                ),
                None,
            )
            title_matches = needle in str(session.title or "").casefold()
            id_matches = needle in session.stored_session_id.casefold()
            if matching_message is None and not title_matches and not id_matches:
                continue
            matches.append(
                HermesSearchResult(
                    stored_session_id=session.stored_session_id,
                    snippet=(
                        str(matching_message.get("content") or "")
                        if matching_message is not None
                        else str(session.title or session.stored_session_id)
                    )[:4_000],
                    title=session.title,
                    role=(
                        str(matching_message.get("role") or "") or None
                        if matching_message is not None
                        else None
                    ),
                    lineage_root=session.stored_session_id,
                )
            )
            if len(matches) >= safe_limit:
                break
        return matches

    async def create_session(self, *, title: str | None = None) -> HermesSession:
        stored = uuid4().hex
        session = HermesSession(stored, uuid4().hex[:8], title or "Nueva conversación")
        self._sessions[stored] = session
        return session

    async def resume_session(self, stored_session_id: str) -> HermesSession:
        existing = self._sessions.get(stored_session_id)
        if existing is None:
            existing = HermesSession(stored_session_id, None, "Conversación recuperada")
        resumed = HermesSession(
            existing.stored_session_id,
            uuid4().hex[:8],
            existing.title,
            "idle",
            datetime.now(timezone.utc),
        )
        self._sessions[stored_session_id] = resumed
        return resumed

    async def history(
        self, route: SessionRoute, *, expected_runtime_generation: str | None = None
    ) -> list[dict[str, Any]]:
        return list(self._messages[route.stored_session_id])

    async def history_readonly(self, stored_session_id: str) -> list[dict[str, Any]]:
        return list(self._messages[stored_session_id])

    async def submit_prompt(
        self,
        route: SessionRoute,
        prompt: str,
        *,
        operation_id: str,
        expected_runtime_generation: str | None = None,
    ) -> PromptReceipt:
        if not route.runtime_session_id:
            raise RuntimeError("Session must be resumed before prompting")
        self._messages[route.stored_session_id].append(
            {"id": uuid4().hex, "role": "user", "content": prompt}
        )
        if self.fail_next_prompt_after_accept:
            self.fail_next_prompt_after_accept = False
            raise RuntimeError("PROMPT_DELIVERY_UNKNOWN")
        if self.event_sink is not None:
            asyncio.create_task(self._emit_response(route, prompt, operation_id))
        return PromptReceipt(operation_id=operation_id, status="streaming")

    async def _emit_response(self, route: SessionRoute, prompt: str, operation_id: str) -> None:
        if "[approval]" in prompt:
            request_id = uuid4().hex
            self._pending_approvals[request_id] = route
            self._sequence += 1
            await self.event_sink(
                NormalizedEvent.create(
                    type="approval.request",
                    gateway_id=self.connection.gateway_id,
                    profile_name=self.connection.profile_name,
                    stored_session_id=route.stored_session_id,
                    runtime_session_id=route.runtime_session_id,
                    sequence=self._sequence,
                    replay_epoch=self._epoch,
                    correlation_id=request_id,
                    runtime_generation=self.runtime_generation,
                    data={
                        "request_id": request_id,
                        "command": "mock-safe-command",
                        "description": "Aprobación determinista solicitada por el mock",
                        "allow_permanent": False,
                        "allow_session": True,
                        "choices": ["once", "session", "deny"],
                    },
                )
            )
            return
        if "[clarify]" in prompt:
            request_id = uuid4().hex[:8]
            self._pending_clarifications[request_id] = route
            self._sequence += 1
            await self.event_sink(
                NormalizedEvent.create(
                    type="clarify.request",
                    gateway_id=self.connection.gateway_id,
                    profile_name=self.connection.profile_name,
                    stored_session_id=route.stored_session_id,
                    runtime_session_id=route.runtime_session_id,
                    sequence=self._sequence,
                    replay_epoch=self._epoch,
                    correlation_id=request_id,
                    runtime_generation=self.runtime_generation,
                    data={
                        "request_id": request_id,
                        "question": "¿Qué opción prefieres?",
                        "choices": ["A", "B"],
                    },
                )
            )
            return
        content = f"Respuesta simulada: {prompt}"
        for part in (content[: len(content) // 2], content[len(content) // 2 :]):
            self._sequence += 1
            await self.event_sink(
                NormalizedEvent.create(
                    type="message.delta",
                    gateway_id=self.connection.gateway_id,
                    profile_name=self.connection.profile_name,
                    stored_session_id=route.stored_session_id,
                    runtime_session_id=route.runtime_session_id,
                    sequence=self._sequence,
                    replay_epoch=self._epoch,
                    correlation_id=operation_id,
                    runtime_generation=self.runtime_generation,
                    data={"delta": part},
                )
            )
            await asyncio.sleep(0)
        self._messages[route.stored_session_id].append(
            {"id": uuid4().hex, "role": "assistant", "content": content}
        )
        self._sequence += 1
        await self.event_sink(
            NormalizedEvent.create(
                type="message.completed",
                gateway_id=self.connection.gateway_id,
                profile_name=self.connection.profile_name,
                stored_session_id=route.stored_session_id,
                runtime_session_id=route.runtime_session_id,
                sequence=self._sequence,
                replay_epoch=self._epoch,
                correlation_id=operation_id,
                runtime_generation=self.runtime_generation,
                data={"status": "completed"},
            )
        )

    async def interrupt(
        self, route: SessionRoute, *, expected_runtime_generation: str | None = None
    ) -> None:
        if self.event_sink is not None:
            self._sequence += 1
            await self.event_sink(
                NormalizedEvent.create(
                    type="session.interrupted",
                    gateway_id=self.connection.gateway_id,
                    profile_name=self.connection.profile_name,
                    stored_session_id=route.stored_session_id,
                    runtime_session_id=route.runtime_session_id,
                    sequence=self._sequence,
                    replay_epoch=self._epoch,
                    runtime_generation=self.runtime_generation,
                    data={},
                )
            )

    async def respond_approval(
        self,
        route: SessionRoute,
        request_id: str,
        choice: str,
        *,
        expected_runtime_generation: str | None = None,
    ) -> dict[str, Any]:
        pending = self._pending_approvals.get(request_id)
        if pending is None or pending.stored_session_id != route.stored_session_id:
            return {"resolved": 0}
        self._pending_approvals.pop(request_id, None)
        await self._emit_gate_completion(route, f"Aprobación: {choice}")
        return {"resolved": 1}

    async def respond_clarification(
        self,
        route: SessionRoute,
        request_id: str,
        answer: str | list[str],
        *,
        question_id: str | None = None,
        expected_runtime_generation: str | None = None,
    ) -> dict[str, Any]:
        pending = self._pending_clarifications.get(request_id)
        if pending is None or pending.stored_session_id != route.stored_session_id:
            return {"status": "expired", "remaining": []}
        self._pending_clarifications.pop(request_id, None)
        rendered = ", ".join(answer) if isinstance(answer, list) else answer
        await self._emit_gate_completion(route, f"Aclaración: {rendered}")
        return {"status": "ok", "remaining": []}

    async def _emit_gate_completion(self, route: SessionRoute, content: str) -> None:
        self._messages[route.stored_session_id].append(
            {"id": uuid4().hex, "role": "assistant", "content": content}
        )
        if self.event_sink is None:
            return
        self._sequence += 1
        await self.event_sink(
            NormalizedEvent.create(
                type="message.completed",
                gateway_id=self.connection.gateway_id,
                profile_name=self.connection.profile_name,
                stored_session_id=route.stored_session_id,
                runtime_session_id=route.runtime_session_id,
                sequence=self._sequence,
                replay_epoch=self._epoch,
                runtime_generation=self.runtime_generation,
                data={"status": "completed", "text": content},
            )
        )

    async def delete_session(self, route: SessionRoute) -> None:
        self._sessions.pop(route.stored_session_id, None)
        self._messages.pop(route.stored_session_id, None)

    async def list_automations(self) -> list[HermesAutomation]:
        return list(self._automations.values())

    async def create_automation(self, automation: HermesAutomation) -> HermesAutomation:
        created = HermesAutomation(
            automation_id=automation.automation_id or uuid4().hex,
            name=automation.name,
            schedule=automation.schedule,
            timezone=automation.timezone,
            enabled=automation.enabled,
            prompt=automation.prompt,
            next_runs=tuple(
                datetime.now(timezone.utc) + timedelta(hours=index + 1) for index in range(5)
            ),
        )
        self._automations[created.automation_id] = created
        return created

    async def update_automation(
        self, automation_id: str, changes: dict[str, Any]
    ) -> HermesAutomation:
        current = self._automations[automation_id]
        updated = HermesAutomation(
            automation_id=current.automation_id,
            name=str(changes.get("name", current.name)),
            schedule=str(changes.get("schedule", current.schedule)),
            timezone=str(changes.get("timezone", current.timezone)),
            enabled=bool(changes.get("enabled", current.enabled)),
            prompt=str(changes.get("prompt", current.prompt)),
            next_runs=current.next_runs,
        )
        self._automations[automation_id] = updated
        return updated

    async def delete_automation(self, automation_id: str) -> None:
        self._automations.pop(automation_id, None)
        self._automation_runs.pop(automation_id, None)

    async def trigger_automation(self, automation_id: str) -> HermesRunReceipt:
        if automation_id not in self._automations:
            raise KeyError(automation_id)
        session = await self.create_session(
            title=f"Automation · {self._automations[automation_id].name}"
        )
        now = datetime.now(timezone.utc)
        receipt = HermesRunReceipt(
            run_id=uuid4().hex,
            status="completed",
            stored_session_id=session.stored_session_id,
            runtime_session_id=session.runtime_session_id,
            started_at=now,
            finished_at=now,
        )
        self._automation_runs[automation_id].insert(0, receipt)
        return receipt

    async def list_automation_runs(
        self, automation_id: str, *, limit: int = 100
    ) -> list[HermesRunReceipt]:
        if automation_id not in self._automations:
            raise KeyError(automation_id)
        return list(self._automation_runs.get(automation_id, ()))[: max(1, min(limit, 100))]

    async def list_models(self) -> AdminResourceSnapshot:
        return admin_snapshot(
            "models",
            {
                "current": dict(self._model),
                "providers": [
                    {
                        "id": "mock",
                        "label": "Mock",
                        "configured": True,
                        "models": ["mock-model", "mock-model-small"],
                    }
                ],
            },
        )

    async def set_model(
        self,
        provider: str,
        model: str,
        *,
        confirm_expensive_model: bool = False,
    ) -> AdminResourceSnapshot:
        del confirm_expensive_model
        provider = _bounded_text(provider, label="model provider", max_length=120)
        model = _bounded_text(model, label="model name", max_length=300)
        self._model = {"provider": provider, "model": model}
        self._config["model"] = {"provider": provider, "default": model}
        return admin_snapshot(
            "models", {"ok": True, "scope": "main", **self._model}
        )

    async def get_config(self) -> AdminResourceSnapshot:
        return admin_snapshot("config", self._config)

    async def update_config(self, config: dict[str, Any]) -> AdminResourceSnapshot:
        if contains_secret_fields(config):
            raise ValueError(
                "Secret-shaped config values must use the write-only secrets endpoint"
            )
        self._config = {**self._config, **config}
        return admin_snapshot("config", {"ok": True, "config": self._config})

    async def get_soul(self) -> AdminResourceSnapshot:
        return admin_snapshot("soul", {"content": self._soul, "exists": True})

    async def update_soul(self, content: str) -> AdminResourceSnapshot:
        if len(content.encode("utf-8")) > 256 * 1024:
            raise ValueError("SOUL content is too large")
        self._soul = content
        return admin_snapshot("soul", {"ok": True, "exists": True})

    async def get_memory(self) -> AdminResourceSnapshot:
        return admin_snapshot(
            "memory",
            {
                "active": self._memory_provider,
                "providers": [
                    {"name": "", "label": "Built-in", "available": True},
                    {"name": "mock-memory", "label": "Mock Memory", "available": True},
                ],
                "builtin_files": dict(self._memory_sizes),
            },
        )

    async def set_memory_provider(self, name: str) -> AdminResourceSnapshot:
        if name not in {"", "mock-memory"}:
            raise KeyError(name)
        self._memory_provider = name
        return admin_snapshot("memory", {"ok": True, "active": name})

    async def reset_memory(self, target: str) -> AdminResourceSnapshot:
        if target not in {"all", "memory", "user"}:
            raise ValueError("Memory reset target must be all, memory, or user")
        deleted: list[str] = []
        for key, filename in (("memory", "MEMORY.md"), ("user", "USER.md")):
            if target in {"all", key} and self._memory_sizes[key]:
                self._memory_sizes[key] = 0
                deleted.append(filename)
        return admin_snapshot("memory", {"ok": True, "deleted": deleted})

    async def list_skills(self) -> AdminResourceSnapshot:
        return admin_snapshot("skills", list(self._skills.values()))

    async def toggle_skill(self, name: str, enabled: bool) -> AdminResourceSnapshot:
        if name not in self._skills:
            raise KeyError(name)
        self._skills[name]["enabled"] = enabled
        return admin_snapshot(
            "skills", {"ok": True, "name": name, "enabled": enabled}
        )

    async def list_toolsets(self) -> AdminResourceSnapshot:
        return admin_snapshot("toolsets", list(self._toolsets.values()))

    async def toggle_toolset(self, name: str, enabled: bool) -> AdminResourceSnapshot:
        if name not in self._toolsets:
            raise KeyError(name)
        self._toolsets[name]["enabled"] = enabled
        return admin_snapshot(
            "toolsets", {"ok": True, "name": name, "enabled": enabled}
        )

    async def list_mcp_servers(self) -> AdminResourceSnapshot:
        return admin_snapshot(
            "mcp", {"servers": list(self._mcp_servers.values())}
        )

    async def create_mcp_server(self, server: dict[str, Any]) -> AdminResourceSnapshot:
        name = _bounded_text(server.get("name"), label="MCP server name", max_length=120)
        if name in self._mcp_servers:
            raise KeyError(name)
        summary = {
            "name": name,
            "url": server.get("url"),
            "command": server.get("command"),
            "args": list(server.get("args") or []),
            "enabled": bool(server.get("enabled", True)),
            "configured": True,
            "env": {
                str(key): {"configured": bool(value)}
                for key, value in dict(server.get("env") or {}).items()
            },
            "auth": (
                {"configured": bool(server.get("auth"))}
                if server.get("auth")
                else None
            ),
        }
        self._mcp_servers[name] = summary
        return admin_snapshot("mcp", summary)

    async def delete_mcp_server(self, name: str) -> AdminResourceSnapshot:
        if self._mcp_servers.pop(name, None) is None:
            raise KeyError(name)
        return admin_snapshot("mcp", {"ok": True, "name": name})

    async def toggle_mcp_server(
        self, name: str, enabled: bool
    ) -> AdminResourceSnapshot:
        if name not in self._mcp_servers:
            raise KeyError(name)
        self._mcp_servers[name]["enabled"] = enabled
        return admin_snapshot(
            "mcp", {"ok": True, "name": name, "enabled": enabled}
        )

    async def test_mcp_server(self, name: str) -> AdminResourceSnapshot:
        if name not in self._mcp_servers:
            raise KeyError(name)
        return admin_snapshot(
            "mcp",
            {"ok": True, "name": name, "tools": ["mock_echo", "mock_status"]},
        )

    async def list_channels(self) -> AdminResourceSnapshot:
        return admin_snapshot(
            "channels", {"platforms": list(self._channels.values())}
        )

    async def update_channel(
        self, name: str, changes: dict[str, Any]
    ) -> AdminResourceSnapshot:
        channel = self._channels.get(name)
        if channel is None:
            raise KeyError(name)
        if "enabled" in changes:
            channel["enabled"] = bool(changes["enabled"])
        env = dict(changes.get("env") or {})
        clear_env = {str(item) for item in changes.get("clear_env") or []}
        for field in channel["env_vars"]:
            key = str(field["key"])
            if key in env:
                field["is_set"] = bool(str(env[key]).strip())
            if key in clear_env:
                field["is_set"] = False
        channel["configured"] = all(
            not field.get("required") or field.get("is_set")
            for field in channel["env_vars"]
        )
        channel["state"] = "ready" if channel["enabled"] and channel["configured"] else "disabled"
        return admin_snapshot(
            "channels", {"ok": True, "platform": name}
        )

    async def test_channel(self, name: str) -> AdminResourceSnapshot:
        channel = self._channels.get(name)
        if channel is None:
            raise KeyError(name)
        return admin_snapshot(
            "channels",
            {
                "ok": bool(channel["enabled"] and channel["configured"]),
                "platform": name,
                "state": channel["state"],
            },
        )

    async def get_usage(self, *, days: int = 30) -> AdminResourceSnapshot:
        if not 1 <= days <= 365:
            raise ValueError("Usage range must be between 1 and 365 days")
        return admin_snapshot(
            "usage",
            {
                "period_days": days,
                "totals": {
                    "total_input": 120,
                    "total_output": 80,
                    "total_sessions": len(self._sessions),
                },
                "daily": [],
                "by_model": [],
            },
        )

    async def list_secrets(self) -> AdminResourceSnapshot:
        names = sorted(
            set(self._secret_values) | {"OPENAI_API_KEY", "OPENROUTER_API_KEY"}
        )
        return admin_snapshot(
            "secrets",
            [
                {
                    "name": name,
                    "configured": name in self._secret_values,
                    "description": "Write-only mock secret",
                    "category": "provider",
                    "advanced": False,
                    "channelManaged": False,
                }
                for name in names
            ],
        )

    async def set_secret(self, name: str, value: str) -> AdminResourceSnapshot:
        name = _bounded_text(name, label="secret name", max_length=200)
        if not value or len(value) > 16_384:
            raise ValueError("Secret value is missing or too large")
        self._secret_values[name] = value
        return admin_snapshot(
            "secrets", {"name": name, "configured": True, "status": "applied"}
        )

    async def delete_secret(self, name: str) -> AdminResourceSnapshot:
        self._secret_values.pop(name, None)
        return admin_snapshot(
            "secrets", {"name": name, "configured": False, "status": "applied"}
        )

    async def close(self) -> None:
        return None
