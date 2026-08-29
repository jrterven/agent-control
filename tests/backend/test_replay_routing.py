from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from hermes_client import (
    HermesGatewayProvider,
    HermesSessionRouter,
    InMemoryHermesProvider,
    JsonRpcClient,
    NormalizedEvent,
    ProviderConnection,
    ProviderPool,
    ReplayState,
    RouteMismatchError,
    SessionRoute,
)
from hermes_control_api.eventing import EventHub
from hermes_control_api.api.routes import bind_owned_realtime_event
from hermes_control_api.models import Gateway, SessionLink, User


def event(seq: int, *, epoch: str = "a", event_id: str | None = None):
    return NormalizedEvent.create(
        event_id=event_id,
        type="message.delta",
        gateway_id="g1",
        profile_name="default",
        stored_session_id="stored",
        sequence=seq,
        replay_epoch=epoch,
    )


def test_replay_deduplicates_detects_gaps_and_resets_on_epoch():
    state = ReplayState()
    first = event(1, event_id="one")
    assert state.apply(first).accept
    assert state.apply(first).duplicate
    gap = state.apply(event(3, event_id="three"))
    assert gap.accept and gap.gap_detected and gap.requires_history
    reset = state.apply(event(1, epoch="b", event_id="new-epoch"))
    assert reset.accept and reset.epoch_changed and reset.requires_history
    assert state.last_sequence == 1


def test_control_restart_with_empty_buffer_forces_history_reconciliation():
    hub = EventHub()
    route_key = "gateway-1\x1fcontrol-dev\x1fruntime-7"

    replay, reconciliations = hub.replay_since(
        {route_key: {"seq": 47, "epoch": "before-control-restart"}}
    )

    assert replay == []
    assert len(reconciliations) == 1
    reconcile = reconciliations[0]
    assert reconcile["type"] == "control.reconcile"
    assert reconcile["gatewayId"] == "gateway-1"
    assert reconcile["profileName"] == "control-dev"
    assert reconcile["reconciliationRequired"] is True
    assert reconcile["data"] == {
        "reason": "buffer_empty",
        "historyRequired": True,
    }
    assert reconcile["_routeIdentity"] == "runtime-7"

    assert hub.replay_since({route_key: {"seq": 0, "epoch": ""}}) == ([], [])


def test_empty_buffer_reconcile_is_owner_bound_and_frontend_rehydratable(
    authenticated, app
):
    client, _ = authenticated
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        gateway = db.scalar(select(Gateway).order_by(Gateway.created_at))
        assert owner is not None and gateway is not None
        other = User(username="replay-other", password_hash="unused", is_admin=False)
        db.add(other)
        db.flush()
        owned_session = SessionLink(
            owner_id=owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-owned-replay",
            runtime_session_id="runtime-owned-replay",
        )
        other_session = SessionLink(
            owner_id=other.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-other-replay",
            runtime_session_id="runtime-other-replay",
        )
        db.add_all([owned_session, other_session])
        db.commit()
        owner_id = owner.id
        other_id = other.id
        owned_session_id = owned_session.id

    route_key = "\x1f".join(
        (gateway.id, "control-dev", "runtime-owned-replay")
    )
    _, reconciliations = EventHub().replay_since(
        {route_key: {"seq": 9, "epoch": "lost-control-buffer"}}
    )
    assert len(reconciliations) == 1
    internal = reconciliations[0]

    bound = bind_owned_realtime_event(
        app.state.session_factory,
        user_id=owner_id,
        payload=internal,
    )
    assert bound is not None
    assert bound["type"] == "control.reconcile"
    assert bound["controlSessionId"] == owned_session_id
    assert bound["sessionId"] == owned_session_id
    assert bound["storedSessionId"] == "stored-owned-replay"
    assert bound["runtimeSessionId"] == "runtime-owned-replay"
    assert bound["data"]["historyRequired"] is True
    assert "_routeIdentity" not in bound
    assert "routeKey" not in bound["data"]

    assert (
        bind_owned_realtime_event(
            app.state.session_factory,
            user_id=other_id,
            payload=internal,
        )
        is None
    )


