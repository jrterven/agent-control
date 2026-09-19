from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from hermes_control_api.integrations import IntegrationError
from hermes_control_api.models import (
    AuditEvent, IdempotencyOperation, ProfileRef, SessionLink, User,
    UserIntegration, UserVoicePreference,
)
from hermes_control_api.openai_live import (
    LIVE_INSTRUCTIONS, OPENAI_LIVE_MAX_RESPONSE_BYTES, LiveSessionLimiter,
    OpenAILiveClient, live_history,
)
from hermes_control_api.security import hash_password
from hermes_control_api.services import SessionService


SECRET = "sk-proj-test_openai_private_123456789"
OFFER = "v=0\r\ns=private-offer\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
ANSWER = "v=0\r\ns=private-answer\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"


def headers(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex}


def configure(client, csrf: str, secret: str = SECRET) -> None:
    response = client.put("/api/v1/integrations/openai/key", headers=headers(csrf), json={"apiKey": secret})
    assert response.status_code == 200, response.text
    response = client.put("/api/v1/integrations/voice", headers=headers(csrf), json={"provider": "openai_live"})
    assert response.status_code == 200, response.text


class FakeLiveClient:
    def __init__(self):
        self.requests = []

    async def create_session(self, **kwargs):
        self.requests.append(kwargs)
        return {"session": {"id": f"opaque_live_{len(self.requests)}"}, "transport": {"type": "webrtc", "sdp": ANSWER}}


def profile_id(app) -> str:
    with app.state.session_factory() as db:
        return db.scalar(select(ProfileRef).where(ProfileRef.profile_name == "default")).id


def test_key_and_voice_preference_are_encrypted_owner_scoped_and_removable(authenticated, app):
    client, csrf = authenticated
    assert client.get("/api/v1/integrations/voice").json() == {"provider": "elevenlabs"}
    assert client.get("/api/v1/integrations/openai").json() == {"configured": False, "provider": "openai", "modelId": "gpt-live-1"}
    assert client.put("/api/v1/integrations/voice", headers=headers(csrf), json={"provider": "openai_live"}).status_code == 409
    elevenlabs_key = "sk_elevenlabs_original_private_12345"
    assert client.put("/api/v1/integrations/elevenlabs/key", headers=headers(csrf), json={"apiKey": elevenlabs_key}).status_code == 200
    configure(client, csrf)
    bootstrap = client.get("/api/v1/bootstrap")
    assert bootstrap.json()["features"]["voice"] == {"provider": "openai_live"}
    assert bootstrap.json()["features"]["live"] == {"available": True, "provider": "openai", "modelId": "gpt-live-1"}
    assert SECRET not in bootstrap.text
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        row = db.scalar(select(UserIntegration).where(UserIntegration.provider == "openai"))
        assert row.api_key_ciphertext.startswith("v1.")
        assert SECRET not in row.api_key_ciphertext
        assert app.state.services.vault.decrypt(row.api_key_ciphertext, aad=f"user-integration:{owner.id}:openai:api-key") == SECRET
        for aad in ["user-integration:other:openai:api-key", f"user-integration:{owner.id}:elevenlabs:api-key"]:
            with pytest.raises(ValueError):
                app.state.services.vault.decrypt(row.api_key_ciphertext, aad=aad)
        for model in (AuditEvent, IdempotencyOperation):
            assert SECRET not in repr([row.__dict__ for row in db.scalars(select(model)).all()])
        db.add(User(username="reader", password_hash=hash_password("reader password long enough"), is_admin=False))
        db.commit()
    login = client.post("/api/v1/auth/login", json={"username": "reader", "password": "reader password long enough"})
    reader_csrf = login.json()["csrfToken"]
    assert client.get("/api/v1/integrations/openai").json()["configured"] is False
    assert client.get("/api/v1/integrations/voice").json()["provider"] == "elevenlabs"
    configure(client, reader_csrf, "sk-reader_private_123456789")
    assert client.delete("/api/v1/integrations/openai/key", headers=headers(reader_csrf)).status_code == 204
    login = client.post("/api/v1/auth/login", json={"username": "admin", "password": "correct horse battery staple"})
    csrf = login.json()["csrfToken"]
    assert client.get("/api/v1/integrations/openai").json()["configured"] is True
    assert client.get("/api/v1/integrations/voice").json()["provider"] == "openai_live"
    assert client.delete("/api/v1/integrations/openai/key", headers=headers(csrf)).status_code == 204
    assert client.get("/api/v1/integrations/voice").json()["provider"] == "elevenlabs"
    assert client.get("/api/v1/integrations/openai").json()["configured"] is False
    assert client.get("/api/v1/integrations/elevenlabs").json()["configured"] is True


