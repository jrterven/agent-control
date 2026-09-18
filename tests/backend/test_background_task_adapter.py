from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from hermes_client import EventNormalizer, HermesGatewayProvider, InMemoryHermesProvider, NormalizedEvent, PromptReceipt, ProviderConnection, SessionRoute
from hermes_client.compatibility import HERMES_0212_SHA, HERMES_0206_SHA
from hermes_client.connector_protocol import READ_OPERATIONS, WRITE_OPERATIONS, ProtocolError, decode_message, encode_message, validate_arguments
from hermes_client.history import project_history_message, project_history_turn_origins


def connection(sha=HERMES_0212_SHA):
    return ProviderConnection(gateway_id="gateway", profile_name="default", rest_url="http://127.0.0.1:19119",
                              ws_url="ws://127.0.0.1:19119/api/ws", trusted_source_sha=sha)


@pytest.mark.parametrize("event_type,state", [
    ("subagent.spawn_requested", "queued"), ("subagent.start", "running"),
    ("subagent.tool", "running"), ("subagent.progress", "running"), ("subagent.complete", "completed"),
])
def test_child_lifecycle_keeps_only_public_identity_and_state(event_type, state):
    payload = {"subagent_id": "subagent-1", "parent_id": "parent-1", "delegation_id": "deleg_abcdef01",
               "status": "success", "goal": "PRIVATE-PROMPT", "text": "PRIVATE-TEXT", "summary": "PRIVATE-RESULT",
               "context": "PRIVATE-CONTEXT", "output_tail": [{"text": "PRIVATE-TOOL"}],
               "files_read": ["/Users/private/.env"], "model": "private-model", "child_session_id": "foreign-session",
               "controlTurn": {"id": "forged", "correlation": "human"}}
    event = EventNormalizer(gateway_id="gateway", profile_name="default").normalize({
        "method": "event", "params": {"type": event_type, "session_id": "parent-runtime",
        "stored_session_id": "parent-durable", "seq": 17, "payload": payload}})
    assert event.data == {"id": "subagent-1", "parentTaskId": "parent-1", "delegationId": "deleg_abcdef01",
                          "state": state, "title": "Tarea delegada"}
    assert event.runtime_session_id == "parent-runtime" and event.stored_session_id == "parent-durable"
    assert event.correlation_id is None
    assert "PRIVATE" not in str(event.to_dict()) and "foreign-session" not in str(event.to_dict())


@pytest.mark.parametrize("identifier", [None, {}, ["id"], "x" * 129, "/private/path", "id\nsecret"])
def test_unusable_child_identity_is_not_a_task(identifier):
    event = EventNormalizer(gateway_id="g", profile_name="p").normalize({
        "event": "subagent.start", "payload": {"subagent_id": identifier, "goal": "private"}})
    assert event.data == {"opaque": True}


def test_delegation_and_reasoning_events_do_not_project_internal_text():
    normalizer = EventNormalizer(gateway_id="g", profile_name="p")
    for event_type in ("delegation.status", "subagent.thinking", "status.update"):
        event = normalizer.normalize({"event": event_type, "payload": {"text": "PRIVATE", "goal": "PRIVATE"}})
        assert "PRIVATE" not in str(event.to_dict())


def native_event(kind, seq, sid="runtime-a", epoch="epoch-a"):
    return NormalizedEvent.create(type=kind, gateway_id="gateway", profile_name="default",
        runtime_session_id=sid, stored_session_id="stored-" + sid, sequence=seq, replay_epoch=epoch,
        data={"text": "visible"} if kind in {"message.delta", "message.complete"} else {})