@pytest.mark.asyncio
async def test_router_isolates_profiles_and_resumes_before_prompt():
    pool = ProviderPool(lambda connection: InMemoryHermesProvider(connection))
    router = HermesSessionRouter(pool)
    default = ProviderConnection("g1", "default", "http://x", "ws://x")
    jarvis = ProviderConnection("g1", "jarvis", "http://x", "ws://x")
    provider = await pool.get(default)
    session = await provider.create_session(title="One")
    route = SessionRoute("g1", "default", session.stored_session_id, None)

    routed, receipt = await router.submit_prompt(
        route=route,
        connection=default,
        prompt="hello",
        idempotency_key="same-request",
    )
    assert routed.runtime_session_id
    assert receipt.status == "streaming"

    _, duplicate_receipt = await router.submit_prompt(
        route=routed,
        connection=default,
        prompt="hello",
        idempotency_key="same-request",
    )
    assert duplicate_receipt.operation_id == receipt.operation_id
    assert len(await provider.history(routed)) == 1

    with pytest.raises(RouteMismatchError):
        await router.submit_prompt(
            route=routed,
            connection=jarvis,
            prompt="must not cross",
            idempotency_key="different",
        )


@pytest.mark.asyncio
async def test_history_uses_durable_read_without_resuming_a_runtime(monkeypatch):
    pool = ProviderPool(lambda connection: InMemoryHermesProvider(connection))
    router = HermesSessionRouter(pool)
    connection = ProviderConnection("g1", "default", "http://x", "ws://x")
    provider = await pool.get(connection)
    provider._messages["stored-readonly"].append(
        {"id": "one", "role": "assistant", "content": "durable"}
    )
    resume = AsyncMock(side_effect=AssertionError("history must not resume"))
    monkeypatch.setattr(provider, "resume_session", resume)

    routed, rows = await router.history(
        SessionRoute("g1", "default", "stored-readonly", "stale-runtime"),
        connection,
    )

    assert routed.runtime_session_id == "stale-runtime"
    assert rows == [{"id": "one", "role": "assistant", "content": "durable"}]
    resume.assert_not_awaited()
    await pool.close()


@pytest.mark.asyncio
async def test_idempotency_scope_includes_gateway_and_profile():
    pool = ProviderPool(lambda connection: InMemoryHermesProvider(connection))
    router = HermesSessionRouter(pool)
    default = ProviderConnection("g1", "default", "http://x", "ws://x")
    jarvis = ProviderConnection("g1", "jarvis", "http://x", "ws://x")
    shared_stored_id = "same-upstream-id"
    _, newton_receipt = await router.submit_prompt(
        route=SessionRoute("g1", "default", shared_stored_id),
        connection=default,
        prompt="for Newton",
        idempotency_key="same-browser-key",
    )
    _, jarvis_receipt = await router.submit_prompt(
        route=SessionRoute("g1", "jarvis", shared_stored_id),
        connection=jarvis,
        prompt="for Jarvis",
        idempotency_key="same-browser-key",
    )
    assert newton_receipt.operation_id != jarvis_receipt.operation_id


@pytest.mark.asyncio
async def test_prompt_timeout_after_dispatch_is_delivery_unknown(monkeypatch):
    provider = HermesGatewayProvider(
        ProviderConnection("g1", "control-dev", "http://x", "ws://x")
    )
    # Simulate the connected generation that `_ensure_connected` guarantees in
    # production while keeping this test focused on a timeout after dispatch.
    provider.rpc._generation = 1
    monkeypatch.setattr(type(provider.rpc), "connected", property(lambda _rpc: True))
    monkeypatch.setattr(provider, "_ensure_connected", AsyncMock(return_value=None))
    monkeypatch.setattr(provider.rpc, "request", AsyncMock(side_effect=TimeoutError()))
    route = SessionRoute("g1", "control-dev", "stored", "runtime")

    with pytest.raises(RuntimeError, match="^PROMPT_DELIVERY_UNKNOWN$"):
        await provider.submit_prompt(route, "do this once", operation_id="op-once")

    await provider.close()


