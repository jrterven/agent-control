from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import hermes_client.provider as provider_module
from hermes_client import EventNormalizer, HermesGatewayProvider, HermesSession, JsonRpcClient, JsonRpcError, ProviderConnection, SessionRoute
from hermes_client.compatibility import HERMES_0212_SHA, HERMES_0216_SHA, PROFILE_TRANSFER_PAIRS
from hermes_client.history import project_history_message, project_history_turn_origins
from hermes_control_api.eventing import EventHub


def connection(sha=HERMES_0216_SHA):
    return ProviderConnection(gateway_id="test", profile_name="jarvis", rest_url="http://127.0.0.1:19119",
        ws_url="ws://127.0.0.1:19119/api/ws", trusted_source_sha=sha)


def frame(method="clarify", sid="runtime", identifier="srq-0123456789ab", **fields):
    params = {"session_id": sid}
    if method == "clarify":
        params["questions"] = [{"qid": "q1", "question": "Choose", "choices": ["One", "Two"], "multi_select": True},
                               {"qid": "q2", "question": "Explain"}]
    elif method == "approval":
        params.update(request_id="private-queue-id", command="echo safe", choices=["once", "deny"], allow_session=False)
    return {"jsonrpc": "2.0", "id": identifier, "method": method, "params": {**params, **fields}}


def owned(provider, sid="runtime", stored="stored"):
    route = SessionRoute("test", "jarvis", stored, sid)
    provider._remember_route(route)
    return route


class Socket:
    def __init__(self):
        self.frames = asyncio.Queue()
        self.sent = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        frame = await self.frames.get()
        if frame is None:
            raise StopAsyncIteration
        return json.dumps(frame)

    async def send(self, data):
        frame = json.loads(data)
        self.sent.append(frame)
        if frame.get("method") == "client.capabilities":
            await self.frames.put({"jsonrpc": "2.0", "id": frame["id"], "result": {"server_requests": ["approval", "clarify"]}})

    async def close(self):
        self.closed = True
        await self.frames.put(None)


class Connector:
    def __init__(self, socket):
        self.socket = socket

    def __await__(self):
        async def ready():
            return self.socket
        return ready().__await__()


@pytest.mark.asyncio
async def test_transport_negotiates_each_connection_handles_questions_and_refuses_unknown(monkeypatch):
    sockets = [Socket(), Socket()]
    pool = iter(sockets)
    monkeypatch.setattr("websockets.asyncio.client.connect", lambda *_args, **_kwargs: Connector(next(pool)))
    callback = AsyncMock(side_effect=lambda raw, generation: raw["method"] == "clarify")
    events = AsyncMock()
    client = JsonRpcClient(url="ws://127.0.0.1:19119/api/ws", gateway_id="test", profile_name="jarvis",
        server_request_callback=callback, event_callback=events)
    try:
        for generation, socket in enumerate(sockets, 1):
            await client.connect()
            assert socket.sent[0]["method"] == "client.capabilities"
            assert socket.sent[0]["params"] == {"server_requests": True}
            await socket.frames.put(frame())
            await socket.frames.put(frame("secret", identifier="srq-1123456789ab", key="DO-NOT-LOG"))
            for _ in range(30):
                await asyncio.sleep(0)
                if len(socket.sent) > 1:
                    break
            assert callback.call_args_list[-2].args[1] == generation
            assert socket.sent[-1] == {"jsonrpc": "2.0", "id": "srq-1123456789ab",
                "error": {"code": -32601, "message": "Request unsupported by Agent Control"}}
            assert events.await_count == 0  # Questions are never ordinary events.
            await client.close()
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("sha,negotiated", [(HERMES_0216_SHA, True), (HERMES_0212_SHA, False), ("f" * 40, False)])
async def test_only_exact_new_contract_installs_request_handler(sha, negotiated):
    provider = HermesGatewayProvider(connection(sha), AsyncMock())
    try:
        assert (provider.rpc.server_request_callback is not None) is negotiated
        assert (HERMES_0216_SHA, HERMES_0216_SHA) in PROFILE_TRANSFER_PAIRS
        assert (HERMES_0212_SHA, HERMES_0216_SHA) not in PROFILE_TRANSFER_PAIRS
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("version,reported,allowed", [
    ("0.21.6", HERMES_0216_SHA, True), ("0.21.2", HERMES_0216_SHA, False),
    ("0.21.6", HERMES_0212_SHA, False), ("0.21.7", HERMES_0216_SHA, False)])