def test_live_sessions_require_authenticated_csrf_and_valid_bounded_payload(authenticated, app):
    client, csrf = authenticated
    fake = FakeLiveClient()
    app.state.openai_live_client = fake
    payload = {"sdp": OFFER, "profileId": profile_id(app)}
    assert client.post("/api/v1/realtime/live-session", json=payload).status_code == 403
    assert client.put("/api/v1/integrations/openai/key", json={"apiKey": SECRET}).status_code == 403
    assert client.put("/api/v1/integrations/openai/key", headers={"X-CSRF-Token": csrf}, json={"apiKey": SECRET}).status_code == 400
    for invalid in ["x", " " * 20, "sk-invalid\nprivate123456789", "s" * 513, {"value": SECRET}]:
        response = client.put("/api/v1/integrations/openai/key", headers=headers(csrf), json={"apiKey": invalid})
        assert response.status_code == 422
        assert SECRET not in response.text
    configure(client, csrf)
    for malformed in [
        {**payload, "sdp": OFFER + "a" * 65_536},
        {**payload, "sdp": "not SDP"},
        {**payload, "instructions": SECRET},
        {**payload, "agentContext": {"agent_name": "Spoofed agent"}},
        {**payload, "apiKey": SECRET},
    ]:
        response = client.post("/api/v1/realtime/live-session", headers=headers(csrf), json=malformed)
        assert response.status_code == 422
        assert SECRET not in response.text
    assert not fake.requests
    client.cookies.clear()
    assert client.get("/api/v1/integrations/openai").status_code == 401
    assert client.post("/api/v1/realtime/live-session", json=payload).status_code == 401


def test_live_exchange_is_no_store_and_never_replayed_or_audited_with_sdp(authenticated, app):
    client, csrf = authenticated
    configure(client, csrf)
    fake = FakeLiveClient()
    app.state.openai_live_client = fake
    payload = {"sdp": OFFER, "profileId": profile_id(app)}
    request_headers = headers(csrf)
    for sequence in [1, 2]:
        response = client.post("/api/v1/realtime/live-session/", headers=request_headers, json=payload)
        assert response.status_code == 201, response.text
        assert response.json()["session"]["id"] == f"opaque_live_{sequence}"
        assert response.headers["cache-control"] == "no-store"
        assert SECRET not in response.text
    assert [item["api_key"] for item in fake.requests] == [SECRET, SECRET]
    with app.state.session_factory() as db:
        assert db.scalar(select(IdempotencyOperation).where(IdempotencyOperation.idempotency_key == request_headers["Idempotency-Key"])) is None
        audit_rows = db.scalars(select(AuditEvent).where(AuditEvent.action == "integration.openai.live.create")).all()
        assert len(audit_rows) == 2
        for private in [OFFER, ANSWER, SECRET, "opaque_live_1", payload["profileId"]]:
            assert private not in repr([row.__dict__ for row in audit_rows])