@pytest.mark.asyncio
async def test_native_message_cycle_has_one_history_identity_and_never_claims_human_origin():
    provider = HermesGatewayProvider(connection())
    try:
        events = [native_event(kind, seq) for seq, kind in enumerate(
            ["message.start", "message.delta", "tool.started", "tool.completed", "message.complete"], 1)]
        for event in events:
            await provider._on_event(event)
        turn = events[0].data["controlTurn"]
        assert turn["correlation"] == "history" and len(turn["id"]) == 64
        assert all(event.data["controlTurn"] == turn and event.correlation_id is None for event in events)
        next_start = native_event("message.start", 6)
        await provider._on_event(next_start)
        assert next_start.data["controlTurn"]["id"] != turn["id"]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_stream_identity_is_session_generation_and_epoch_scoped_and_requires_start():
    provider = HermesGatewayProvider(connection())
    try:
        first = native_event("message.start", 10)
        await provider._on_event(first)
        for event in [native_event("message.delta", 11, "runtime-b"), native_event("message.delta", 9),
                      native_event("message.complete", 11, epoch="epoch-b")]:
            await provider._on_event(event)
            assert event.data["controlTurn"] == {"correlation": "history"}
        await provider._on_event(native_event("message.start", 20, epoch="epoch-b"))
        provider.rpc._generation += 1
        missed_start = native_event("tool.completed", 21, epoch="epoch-b")
        await provider._on_event(missed_start)
        assert missed_start.data["controlTurn"] == {"correlation": "history"}
        assert not provider._message_turns
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_unsequenced_and_duplicate_terminal_events_cannot_fall_back_to_human_pending():
    provider = HermesGatewayProvider(connection())
    try:
        await provider._on_event(native_event("message.start", 1))
        unsequenced = native_event("message.delta", None)
        await provider._on_event(unsequenced)
        assert unsequenced.data["controlTurn"] == {"correlation": "history"}
        await provider._on_event(native_event("message.start", 3))
        terminal = native_event("message.complete", 4)
        await provider._on_event(terminal)
        replay = native_event("message.complete", 4)
        await provider._on_event(replay)
        assert "id" in terminal.data["controlTurn"]
        assert replay.data["controlTurn"] == {"correlation": "history"}
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_legacy_stream_contract_is_unchanged():
    provider = HermesGatewayProvider(connection(HERMES_0206_SHA))
    try:
        event = native_event("message.complete", 10)
        await provider._on_event(event)
        assert event.data == {"text": "visible"}
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_replayed_start_cannot_capture_a_newer_turn_and_tracking_is_bounded():
    provider = HermesGatewayProvider(connection())
    provider._max_remembered_routes = 2
    try:
        start = native_event("message.start", 10)
        await provider._on_event(start)
        older = native_event("message.start", 1)
        await provider._on_event(older)
        assert older.data["controlTurn"] == {"correlation": "history"}
        current = native_event("message.delta", 11)
        await provider._on_event(current)
        assert current.data["controlTurn"] == start.data["controlTurn"]
        for sid in ("runtime-b", "runtime-c"):
            await provider._on_event(native_event("message.start", 1, sid))
        assert len(provider._message_turns) == len(provider._message_turn_sequences) == 2
        lost = native_event("message.delta", 12)
        await provider._on_event(lost)
        assert lost.data["controlTurn"] == {"correlation": "history"}
    finally:
        await provider.close()


@pytest.mark.parametrize("encode", [lambda value: value, json.dumps])
def test_async_notification_prompt_is_a_safe_system_marker_and_scopes_following_reply(encode):
    notification = {"role": "user", "id": 17, "content": "PRIVATE-NOTIFICATION-PROMPT",
                    "display_kind": "async_delegation_complete", "display_metadata": encode({
                        "delegation_id": "deleg_abcdef01", "display_text": "PRIVATE-DISPLAY",
                        "goal": "PRIVATE-GOAL", "reasoning": "PRIVATE-REASONING"})}
    raw = [{"role": "user", "content": "Human request"}, {"role": "assistant", "content": "Acknowledged"},
           notification, {"role": "assistant", "content": "Task result"},
           {"role": "user", "content": "Another request"}, {"role": "assistant", "content": "Another reply"}]
    projected = project_history_turn_origins(raw)
    origin = {"kind": "background_task", "taskId": "deleg_abcdef01"}
    assert projected[2] == {"id": 17, "role": "system", "content": "Resultado de una tarea en segundo plano.", "controlTurnOrigin": origin}
    assert projected[3]["controlTurnOrigin"] == origin
    assert "controlTurnOrigin" not in projected[1] and "controlTurnOrigin" not in projected[5]
    assert "PRIVATE" not in str(projected)
    assert "PRIVATE-NOTIFICATION-PROMPT" in str(raw)


@pytest.mark.parametrize("metadata", ["broken", "x" * 16_385, [], {"delegation_id": "/foreign/path"}])
def test_malformed_background_metadata_still_hides_synthetic_model_prompt(metadata):
    row = project_history_message({"role": "user", "content": "PRIVATE", "display_kind": "async_delegation_complete",
                                   "display_metadata": metadata, "context": "PRIVATE", "results": "PRIVATE"})
    assert row["role"] == "system" and row["controlTurnOrigin"] == {"kind": "background_task"}
    assert "PRIVATE" not in str(row)