async def test_new_writes_require_exact_version_revision_and_read_probes(monkeypatch, version, reported, allowed):
    provider = HermesGatewayProvider(connection())
    monkeypatch.setattr(provider, "_read", AsyncMock(return_value={}))
    async def read(client, method, path, **kwargs):
        if path == "/api/status":
            return {"version": version, "source_sha": reported}
        if path == "/api/sessions/search":
            return {"results": []}
        if path == "/api/cron/jobs":
            return {"jobs": []}
        return {}
    monkeypatch.setattr(provider_module, "bounded_json_request", read)
    try:
        caps = await provider.capabilities()
        assert ("prompt.submit" in caps.methods) is allowed
        assert ("clarify.respond" in caps.methods) is allowed  # Neutral Control capability.
        assert ("profiles.transfer" in caps.methods) is allowed
        assert "memory.get" not in caps.methods
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_live_requests_are_owned_redacted_and_answered_through_confirmed_native_rpc(monkeypatch):
    events = []
    async def sink(event):
        events.append(event)
    provider = HermesGatewayProvider(connection(), sink)
    route = owned(provider)
    reply = AsyncMock(return_value={"status": "ok"})
    monkeypatch.setattr(provider, "_interaction_request", reply)
    try:
        assert not await provider._on_server_request(frame(sid="foreign"), 0)
        assert not await provider._on_server_request(frame(profile="other"), 0)
        assert await provider._on_server_request(frame("approval", command="echo token=private"), 0)
        assert events[-1].type == "approval.request"
        assert events[-1].data["request_id"] == "srq-0123456789ab"
        assert events[-1].stored_session_id == route.stored_session_id
        assert "private" not in events[-1].data["command"]
        with pytest.raises(JsonRpcError):
            await provider.respond_approval(SessionRoute("test", "jarvis", "foreign", "runtime"), "srq-0123456789ab", "once")
        with pytest.raises(ValueError):
            await provider.respond_approval(route, "srq-0123456789ab", "always")
        assert not reply.called
        assert await provider.respond_approval(route, "srq-0123456789ab", "once") == {"resolved": 1}
        assert reply.call_args.args[1:] == ("request.answer", {"id": "srq-0123456789ab", "result": {"choice": "once"}})
        with pytest.raises(JsonRpcError):
            await provider.respond_approval(route, "srq-0123456789ab", "once")
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_clarify_locks_restore_after_reconnect_and_are_generation_scoped(monkeypatch):
    events = []
    async def sink(event):
        events.append(event)
    provider = HermesGatewayProvider(connection(), sink)
    route = owned(provider)
    reply = AsyncMock(side_effect=[{"status": "ok", "remaining": ["q2"]}, {"status": "ok", "remaining": []}])
    monkeypatch.setattr(provider, "_interaction_request", reply)
    try:
        assert await provider._on_server_request(frame(), 0)
        assert await provider.respond_clarification(route, "srq-0123456789ab", ["One", "custom"], question_id="q1") == {
            "status": "ok", "remaining": ["q2"]}
        assert reply.call_args.args[1] == "clarify.lock"
        assert json.loads(reply.call_args.args[2]["answer"]) == ["One", "custom"]
        assert await provider._on_server_request(frame(), 0)  # Stale same-generation replay.
        assert events[-1].data["questions"] == [{"qid": "q2", "question": "Explain"}]
        with pytest.raises(JsonRpcError):
            await provider.respond_clarification(route, "srq-0123456789ab", "again", question_id="q1")
        provider.rpc._generation += 1
        with pytest.raises(JsonRpcError):
            await provider.respond_clarification(route, "srq-0123456789ab", "stale", question_id="q2")
        owned(provider)
        raw = {"stored_session_id": "stored", "session_id": "runtime", "open_requests": [frame(answers={"q1": '["One", "custom"]'})]}
        session = provider._session(raw)
        assert session.status == "waiting"
        await provider._emit_resumed_interactions(raw, session)
        assert events[-1].data["questions"] == [{"qid": "q2", "question": "Explain"}]
        assert events[-1].runtime_generation == provider.runtime_generation
        assert await provider.respond_clarification(route, "srq-0123456789ab", "done", question_id="q2") == {"status": "ok", "remaining": []}
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_cancel_is_session_bound_and_empty_authoritative_snapshot_expires_gate():
    hub = EventHub()
    events = []
    async def sink(event):
        events.append(event)
        hub._update_interactions(event)
    provider = HermesGatewayProvider(connection(), sink)
    route = owned(provider)
    try:
        await provider._on_server_request(frame(), 0)
        normalizer = EventNormalizer(gateway_id="test", profile_name="jarvis")
        await provider._on_event(normalizer.normalize({"method": "event", "params": {"type": "request.cancel",
            "session_id": "foreign", "payload": {"id": "srq-0123456789ab", "method": "clarify"}}}))
        assert len(provider._server_requests) == 1 and len(hub._interactions) == 1
        await provider._restore_server_requests({"open_requests": []}, HermesSession("stored", "runtime", None, "idle"))
        assert events[-1].type == "clarify.expire"
        assert not provider._server_requests and not hub._interactions
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [frame("secret"), frame("sudo"), frame("window.read"), frame(questions=[]),
    frame(questions=[{"qid": "x", "question": "ok"}, {"qid": "x", "question": "duplicate"}]),
    frame(answers={"foreign": "answer"}), frame("approval", choices=["execute"]),
    {"id": "srq-0123456789ab", "method": [], "params": {}}])