def test_live_session_checks_profile_conversation_ownership_archive_and_operator_policy(authenticated, app, monkeypatch):
    client, csrf = authenticated
    configure(client, csrf)
    fake = FakeLiveClient()
    app.state.openai_live_client = fake
    pid = profile_id(app)
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        profile = db.get(ProfileRef, pid)
        reader = User(username="other", password_hash="unused", is_admin=False)
        db.add(reader)
        db.flush()
        foreign = SessionLink(owner_id=reader.id, gateway_id=profile.gateway_id, profile_name=profile.profile_name, stored_session_id="foreign")
        archived = SessionLink(owner_id=owner.id, gateway_id=profile.gateway_id, profile_name=profile.profile_name, stored_session_id="archived", archived_at=datetime.now(timezone.utc))
        wrong_profile = SessionLink(owner_id=owner.id, gateway_id=profile.gateway_id, profile_name="other-route", stored_session_id="wrong-route")
        db.add_all([foreign, archived, wrong_profile])
        db.commit()
        session_ids = [foreign.id, archived.id, wrong_profile.id]
    for sid in session_ids:
        response = client.post("/api/v1/realtime/live-session", headers=headers(csrf), json={"sdp": OFFER, "profileId": pid, "sessionId": sid})
        assert response.status_code == 404, response.text
    assert client.post("/api/v1/realtime/live-session", headers=headers(csrf), json={"sdp": OFFER, "profileId": str(uuid4())}).status_code == 404
    monkeypatch.setattr(app.state.services.settings, "mutable_profiles", [])
    monkeypatch.setattr(app.state.services.settings, "interactive_profiles", [])
    assert client.post("/api/v1/realtime/live-session", headers=headers(csrf), json={"sdp": OFFER, "profileId": pid}).status_code == 409
    assert not fake.requests


def test_live_session_seeds_only_owned_server_history(authenticated, app, monkeypatch):
    client, csrf = authenticated
    configure(client, csrf)
    pid = profile_id(app)
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        profile = db.get(ProfileRef, pid)
        conversation = SessionLink(owner_id=owner.id, gateway_id=profile.gateway_id, profile_name=profile.profile_name, stored_session_id="existing")
        db.add(conversation)
        db.commit()
        sid = conversation.id
    calls = []
    async def history(self, db, actor, row):
        calls.append((actor.username, row.id))
        return [{"role": "user", "content": "Remember Thursday"}]
    monkeypatch.setattr(SessionService, "history", history)
    fake = FakeLiveClient()
    app.state.openai_live_client = fake
    response = client.post("/api/v1/realtime/live-session", headers=headers(csrf), json={"sdp": OFFER, "profileId": pid, "sessionId": sid})
    assert response.status_code == 201, response.text
    assert calls == [("admin", sid)]
    assert fake.requests[0]["history"] == [{"role": "user", "content": "Remember Thursday"}]