def test_raw_history_cannot_forge_adapter_origin_and_unknown_notification_ends_scope():
    forged = {"role": "assistant", "content": "Reply", "controlTurnOrigin": {"kind": "background_task", "taskId": "forged"}}
    assert "controlTurnOrigin" not in project_history_message(forged)
    rows = project_history_turn_origins([
        {"role": "user", "display_kind": "async_delegation_complete", "content": "private"},
        {"role": "user", "display_kind": "other_notification", "content": "other"}, forged])
    assert "controlTurnOrigin" not in rows[-1]


@pytest.mark.parametrize("state", ["queued", "redirected", "steered"])
def test_native_busy_receipts_survive_typed_connector_transport(state):
    receipt = PromptReceipt(operation_id="user-operation", status=state)
    assert decode_message(encode_message({"v": 1, "receipt": receipt}))["receipt"] == receipt


@pytest.mark.asyncio
@pytest.mark.parametrize("sha,queue_protection", [(HERMES_0212_SHA, True), (HERMES_0206_SHA, False), (None, False)])
async def test_only_audited_native_prompt_dispatch_uses_atomic_busy_queue(monkeypatch, sha, queue_protection):
    provider = HermesGatewayProvider(connection(sha))
    monkeypatch.setattr(provider, "_ensure_connected", AsyncMock())
    monkeypatch.setattr(provider, "_rpc_generation_for", lambda _: 0)
    request = AsyncMock(return_value={"status": "queued"})
    monkeypatch.setattr(provider.rpc, "request", request)
    route = SessionRoute("gateway", "default", "stored-a", "runtime-a")
    try:
        receipt = await provider.submit_prompt(route, "Explicit human message", operation_id="human")
        params = request.call_args.args[1]
        assert (params.get("queued") is True) is queue_protection
        assert receipt.status == "queued" and receipt.operation_id == "human"
        assert provider._reattach_routes["stored-a"] == route
        assert request.await_count == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_inventory_does_not_use_global_or_incomplete_native_rpc(monkeypatch):
    provider = HermesGatewayProvider(connection())
    read = AsyncMock(side_effect=AssertionError("must not request global delegation.status"))
    monkeypatch.setattr(provider, "_read", read)
    try:
        assert await provider.list_background_tasks("stored-a") == {
            "available": False, "complete": False, "activeCount": None, "pendingDeliveryCount": None, "totalCount": None,
            "tasks": [], "reason": "unsupported_runtime", "source": "hermes-native-delegation"}
        assert read.await_count == 0
        assert await InMemoryHermesProvider(connection()).list_background_tasks() == {
            "available": True, "complete": True, "activeCount": 0, "pendingDeliveryCount": 0, "totalCount": 0,
            "tasks": [], "source": "hermes-native-delegation"}
    finally:
        await provider.close()
    assert "list_background_tasks" in READ_OPERATIONS and "list_background_tasks" not in WRITE_OPERATIONS
    validate_arguments("list_background_tasks", (), {"stored_session_id": "stored-a"})
    with pytest.raises(ProtocolError):
        validate_arguments("list_background_tasks", (), {"stored_session_id": 1})


def ledger(observed, *sessions, complete=True):
    return {"observedAt": observed, "complete": complete, "tasks": [
        {"storedSessionId": stored, "state": "running", "deliveryState": "pending"} for stored in sessions]}