async def test_unsupported_or_malformed_requests_never_become_human_controls(bad):
    sink = AsyncMock()
    provider = HermesGatewayProvider(connection(), sink)
    owned(provider)
    try:
        assert not await provider._on_server_request(bad, 0)
        assert not sink.called and not provider._server_requests
    finally:
        await provider.close()


def test_public_history_honors_display_projection_and_hidden_boundaries():
    rows = [{"id": 1, "role": "user", "content": "PRIVATE scaffolding", "display_kind": "hidden", "args": {"private": True}},
            {"id": 2, "role": "assistant", "content": "PRIVATE retained", "display_content": "Public reply",
             "display_commentary": ["unused"], "display_reasoning": "PRIVATE thoughts", "codex_reasoning_items": ["PRIVATE"]},
            {"id": 3, "role": "assistant", "content": "PRIVATE retained", "display_content": ""}]
    projected = project_history_turn_origins(rows)
    assert projected == [{"id": 1, "role": "system", "content": ""},
        {"id": 2, "role": "assistant", "content": "Public reply"}, {"id": 3, "role": "assistant", "content": ""}]
    assert "PRIVATE" not in json.dumps(projected)
    assert rows[0]["content"] == "PRIVATE scaffolding"
    assert project_history_message({"role": "assistant", "text": "raw", "display_content": []}) == {"role": "assistant", "text": ""}