@pytest.mark.asyncio
async def test_same_version_unknown_revision_never_enables_write_capabilities(monkeypatch):
    provider = HermesGatewayProvider(
        ProviderConnection("g1", "default", "http://x", "ws://x")
    )
    monkeypatch.setattr(provider, "_read", AsyncMock(return_value={}))
    response = httpx.Response(
        200,
        json={"version": "0.20.6", "source_sha": "f" * 40},
        request=httpx.Request("GET", "http://x/api/status"),
    )
    monkeypatch.setattr(provider.http, "get", AsyncMock(return_value=response))

    capabilities = await provider.capabilities()

    assert "session.list" in capabilities.methods
    # The synthetic status object is not a valid /api/cron/jobs list, so cron
    # is correctly left uncertified by the independent REST probe.
    assert "cron.list" not in capabilities.methods
    assert "prompt.submit" not in capabilities.methods
    assert "cron.create" not in capabilities.methods
    await provider.close()


@pytest.mark.asyncio
async def test_gateway_epoch_is_applied_to_session_cursor_and_forces_reconciliation(monkeypatch):
    emitted: list[NormalizedEvent] = []

    async def collect(item: NormalizedEvent) -> None:
        emitted.append(item)

    provider = HermesGatewayProvider(
        ProviderConnection("g1", "default", "http://x", "ws://x"), collect
    )
    route = SessionRoute("g1", "default", "stored", "runtime")
    provider._remember_route(route)
    await provider._on_event(
        NormalizedEvent.create(
            type="gateway.ready",
            gateway_id="g1",
            profile_name="default",
            replay_epoch="epoch-old",
        )
    )
    await provider._on_event(
        NormalizedEvent.create(
            type="message.delta",
            gateway_id="g1",
            profile_name="default",
            runtime_session_id="runtime",
            sequence=100,
            data={"text": "before restart"},
        )
    )
    assert emitted[-1].replay_epoch == "epoch-old"
    monkeypatch.setattr(
        provider.rpc,
        "request",
        AsyncMock(
            return_value={
                "events": [],
                "latest_seq": 0,
                "truncated": False,
                "epoch": "epoch-new",
            }
        ),
    )

    await provider._recover_after_reconnect()

    reconcile = [item for item in emitted if item.type == "control.reconcile"]
    assert reconcile and reconcile[-1].data["reason"] == "epoch_changed"
    await provider.close()


@pytest.mark.asyncio
async def test_connection_supervisor_reconnects_without_another_user_request():
    emitted: list[NormalizedEvent] = []

    async def collect(item: NormalizedEvent) -> None:
        emitted.append(item)

    class DisconnectedRpc:
        connected = False
        generation = 1

        async def connect(self) -> None:
            self.connected = True
            self.generation += 1

        async def close(self) -> None:
            self.connected = False

    provider = HermesGatewayProvider(
        ProviderConnection("g1", "default", "http://x", "ws://x"), collect
    )
    provider.rpc = DisconnectedRpc()  # type: ignore[assignment]
    provider._ever_connected = True
    provider._ensure_supervisor()
    for _ in range(100):
        if provider.rpc.connected:
            break
        await asyncio.sleep(0.01)

    assert provider.rpc.connected
    states = [item.data.get("state") for item in emitted if item.type == "control.connection"]
    assert states[:2] == ["reconnecting", "connected"]
    await provider.close()


@pytest.mark.asyncio
async def test_stale_rpc_workers_cannot_close_or_use_new_generation():
    class QuietSocket:
        def __init__(self) -> None:
            self.closed = False
            self.sent: list[str] = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def send(self, message: str) -> None:
            self.sent.append(message)

        async def close(self) -> None:
            self.closed = True

    client = JsonRpcClient(
        url="ws://example.invalid/api/ws",
        gateway_id="gateway-1",
        profile_name="control-dev",
        heartbeat_interval=0.001,
        inbound_deadline=60,
    )
    old_socket = QuietSocket()
    new_socket = QuietSocket()
    never = asyncio.Event()
    new_reader = asyncio.create_task(never.wait())
    new_heartbeat = asyncio.create_task(never.wait())
    client._socket = new_socket
    client._generation = 2
    client._last_inbound = asyncio.get_running_loop().time()
    client._reader = new_reader
    client._heartbeat = new_heartbeat

    await client._read_loop(old_socket, 1)
    await client._heartbeat_loop(old_socket, 1)

    assert client._socket is new_socket
    assert client.generation == 2
    assert not new_socket.closed
    assert not new_reader.done()
    assert not new_heartbeat.done()
    assert old_socket.sent == []

    await client.close()
    assert new_socket.closed
