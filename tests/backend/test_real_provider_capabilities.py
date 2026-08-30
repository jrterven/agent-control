from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

import hermes_client.provider as provider_module
from hermes_client import (
    HermesAutomation,
    HermesGatewayProvider,
    JsonRpcError,
    ProviderConnection,
)


AUDITED_0206_SHA = "9978706e9303dbf990d90e744b131361449d73b9"
AUDITED_0205_SHA = "791e2ae3257e211d14ca77e654dfe10ee1976a1c"


def _connection(*, trusted_source_sha: str | None = None, api: bool = False):
    return ProviderConnection(
        gateway_id="gateway",
        profile_name="control-dev",
        rest_url="http://127.0.0.1:19119",
        ws_url="ws://127.0.0.1:19119/api/ws",
        api_url="http://127.0.0.1:18642" if api else None,
        api_key="api-key" if api else None,
        trusted_source_sha=trusted_source_sha,
    )


def _official_api_capabilities() -> dict:
    return {
        "object": "hermes.api_server.capabilities",
        "features": {
            "session_resources": True,
            "session_chat": True,
            "session_chat_streaming": True,
        },
        "endpoints": {
            "sessions": {"method": "GET", "path": "/api/sessions"},
            "session_create": {"method": "POST", "path": "/api/sessions"},
            "session_delete": {
                "method": "DELETE",
                "path": "/api/sessions/{session_id}",
            },
            "session_messages": {
                "method": "GET",
                "path": "/api/sessions/{session_id}/messages",
            },
            "session_chat": {
                "method": "POST",
                "path": "/api/sessions/{session_id}/chat",
            },
        },
    }


@pytest.mark.asyncio
async def test_official_8642_features_and_endpoints_are_parsed(monkeypatch):
    provider = HermesGatewayProvider(
        _connection(trusted_source_sha=AUDITED_0206_SHA, api=True)
    )

    async def disconnected_read(method: str, params=None):
        raise ConnectionError("dashboard socket unavailable")

    async def fake_request(client, method: str, path: str, **kwargs):
        if client is provider.http and path == "/api/status":
            return {"version": "0.20.6"}
        if client is provider.http and path == "/api/sessions/search":
            return {"results": []}
        if client is provider.api and path == "/v1/capabilities":
            return _official_api_capabilities()
        raise AssertionError((client, method, path, kwargs))

    monkeypatch.setattr(provider, "_read", disconnected_read)
    monkeypatch.setattr(provider_module, "bounded_json_request", fake_request)
    try:
        capabilities = await provider.capabilities()
    finally:
        await provider.close()

    assert capabilities.protocol == "openai-compatible"
    # The operator trust anchor enables the audited contract internally but
    # remains write-only when Hermes does not report its own revision.
    assert capabilities.source_sha is None
    assert {
        "session.list",
        "session.create",
        "session.resume",
        "session.history",
        "prompt.submit",
        "session.delete",
    }.issubset(capabilities.methods)