@pytest.mark.asyncio
async def test_new_turn_keeps_native_queue_and_history_correlation(monkeypatch):
    provider = HermesGatewayProvider(connection(), AsyncMock())
    route = owned(provider)
    monkeypatch.setattr(provider, "_ensure_connected", AsyncMock())
    monkeypatch.setattr(provider, "_rpc_generation_for", lambda _: 0)
    request = AsyncMock(return_value={"status": "queued"})
    monkeypatch.setattr(provider.rpc, "request", request)
    try:
        receipt = await provider.submit_prompt(route, "explicit human request", operation_id="op")
        assert receipt.status == "queued"
        assert request.call_args.args[1]["queued"] is True
        assert ("stored", "op") in provider._human_continuations
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("method,params,canonical", [
    ("session.resume", {"session_id": "stored", "stored_session_id": "stored", "omit_messages": True},
     {"session_id": "stored", "omit_messages": True}),
    ("session.history", {"session_id": "runtime", "stored_session_id": "stored"}, {"session_id": "runtime"}),
    ("session.interrupt", {"session_id": "runtime", "stored_session_id": "stored"}, {"session_id": "runtime"}),
    ("prompt.submit", {"session_id": "runtime", "stored_session_id": "stored", "text": "Hi", "prompt": "Hi", "request_id": "op", "queued": True},
     {"session_id": "runtime", "text": "Hi", "queued": True}),
    ("control.session.history", {"stored_session_id": "stored"}, {"stored_session_id": "stored"}),
])
async def test_exact_strict_wire_drops_only_legacy_aliases(monkeypatch, strict, method, params, canonical):
    socket = Socket()
    monkeypatch.setattr("websockets.asyncio.client.connect", lambda *_args, **_kwargs: Connector(socket))
    client = JsonRpcClient(url="ws://127.0.0.1:19119/api/ws", gateway_id="test", profile_name="jarvis", strict_params=strict)
    try:
        await client.connect()
        pending = asyncio.create_task(client.request(method, params))
        for _ in range(30):
            await asyncio.sleep(0)
            if socket.sent:
                break
        assert socket.sent[-1]["params"] == {**(canonical if strict else params), "profile": "jarvis"}
        await socket.frames.put({"jsonrpc": "2.0", "id": socket.sent[-1]["id"], "result": {}})
        assert await pending == {}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unsupported_owned_snapshot_is_explicitly_rejected(monkeypatch):
    provider = HermesGatewayProvider(connection(), AsyncMock())
    monkeypatch.setattr(JsonRpcClient, "connected", property(lambda _: True))
    owned(provider)
    reject = AsyncMock()
    monkeypatch.setattr(provider.rpc, "reject_server_request", reject)
    try:
        await provider._restore_server_requests({"open_requests": [frame("sudo"), frame("secret", sid="foreign")]},
            HermesSession("stored", "runtime", None, "waiting"))
        reject.assert_awaited_once_with("srq-0123456789ab", expected_generation=0)
        assert not provider._server_requests
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("pending,expected", [(True, {"identity_settlement_pending": True}), (False, None), ("yes", None)])
async def test_profile_delete_exposes_only_neutral_pending_identity(monkeypatch, pending, expected):
    provider = HermesGatewayProvider(connection())
    monkeypatch.setattr(provider, "assert_default_management_server", AsyncMock())
    monkeypatch.setattr(provider, "_profile_mutation_json", AsyncMock(return_value={"ok": True,
        "settlement_pending": pending, "path": "/private/profile", "retry_command": "PRIVATE command"}))
    try:
        assert await provider.delete_profile("example") == expected
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["replay", "resume"])
async def test_snapshot_fence_preserves_live_question_received_after_rpc_response(monkeypatch, operation):
    events = []
    async def sink(event):
        events.append(event)
    provider = HermesGatewayProvider(connection(), sink)
    route = owned(provider)
    async def read(*_args, **_kwargs):
        raw = {"events": [], "open_requests": [], "stored_session_id": "stored", "session_id": "runtime"}
        await provider._on_server_request(frame("approval"), 0)
        return raw
    monkeypatch.setattr(provider, "_read", read)
    try:
        if operation == "replay":
            await provider.replay_since(route, 0)
        else:
            await provider.resume_session("stored")
        assert "srq-0123456789ab" in provider._server_requests
        assert not any(event.type == "approval.expire" for event in events)
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", ["approval", "clarify", "cancel"])
async def test_late_snapshot_cannot_reopen_confirmed_terminal_request(monkeypatch, terminal):
    events = []
    async def sink(event):
        events.append(event)
    provider = HermesGatewayProvider(connection(), sink)
    route = owned(provider)
    method = "approval" if terminal == "approval" else "clarify"
    original = frame(method)
    monkeypatch.setattr(provider, "_interaction_request", AsyncMock(return_value={"status": "ok", "remaining": []}))
    try:
        await provider._on_server_request(original, 0)
        snapshot = provider._server_request_snapshot()
        if terminal == "approval":
            await provider.respond_approval(route, original["id"], "once")
        elif terminal == "clarify":
            await provider.respond_clarification(route, original["id"], "answer", question_id="q1")
        else:
            await provider._on_event(EventNormalizer(gateway_id="test", profile_name="jarvis").normalize({"method": "event",
                "params": {"type": "request.cancel", "session_id": "runtime", "payload": {"id": original["id"], "method": method}}}))
        count = len(events)
        await provider._restore_server_requests({"open_requests": [original]}, HermesSession("stored", "runtime", None, "waiting"), snapshot)
        assert not provider._server_requests and len(events) == count
        provider.rpc._generation += 1
        owned(provider)
        await provider._restore_server_requests({"open_requests": [original]}, HermesSession("stored", "runtime", None, "waiting"), snapshot)
        assert not provider._server_requests  # An old-generation snapshot is never reinterpreted.
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_cancel_before_snapshot_is_bound_cannot_reopen_card():
    provider = HermesGatewayProvider(connection(), AsyncMock())
    snapshot = provider._server_request_snapshot()
    try:
        await provider._on_event(EventNormalizer(gateway_id="test", profile_name="jarvis").normalize({"method": "event",
            "params": {"type": "request.cancel", "session_id": "runtime", "payload": {"id": "srq-0123456789ab", "method": "approval"}}}))
        owned(provider)
        await provider._restore_server_requests({"open_requests": [frame("approval")]}, HermesSession("stored", "runtime", None, "waiting"), snapshot)
        assert not provider._server_requests and not provider.event_sink.called
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_snapshot_expiration_is_bound_to_both_stored_and_runtime_ids():
    provider = HermesGatewayProvider(connection(), AsyncMock())
    owned(provider)
    try:
        await provider._on_server_request(frame("approval"), 0)
        await provider._restore_server_requests({"open_requests": []}, HermesSession("stored", "different-runtime", None, "idle"))
        assert "srq-0123456789ab" in provider._server_requests
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_partial_answer_survives_snapshot_replacing_binding_while_rpc_waits(monkeypatch):
    provider = HermesGatewayProvider(connection(), AsyncMock())
    route = owned(provider)
    async def answer(*_args, **_kwargs):
        await provider._on_server_request(frame(), 0)
        return {"status": "ok", "remaining": ["q2"]}
    monkeypatch.setattr(provider, "_interaction_request", answer)
    try:
        await provider._on_server_request(frame(), 0)
        await provider.respond_clarification(route, "srq-0123456789ab", "One", question_id="q1")
        assert provider._server_requests["srq-0123456789ab"].locked_ids == frozenset({"q1"})
        with pytest.raises(JsonRpcError):
            await provider.respond_clarification(route, "srq-0123456789ab", "again", question_id="q1")
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_gate_retention_is_bounded_and_excludes_arbitrary_extensions():
    provider = HermesGatewayProvider(connection(), AsyncMock())
    owned(provider)
    try:
        assert await provider._on_server_request(frame("approval", private_extension="NEVER_RETAIN"), 0)
        assert "NEVER_RETAIN" not in json.dumps(provider._server_requests["srq-0123456789ab"].payload)
        assert not await provider._on_server_request(frame("approval", identifier="srq-1123456789ab", command="x" * 20_000), 0)
        assert "srq-1123456789ab" not in provider._server_requests
    finally:
        await provider.close()


