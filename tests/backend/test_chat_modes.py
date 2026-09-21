from sqlalchemy import select
import pytest
from unittest.mock import AsyncMock
from hermes_client import HermesGatewayProvider, SessionRoute
from .test_real_provider_capabilities import _connection

from hermes_control_api.models import SessionLink, IdempotencyOperation, AuditEvent
from .conftest import mutation_headers
from .test_api_sessions import gateway_id


def create(client, csrf, mode):
    result = client.post("/api/v1/sessions", headers=mutation_headers(csrf, mode), json={
        "gatewayId": gateway_id(client), "profileName": "control-dev", "chatMode": mode,
    })
    assert result.status_code == 201, result.text
    return result.json()


def test_readonly_mode_survives_history_and_bootstrap(authenticated):
    client, csrf = authenticated
    session = create(client, csrf, "memory_read_only")
    assert session["chatMode"] == "memory_read_only"
    rows = client.get("/api/v1/bootstrap").json()["sessions"]
    assert next(row for row in rows if row["id"] == session["id"])["chatMode"] == "memory_read_only"


def test_temporary_content_never_enters_durable_history(authenticated):
    client, csrf = authenticated
    session = create(client, csrf, "temporary")
    sid = session["id"]
    assert sid.startswith("tmp_")
    assert session["storedSessionId"].startswith("ac_tmp_")
    assert create(client, csrf, "temporary")["id"] == sid
    headers = {**mutation_headers(csrf, "private-prompt"), "X-Temporary-Chat": session["temporaryAccess"]}
    assert client.get(f"/api/v1/sessions/{sid}/messages").status_code == 404
    result = client.post(f"/api/v1/sessions/{sid}/prompts", headers=headers, json={"content": "private canary"})
    assert result.status_code == 202, result.text
    history = client.get(f"/api/v1/sessions/{sid}/messages", headers=headers)
    assert history.status_code == 200, history.text
    assert any(row["content"] == "private canary" for row in history.json()["items"])
    assert sid not in str(client.get("/api/v1/bootstrap").json())
    with client.app.state.session_factory() as db:
        assert db.get(SessionLink, sid) is None
        assert not db.scalars(select(IdempotencyOperation).where(IdempotencyOperation.scope == f"session:{sid}:prompt")).all()
        for operation in db.scalars(select(IdempotencyOperation)):
            assert session["temporaryAccess"] not in str(operation.response_json)
            assert "private canary" not in str(operation.response_json)
        assert not db.scalars(select(AuditEvent).where(AuditEvent.target_id == sid)).all()
    assert client.post(f"/api/v1/sessions/{sid}/temporary/renew", headers=headers).status_code == 204
    assert client.post(f"/api/v1/sessions/{sid}/temporary/close", headers=headers).status_code == 204
    assert client.post(f"/api/v1/sessions/{sid}/temporary/close", headers=headers).status_code == 204
    assert client.get(f"/api/v1/sessions/{sid}/messages", headers=headers).status_code == 410
    assert not client.app.state.services.temporary_chats.entries


def test_private_access_and_expiry_fail_closed(authenticated):
    import time
    client, csrf = authenticated
    session = create(client, csrf, "temporary")
    sid = session["id"]
    wrong_tab = {**mutation_headers(csrf, "wrong-tab"), "X-Temporary-Chat": "wrong"}
    assert client.get(f"/api/v1/sessions/{sid}/messages", headers=wrong_tab).status_code == 404
    headers = {**mutation_headers(csrf, "expired"), "X-Temporary-Chat": session["temporaryAccess"]}
    client.app.state.services.temporary_chats.entries[sid].expires_at = time.monotonic() - 1
    assert client.post(f"/api/v1/sessions/{sid}/temporary/renew", headers=headers).status_code == 410
    assert client.post(f"/api/v1/sessions/{sid}/prompts", headers=headers, json={"content": "late"}).status_code == 410


def test_invalid_mode_rejected(authenticated):
    client, csrf = authenticated
    result = client.post("/api/v1/sessions", headers=mutation_headers(csrf, "invalid"), json={
        "gatewayId": gateway_id(client), "profileName": "control-dev", "chatMode": "private-ish",
    })
    assert result.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_id", ["ac_tmp_closed", "ac_ro_retained", "compressed-child"])
async def test_restricted_turn_never_uses_legacy_api_after_disconnect(monkeypatch, stored_id):
    provider = HermesGatewayProvider(_connection(api=True))
    if stored_id == "compressed-child":
        provider._session({"stored_session_id": stored_id, "chat_mode": "memory_read_only"})
    monkeypatch.setattr(provider, "_ensure_connected", AsyncMock(side_effect=ConnectionError("disconnected")))
    try:
        with pytest.raises(RuntimeError, match="CHAT_MODE_UNAVAILABLE"):
            await provider.submit_prompt(SessionRoute("gateway", "control-dev", stored_id), "private", operation_id="private-op")
        with pytest.raises(ConnectionError):
            await provider.resume_session(stored_id)
    finally:
        await provider.close()