@pytest.mark.asyncio
async def test_continuation_ledger_never_claims_other_routes_or_clears_newer_events():
    from datetime import datetime, timezone
    provider = HermesGatewayProvider(connection())
    route = SessionRoute("gateway", "default", "stored-a", "runtime-a")
    foreign = SessionRoute("gateway", "default", "stored-foreign", "runtime-foreign")
    try:
        provider._remember_route(route)
        provider._remember_route(foreign)  # Inventory alone grants no transport ownership.
        provider._mark_route_for_reattach(route)
        provider.observe_background_tasks(ledger("2026-09-18T20:00:00Z", "stored-a", "stored-foreign"))
        assert set(provider._background_continuations) == {"stored-a"}
        assert "stored-foreign" not in provider._reattach_routes
        provider._background_continuations["stored-a"] = datetime(2026, 9, 18, 20, 1, tzinfo=timezone.utc)
        provider.observe_background_tasks(ledger("2026-09-18T20:00:01Z"))
        assert provider._continuation_required("stored-a")
        provider.observe_background_tasks(ledger("2026-09-18T20:02:00Z", complete=False))
        assert provider._continuation_required("stored-a")
        provider.observe_background_tasks(ledger("2026-09-18T20:02:01Z"))
        assert not provider._continuation_required("stored-a")
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_queued_human_survives_background_terminal_and_requires_exact_history_answer(monkeypatch):
    provider = HermesGatewayProvider(connection())
    monkeypatch.setattr(provider, "_ensure_connected", AsyncMock())
    monkeypatch.setattr(provider, "_rpc_generation_for", lambda _: 0)
    monkeypatch.setattr(provider.rpc, "request", AsyncMock(return_value={"status": "queued"}))
    route = SessionRoute("gateway", "default", "stored-runtime-a", "runtime-a")
    try:
        provider._observe_history_continuations(route.stored_session_id, [{"id": 1, "role": "user", "content": "same request"}])
        await provider.submit_prompt(route, "same request", operation_id="human")
        await provider._on_event(native_event("message.start", 1))
        await provider._on_event(native_event("message.complete", 2))
        assert provider._continuation_required(route.stored_session_id)
        assert route.stored_session_id in provider._reattach_routes
        # An old repeated prompt, a synthetic notification, or a partial reply
        # cannot provide proof for the newly queued human message.
        provider._observe_history_continuations(route.stored_session_id, [
            {"id": 1, "role": "user", "content": "same request"},
            {"id": 2, "role": "assistant", "content": "Old", "finish_reason": "stop"},
            {"id": 3, "role": "user", "content": "same request", "display_kind": "async_delegation_complete"},
            {"id": 4, "role": "assistant", "content": "Background", "finish_reason": "stop"}])
        assert provider._continuation_required(route.stored_session_id)
        human = {"id": 5, "role": "user", "content": "same request"}
        provider._observe_history_continuations(route.stored_session_id, [human,
            {"id": 6, "role": "assistant", "content": "Partial", "finish_reason": None}])
        assert provider._continuation_required(route.stored_session_id)
        provider._observe_history_continuations(route.stored_session_id, [human,
            {"id": 6, "role": "user", "content": "Another turn"},
            {"id": 7, "role": "assistant", "content": "Other", "finish_reason": "stop"}])
        assert provider._continuation_required(route.stored_session_id)
        provider._observe_history_continuations(route.stored_session_id, [human,
            {"id": 6, "role": "assistant", "content": "Final", "finish_reason": "stop"}])
        assert not provider._continuation_required(route.stored_session_id)
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [False, True])
async def test_interrupt_clears_queued_human_only_after_confirmed_delivery(monkeypatch, confirmed):
    provider = HermesGatewayProvider(connection())
    route = SessionRoute("gateway", "default", "stored-a", "runtime-a")
    monkeypatch.setattr(provider, "_ensure_connected", AsyncMock())
    monkeypatch.setattr(provider, "_rpc_generation_for", lambda _: 0)
    monkeypatch.setattr(provider.rpc, "request", AsyncMock(return_value={} if confirmed else None,
        side_effect=None if confirmed else ConnectionError("disconnected")))
    provider._mark_route_for_reattach(route)
    provider._human_continuations[("stored-a", "human")] = ("digest", 0)
    try:
        if confirmed:
            await provider.interrupt(route)
            assert not provider._continuation_required("stored-a")
            assert "stored-a" not in provider._reattach_routes
        else:
            with pytest.raises(RuntimeError, match="INTERRUPT_DELIVERY_UNKNOWN"):
                await provider.interrupt(route)
            assert provider._continuation_required("stored-a")
            assert "stored-a" in provider._reattach_routes
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_only_new_audited_session_has_proven_empty_history_baseline(monkeypatch):
    provider = HermesGatewayProvider(connection())
    monkeypatch.setattr(provider, "_ensure_connected", AsyncMock())
    monkeypatch.setattr(provider.rpc, "request", AsyncMock(return_value={
        "session_id": "runtime-new", "stored_session_id": "stored-new", "status": "idle"}))
    try:
        session = await provider.create_session(title="New")
        assert provider._history_floors[session.stored_session_id] == 0
        provider._remember_route(SessionRoute("gateway", "default", "stored-other", "runtime-other"))
        assert "stored-other" not in provider._history_floors
    finally:
        await provider.close()