def test_native_process_completion_model_input_is_not_public_user_text():
    projected = project_history_message({"role": "user", "display_kind": "process_complete", "content": "PRIVATE command output",
        "display_metadata": {"display_text": "PRIVATE extension"}, "tool_calls": [{"arguments": "PRIVATE"}]})
    assert projected == {"role": "system", "content": "Finalizó un proceso en segundo plano."}


@pytest.mark.asyncio
async def test_snapshot_cleanup_cannot_expire_rebound_request_after_sink_reconnect(monkeypatch):
    provider = HermesGatewayProvider(connection(), AsyncMock())
    owned(provider)
    retained = frame("approval", identifier="srq-1123456789ab")
    try:
        await provider._on_server_request(retained, 0)
        snapshot = provider._server_request_snapshot()
        original_handler = provider._on_server_request
        async def reconnect_during_restore(raw, generation):
            result = await original_handler(raw, generation)
            provider.rpc._generation += 1
            owned(provider)
            await original_handler(retained, provider.rpc.generation)
            return result
        monkeypatch.setattr(provider, "_on_server_request", reconnect_during_restore)
        await provider._restore_server_requests({"open_requests": [frame("approval")]},
            HermesSession("stored", "runtime", None, "waiting"), snapshot)
        assert provider._server_requests[retained["id"]].generation == provider.runtime_generation
    finally:
        await provider.close()