@pytest.mark.asyncio
async def test_api_fallback_write_endpoints_remain_read_only_without_trusted_sha(
    monkeypatch,
):
    provider = HermesGatewayProvider(_connection(api=True))

    async def disconnected_read(method: str, params=None):
        raise ConnectionError("dashboard socket unavailable")

    async def fake_request(client, method: str, path: str, **kwargs):
        if client is provider.http and path == "/api/status":
            # A server-controlled SHA is diagnostic only and cannot certify a
            # write contract.
            return {"version": "0.20.6", "source_sha": AUDITED_0206_SHA}
        if client is provider.http and path == "/api/sessions/search":
            return {"results": []}
        if client is provider.api and path == "/v1/capabilities":
            body = _official_api_capabilities()
            body["methods"] = ["models.set", "prompt.submit"]
            return body
        raise AssertionError((client, method, path, kwargs))

    monkeypatch.setattr(provider, "_read", disconnected_read)
    monkeypatch.setattr(provider_module, "bounded_json_request", fake_request)
    try:
        capabilities = await provider.capabilities()
    finally:
        await provider.close()

    assert {"session.list", "session.history"}.issubset(capabilities.methods)
    assert {
        "session.create",
        "session.resume",
        "prompt.submit",
        "session.delete",
        "models.set",
    }.isdisjoint(capabilities.methods)


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_sha", ["0" * 40, 123, ""])
async def test_reported_sha_that_contradicts_operator_anchor_disables_writes(
    monkeypatch, reported_sha,
):
    provider = HermesGatewayProvider(
        _connection(trusted_source_sha=AUDITED_0206_SHA)
    )

    async def fake_read(method: str, params=None):
        return {}

    async def fake_request(client, method: str, path: str, **kwargs):
        assert client is provider.http
        if path == "/api/status":
            return {"version": "0.20.6", "source_sha": reported_sha}
        if path == "/api/cron/jobs":
            return []
        if path == "/api/config":
            return {"timezone": "America/Mexico_City"}
        return {}

    monkeypatch.setattr(provider, "_read", fake_read)
    monkeypatch.setattr(provider_module, "bounded_json_request", fake_request)
    try:
        capabilities = await provider.capabilities()
    finally:
        await provider.close()

    assert capabilities.source_sha == (
        reported_sha if isinstance(reported_sha, str) and reported_sha else None
    )
    assert "session.list" in capabilities.methods
    assert "cron.list" in capabilities.methods
    assert capabilities.methods.isdisjoint(
        {
            "session.create",
            "session.resume",
            "prompt.submit",
            "session.interrupt",
            "approval.respond",
            "clarify.respond",
            "cron.create",
            "cron.update",
            "cron.delete",
            "cron.trigger",
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("version", "trusted_sha"),
    [("0.20.5", AUDITED_0205_SHA), ("0.20.6", AUDITED_0206_SHA)],
)
async def test_audited_real_provider_uses_rest_cron_probe_and_hides_memory(
    monkeypatch, version: str, trusted_sha: str
):
    provider = HermesGatewayProvider(
        _connection(trusted_source_sha=trusted_sha)
    )
    rpc_calls: list[str] = []
    http_calls: list[str] = []

    async def fake_read(method: str, params=None):
        rpc_calls.append(method)
        return {}

    async def fake_request(client, method: str, path: str, **kwargs):
        assert client is provider.http
        http_calls.append(path)
        if path == "/api/status":
            return {"version": version}
        if path == "/api/cron/jobs":
            assert kwargs["params"] == {"profile": "control-dev"}
            return []
        if path == "/api/config":
            return {"timezone": "America/Mexico_City"}
        if path == "/api/memory":
            raise AssertionError("profile-blind memory endpoint was probed")
        return {}

    monkeypatch.setattr(provider, "_read", fake_read)
    monkeypatch.setattr(provider_module, "bounded_json_request", fake_request)
    try:
        capabilities = await provider.capabilities()
    finally:
        await provider.close()

    assert "cron.list" not in rpc_calls
    assert "/api/cron/jobs" in http_calls
    assert "cron.list" in capabilities.methods
    assert "cron.create" in capabilities.methods
    assert "session.create" in capabilities.methods
    assert "profiles.create" in capabilities.methods
    assert {"approval.respond", "clarify.respond"}.issubset(capabilities.methods)
    assert not any(method.startswith("memory.") for method in capabilities.methods)


@pytest.mark.asyncio
async def test_profile_creation_uses_fresh_shared_auth_without_writing_soul(monkeypatch):
    provider = HermesGatewayProvider(
        _connection(trusted_source_sha=AUDITED_0205_SHA)
    )
    monkeypatch.setattr(provider, "_ensure_connected", AsyncMock())
    request = AsyncMock(
        return_value={
            "ok": True,
            "name": "researcher",
            "path": "/private/profile/path",
            "soul_written": True,
            "model_set": False,
            "mirrored": {
                "env": False,
                "auth": "shared",
                "model_inherited": True,
                "voice": True,
            },
        }
    )
    monkeypatch.setattr(provider.rpc, "request", request)
    try:
        created = await provider.create_profile(
            name="researcher",
            display_name="Researcher",
        )
    finally:
        await provider.close()

    assert created.name == "researcher"
    assert created.display_name == "Researcher"
    assert created.status == "unknown"
    request.assert_awaited_once()
    method, params = request.await_args.args
    assert method == "profiles.create"
    assert params["mirror_credentials"] is True
    assert params["share_auth"] is True
    assert params["clone_all"] is False
    assert "clone_from" not in params
    assert "description" not in params
    assert "soul" not in params


@pytest.mark.asyncio
async def test_profile_creation_marks_partial_setup_degraded(monkeypatch):
    provider = HermesGatewayProvider(
        _connection(trusted_source_sha=AUDITED_0205_SHA)
    )
    monkeypatch.setattr(provider, "_ensure_connected", AsyncMock())
    monkeypatch.setattr(
        provider.rpc,
        "request",
        AsyncMock(
            return_value={
                "ok": True,
                "name": "researcher",
                "soul_written": False,
                "model_set": False,
                "mirrored": {"auth": "shared", "model_inherited": False},
            }
        ),
    )
    try:
        created = await provider.create_profile(
            name="researcher",
            display_name="Researcher",
        )
    finally:
        await provider.close()

    assert created.status == "degraded"


@pytest.mark.asyncio
async def test_audited_writes_require_a_successful_session_list_probe(monkeypatch):
    provider = HermesGatewayProvider(
        _connection(trusted_source_sha=AUDITED_0206_SHA)
    )

    async def fake_read(method: str, params=None):
        if method == "session.list":
            raise JsonRpcError(5004, "session database unavailable")
        return {}

    async def fake_request(client, method: str, path: str, **kwargs):
        assert client is provider.http
        if path == "/api/status":
            return {"version": "0.20.6"}
        if path == "/api/sessions/search":
            return {"results": []}
        raise ValueError("optional read module unavailable")

    monkeypatch.setattr(provider, "_read", fake_read)
    monkeypatch.setattr(provider_module, "bounded_json_request", fake_request)
    try:
        capabilities = await provider.capabilities()
    finally:
        await provider.close()

    assert "session.list" not in capabilities.methods
    assert {
        "session.create",
        "session.resume",
        "session.delete",
        "prompt.submit",
        "session.interrupt",
        "approval.respond",
        "clarify.respond",
    }.isdisjoint(capabilities.methods)


@pytest.mark.asyncio
async def test_human_gate_rpc_uses_exact_official_methods_and_params(monkeypatch):
    provider = HermesGatewayProvider(
        _connection(trusted_source_sha=AUDITED_0206_SHA)
    )

    class RunningReader:
        @staticmethod
        def done() -> bool:
            return False

    provider.rpc._socket = object()
    provider.rpc._reader = RunningReader()  # type: ignore[assignment]
    provider.rpc._generation = 7
    calls: list[tuple[str, dict, int | None]] = []

    async def fake_request(method: str, params=None, **kwargs):
        calls.append((method, dict(params or {}), kwargs.get("expected_generation")))
        if method == "approval.respond":
            return {"resolved": 1}
        return {"status": "ok", "remaining": ["q1"]}

    monkeypatch.setattr(provider.rpc, "request", fake_request)
    route = provider_module.SessionRoute(
        "gateway", "control-dev", "stored-1", "runtime-1"
    )
    generation = provider.runtime_generation
    try:
        approval = await provider.respond_approval(
            route,
            "approval-1",
            "session",
            expected_runtime_generation=generation,
        )
        clarification = await provider.respond_clarification(
            route,
            "clarify-1",
            ["web", "terminal"],
            question_id="q0",
            expected_runtime_generation=generation,
        )
    finally:
        provider.rpc._socket = None
        provider.rpc._reader = None
        await provider.close()

    assert approval == {"resolved": 1}
    assert clarification == {"status": "ok", "remaining": ["q1"]}
    assert calls == [
        (
            "approval.respond",
            {
                "session_id": "runtime-1",
                "request_id": "approval-1",
                "choice": "session",
            },
            7,
        ),
        (
            "clarify.respond",
            {
                "request_id": "clarify-1",
                "answer": ["web", "terminal"],
                "question_id": "q0",
            },
            7,
        ),
    ]


@pytest.mark.asyncio
async def test_resume_replays_official_pending_human_gates(monkeypatch):
    emitted = []

    async def sink(event):
        emitted.append(event)

    provider = HermesGatewayProvider(
        _connection(trusted_source_sha=AUDITED_0206_SHA), sink
    )

    async def fake_read(method: str, params=None):
        assert method == "session.resume"
        return {
            "session_id": "runtime-new",
            "session_key": "stored-1",
            "pending_approval": {
                "request_id": "approval-1",
                "command": "echo safe",
                "choices": ["once", "deny"],
            },
            "pending_clarify": {
                "request_id": "clarify-1",
                "question": "¿Continuar?",
                "choices": ["sí", "no"],
            },
        }

    monkeypatch.setattr(provider, "_read", fake_read)
    try:
        resumed = await provider.resume_session("stored-1")
    finally:
        await provider.close()

    assert resumed.stored_session_id == "stored-1"
    assert [event.type for event in emitted] == [
        "approval.request",
        "clarify.request",
    ]
    assert all(event.stored_session_id == "stored-1" for event in emitted)
    assert all(event.runtime_session_id == "runtime-new" for event in emitted)


@pytest.mark.asyncio
async def test_cron_writes_follow_audited_contract_when_hermes_uses_local_timezone(
    monkeypatch,
):
    provider = HermesGatewayProvider(
        _connection(trusted_source_sha=AUDITED_0206_SHA)
    )

    async def fake_read(method: str, params=None):
        return {}

    async def fake_request(client, method: str, path: str, **kwargs):
        if path == "/api/status":
            return {"version": "0.20.6"}
        if path == "/api/cron/jobs":
            return []
        if path == "/api/config":
            return {"timezone": ""}
        return {}

    monkeypatch.setattr(provider, "_read", fake_read)
    monkeypatch.setattr(provider_module, "bounded_json_request", fake_request)
    try:
        capabilities = await provider.capabilities()
    finally:
        await provider.close()

    assert "cron.list" in capabilities.methods
    assert {"cron.create", "cron.update", "cron.delete", "cron.trigger"}.issubset(
        capabilities.methods
    )


@pytest.mark.asyncio
async def test_cron_create_and_update_use_hermes_local_timezone_when_config_is_empty(
    monkeypatch,
):
    provider = HermesGatewayProvider(
        _connection(trusted_source_sha=AUDITED_0206_SHA)
    )
    requests: list[tuple[str, str, dict]] = []

    async def fake_request(client, method: str, path: str, **kwargs):
        assert client is provider.http
        requests.append((method, path, kwargs))
        if path == "/api/config":
            return {"timezone": ""}
        if method == "POST" and path == "/api/cron/jobs":
            return {
                "id": "cron-local",
                "name": "Local cron",
                "schedule": {"kind": "cron", "expr": "0 9 * * *"},
                "prompt": "Run locally",
                "enabled": True,
            }
        if method == "PUT" and path == "/api/cron/jobs/cron-local":
            return {
                "id": "cron-local",
                "name": "Updated local cron",
                "schedule": {"kind": "cron", "expr": "30 9 * * *"},
                "prompt": "Run locally",
                "enabled": True,
            }
        raise AssertionError((method, path, kwargs))

    monkeypatch.setattr(provider_module, "bounded_json_request", fake_request)
    try:
        created = await provider.create_automation(
            HermesAutomation(
                automation_id="",
                name="Local cron",
                schedule="0 9 * * *",
                timezone="Hermes local",
                enabled=True,
                prompt="Run locally",
            )
        )
        updated = await provider.update_automation(
            created.automation_id,
            {"name": "Updated local cron", "schedule": "30 9 * * *"},
        )
    finally:
        await provider.close()

    assert created.timezone == "Hermes local"
    assert updated.timezone == "Hermes local"
    assert any(
        method == "POST"
        and path == "/api/cron/jobs"
        and kwargs["json"]["schedule"] == "0 9 * * *"
        for method, path, kwargs in requests
    )
    assert any(
        method == "PUT"
        and path == "/api/cron/jobs/cron-local"
        and kwargs["json"]["updates"]["schedule"] == "30 9 * * *"
        for method, path, kwargs in requests
    )


@pytest.mark.asyncio
async def test_official_api_create_response_extracts_nested_session(monkeypatch):
    provider = HermesGatewayProvider(
        _connection(trusted_source_sha=AUDITED_0206_SHA, api=True)
    )

    async def disconnected():
        raise ConnectionError("dashboard socket unavailable")

    async def fake_request(client, method: str, path: str, **kwargs):
        assert client is provider.api
        assert (method, path) == ("POST", "/api/sessions")
        return {
            "object": "hermes.session",
            "session": {"id": "stored-official", "title": "Official session"},
        }

    monkeypatch.setattr(provider, "_ensure_connected", disconnected)
    monkeypatch.setattr(provider_module, "bounded_json_request", fake_request)
    try:
        session = await provider.create_session(title="Official session")
    finally:
        await provider.close()

    assert session.stored_session_id == "stored-official"
    assert session.runtime_session_id == "api:stored-official"
    assert session.title == "Official session"