def test_live_requires_own_key_and_rate_limit_but_not_exclusive_mode(authenticated, app):
    client, csrf = authenticated
    payload = {"sdp": OFFER, "profileId": profile_id(app)}
    fake = FakeLiveClient()
    app.state.openai_live_client = fake
    response = client.post("/api/v1/realtime/live-session", headers=headers(csrf), json=payload)
    assert response.status_code == 409
    assert fake.requests == []
    assert client.put("/api/v1/integrations/openai/key", headers=headers(csrf), json={"apiKey": SECRET}).status_code == 200
    assert client.get("/api/v1/integrations/voice").json()["provider"] == "elevenlabs"
    app.state.live_session_limiter = LiveSessionLimiter(limit=1, window_seconds=60)
    assert client.post("/api/v1/realtime/live-session", headers=headers(csrf), json=payload).status_code == 201
    limited = client.post("/api/v1/realtime/live-session", headers=headers(csrf), json=payload)
    assert limited.status_code == 429
    assert limited.json()["code"] == "LIVE_SESSION_RATE_LIMITED"
    assert int(limited.headers["retry-after"]) > 0
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_official_live_contract_fixed_origin_permissions_and_redacted_bounded_context():
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(201, json={"session": {"id": "opaque_id", "internal": SECRET}, "transport": {"type": "webrtc", "sdp": ANSWER}, "api_key": SECRET})
    history = [{"role": "system", "content": "never_forward"}, {"role": "tool", "content": "never_forward"}, {"role": "user", "content": "Remember jueves"}, {"role": "assistant", "content": "Confirmed " + SECRET}]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await OpenAILiveClient(client).create_session(api_key=SECRET, sdp=OFFER, history=history)
    assert result == {"session": {"id": "opaque_id"}, "transport": {"type": "webrtc", "sdp": ANSWER}}
    request = captured[0]
    assert str(request.url) == "https://api.openai.com/v1/live/sessions"
    assert request.headers["authorization"] == f"Bearer {SECRET}"
    body = json.loads(request.content)
    assert SECRET not in request.content.decode()
    assert body["transport"] == {"type": "webrtc", "sdp": OFFER}
    session = body["session"]
    assert session["model"] == "gpt-live-1"
    assert session["delegation"] == {"type": "client"}
    assert session["instructions"] == LIVE_INSTRUCTIONS
    assert session["store"] is False
    assert session["audio"] == {"output": {"voice": "marin"}}
    assert session["client"]["data_channel"]["allowed_client_events"] == [
        "session.commentary.append", "session.instructions.append",
        "session.thinking.append", "session.close",
    ]
    assert session["input"] == [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Remember jueves"}]}, {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Confirmed [REDACTED]"}]}]
    bounded = live_history([{"role": "user", "content": "é" * 10_000} for _ in range(200)], api_key=SECRET)
    assert sum(len(message["content"][0]["text"].encode()) for message in bounded) <= 6000
    assert len(bounded) <= 12


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [(401, "OPENAI_LIVE_ACCESS_DENIED"), (403, "OPENAI_LIVE_ACCESS_DENIED"), (404, "OPENAI_LIVE_ACCESS_DENIED"), (429, "OPENAI_LIVE_RATE_LIMITED"), (500, "OPENAI_LIVE_UNAVAILABLE"), (307, "OPENAI_LIVE_UNAVAILABLE")])
async def test_provider_errors_are_sanitized_and_redirects_never_followed(status, expected):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text=SECRET + OFFER, headers={"location": "https://attacker.invalid/key", "retry-after": "4"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        with pytest.raises(IntegrationError) as error:
            await OpenAILiveClient(client).create_session(api_key=SECRET, sdp=OFFER)
    assert error.value.code == expected
    assert SECRET not in str(error.value)
    assert OFFER not in str(error.value)
    assert error.value.retryable is False
    assert len(calls) == 1


def test_openai_voice_is_owner_scoped_and_survives_key_and_mode_changes(authenticated, app):
    client, csrf = authenticated
    voice_path = "/api/v1/integrations/openai/voice"
    assert client.get(voice_path).json() == {"voiceId": "marin"}
    with app.state.session_factory() as db:
        assert db.scalar(select(UserVoicePreference)) is None
    # A voice can be chosen before adding a credential or switching modes.
    response = client.put(voice_path, headers=headers(csrf), json={"voiceId": "willow"})
    assert response.status_code == 200
    assert response.json() == {"voiceId": "willow"}
    assert client.get("/api/v1/integrations/voice").json() == {"provider": "elevenlabs"}
    assert client.get("/api/v1/integrations/openai").json()["configured"] is False
    configure(client, csrf)
    assert client.get(voice_path).json() == {"voiceId": "willow"}
    replacement = "sk-proj-replacement_key_123456789"
    assert client.put("/api/v1/integrations/openai/key", headers=headers(csrf), json={"apiKey": replacement}).status_code == 200
    assert client.put("/api/v1/integrations/voice", headers=headers(csrf), json={"provider": "elevenlabs"}).json() == {"provider": "elevenlabs"}
    assert client.delete("/api/v1/integrations/openai/key", headers=headers(csrf)).status_code == 204
    assert client.get(voice_path).json() == {"voiceId": "willow"}
    with app.state.session_factory() as db:
        db.add(User(username="voice-reader", password_hash=hash_password("reader password long enough"), is_admin=False))
        db.commit()
    login = client.post("/api/v1/auth/login", json={"username": "voice-reader", "password": "reader password long enough"})
    reader_csrf = login.json()["csrfToken"]
    assert client.get(voice_path).json() == {"voiceId": "marin"}
    assert client.put(voice_path, headers=headers(reader_csrf), json={"voiceId": "cedar"}).status_code == 200
    with app.state.session_factory() as db:
        preferences = {
            name: preference.openai_voice_id
            for name, preference in db.execute(
                select(User.username, UserVoicePreference).join(
                    UserVoicePreference, UserVoicePreference.owner_id == User.id
                )
            )
        }
    assert preferences == {"admin": "willow", "voice-reader": "cedar"}


def test_openai_voice_mutation_is_authenticated_validated_and_idempotent(authenticated, app):
    client, csrf = authenticated
    voice_path = "/api/v1/integrations/openai/voice"
    assert client.put(voice_path, json={"voiceId": "marin"}).status_code == 403
    assert client.put(voice_path, headers={"X-CSRF-Token": csrf}, json={"voiceId": "marin"}).status_code == 400
    for invalid in ["unknown", "Marin", "", "marin\n", {"id": "custom_voice"}, None, 42]:
        response = client.put(voice_path, headers=headers(csrf), json={"voiceId": invalid})
        assert response.status_code == 422
    assert client.put(voice_path, headers=headers(csrf), json={"voiceId": "ash", "ownerId": "other"}).status_code == 422
    replay_headers = headers(csrf)
    for _ in range(2):
        response = client.put(voice_path, headers=replay_headers, json={"voiceId": "vesper"})
        assert response.status_code == 200
        assert response.json() == {"voiceId": "vesper"}
    with app.state.session_factory() as db:
        assert len(db.scalars(select(AuditEvent).where(AuditEvent.action == "integration.openai.voice.set")).all()) == 1
    client.cookies.clear()
    assert client.get(voice_path).status_code == 401
    assert client.put(voice_path, headers=headers(csrf), json={"voiceId": "marin"}).status_code == 401


def test_live_route_uses_saved_voice_and_rejects_unsaved_override(authenticated, app):
    client, csrf = authenticated
    configure(client, csrf)
    fake = FakeLiveClient()
    app.state.openai_live_client = fake
    payload = {"sdp": OFFER, "profileId": profile_id(app)}
    assert client.post("/api/v1/realtime/live-session", headers=headers(csrf), json=payload).status_code == 201
    assert fake.requests[-1]["voice_id"] == "marin"
    assert client.put("/api/v1/integrations/openai/voice", headers=headers(csrf), json={"voiceId": "quartz"}).status_code == 200
    assert client.post("/api/v1/realtime/live-session", headers=headers(csrf), json=payload).status_code == 201
    assert fake.requests[-1]["voice_id"] == "quartz"
    assert client.post("/api/v1/realtime/live-session", headers=headers(csrf), json={**payload, "voiceId": "willow"}).status_code == 422
    assert len(fake.requests) == 2


@pytest.mark.asyncio
async def test_saved_voice_is_forwarded_only_as_startup_output_voice():
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(201, json={"session": {"id": "live_test"}, "transport": {"type": "webrtc", "sdp": ANSWER}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        live = OpenAILiveClient(client)
        await live.create_session(api_key=SECRET, sdp=OFFER, voice_id="bossa")
        with pytest.raises(IntegrationError) as error:
            await live.create_session(api_key=SECRET, sdp=OFFER, voice_id="custom_invalid")
    assert len(requests) == 1
    assert requests[0]["session"]["audio"] == {"output": {"voice": "bossa"}}
    assert requests[0]["session"]["model"] == "gpt-live-1"
    assert requests[0]["transport"]["sdp"] == OFFER
    assert error.value.code == "OPENAI_LIVE_VOICE_UNAVAILABLE"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {}, {"session": {"id": SECRET}, "transport": {"type": "webrtc", "sdp": ANSWER}}, {"session": {"id": "live_test"}, "transport": {"type": "webrtc", "sdp": ANSWER + SECRET}}])
async def test_malformed_or_secret_bearing_provider_response_is_rejected(payload):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(201, json=payload))) as client:
        with pytest.raises(IntegrationError) as error:
            await OpenAILiveClient(client).create_session(api_key=SECRET, sdp=OFFER)
    assert error.value.code == "OPENAI_LIVE_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_provider_response_read_stops_at_wire_limit_and_network_failure_never_retries():
    class Stream(httpx.AsyncByteStream):
        chunks = 0
        async def __aiter__(self):
            for _ in range(100):
                self.chunks += 1
                yield b"x" * OPENAI_LIVE_MAX_RESPONSE_BYTES
    stream = Stream()
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(201, stream=stream))) as client:
        with pytest.raises(IntegrationError):
            await OpenAILiveClient(client).create_session(api_key=SECRET, sdp=OFFER)
    assert stream.chunks == 2
    calls = []
    def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout(SECRET)
    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(IntegrationError) as error:
            await OpenAILiveClient(client).create_session(api_key=SECRET, sdp=OFFER)
    assert len(calls) == 1
    assert error.value.code == "OPENAI_LIVE_CONNECTION_FAILED"
    assert SECRET not in str(error.value)
