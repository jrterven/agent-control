from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from hermes_control_api.models import AuditEvent, IdempotencyOperation, UserVoicePreference
from hermes_control_api.openai_live import LiveSessionLimiter, OpenAILiveClient
from hermes_control_api.openai_voices import OPENAI_LIVE_PREVIEW_PHRASES


SECRET = "sk-proj-private_voice_sample_123456789"
OFFER = "v=0\r\ns=private-offer\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
ANSWER = "v=0\r\ns=private-answer\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
PREVIEW_PATH = "/api/v1/realtime/live-voice-preview"


def save_key(client, csrf: str) -> None:
    response = client.put(
        "/api/v1/integrations/openai/key",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
        json={"apiKey": SECRET},
    )
    assert response.status_code == 200


class FakePreviewClient:
    def __init__(self):
        self.requests = []

    async def create_voice_preview(self, **kwargs):
        self.requests.append(kwargs)
        return {
            "session": {"id": f"preview_{len(self.requests)}"},
            "transport": {"type": "webrtc", "sdp": ANSWER},
        }


def test_preview_needs_no_profile_or_mode_and_changes_no_voice_preference(authenticated, app):
    client, csrf = authenticated
    save_key(client, csrf)
    fake = FakePreviewClient()
    app.state.openai_live_client = fake
    replay_key = uuid4().hex
    for sequence in (1, 2):
        response = client.post(
            PREVIEW_PATH + "/",
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": replay_key},
            json={"sdp": OFFER, "voiceId": "bossa", "language": "pt"},
        )
        assert response.status_code == 201
        assert response.json()["session"]["id"] == f"preview_{sequence}"
        assert response.headers["cache-control"] == "no-store"
        assert SECRET not in response.text
    assert fake.requests[0] == {
        "api_key": SECRET, "sdp": OFFER, "voice_id": "bossa", "language": "pt",
    }
    assert client.get("/api/v1/integrations/voice").json() == {"provider": "elevenlabs"}
    assert client.get("/api/v1/integrations/openai/voice").json() == {"voiceId": "marin"}
    with app.state.session_factory() as db:
        assert db.scalar(select(UserVoicePreference)) is None
        assert db.scalar(select(IdempotencyOperation).where(
            IdempotencyOperation.idempotency_key == replay_key
        )) is None
        audits = db.scalars(select(AuditEvent).where(
            AuditEvent.action == "integration.openai.voice.preview"
        )).all()
        assert len(audits) == 2
        persisted = repr([row.__dict__ for row in audits])
        assert all(value not in persisted for value in (SECRET, OFFER, ANSWER, "preview_1"))


def test_preview_requires_owner_key_csrf_valid_payload_and_shared_rate_limit(authenticated, app):
    client, csrf = authenticated
    payload = {"sdp": OFFER, "voiceId": "quartz", "language": "es"}
    request_headers = {"X-CSRF-Token": csrf}
    fake = FakePreviewClient()
    app.state.openai_live_client = fake
    assert client.post(PREVIEW_PATH, json=payload).status_code == 403
    response = client.post(PREVIEW_PATH, headers=request_headers, json=payload)
    assert response.status_code == 409
    assert response.json()["code"] == "OPENAI_NOT_CONFIGURED"
    save_key(client, csrf)
    for changes in [
        {"voiceId": "unknown"}, {"language": "it"}, {"sdp": ""},
        {"sdp": OFFER + "a" * 65_536}, {"instructions": "do something else"},
        {"sessionId": "other-owner"}, {"profileId": "other-profile"},
        {"apiKey": "override"}, {"history": []},
    ]:
        response = client.post(PREVIEW_PATH, headers=request_headers, json={**payload, **changes})
        assert response.status_code == 422
    assert fake.requests == []
    app.state.live_session_limiter = LiveSessionLimiter(limit=1, window_seconds=60)
    assert client.post(PREVIEW_PATH, headers=request_headers, json=payload).status_code == 201
    response = client.post(PREVIEW_PATH, headers=request_headers, json=payload)
    assert response.status_code == 429
    assert response.json()["code"] == "LIVE_SESSION_RATE_LIMITED"
    assert len(fake.requests) == 1
    client.cookies.clear()
    assert client.post(PREVIEW_PATH, headers=request_headers, json=payload).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["es", "en", "fr", "de", "pt"])
async def test_preview_sends_fixed_localized_sample_and_no_history_or_tools(language):
    captured = []
    def handler(request):
        captured.append(json.loads(request.content))
        return httpx.Response(201, json={
            "session": {"id": "preview"},
            "transport": {"type": "webrtc", "sdp": ANSWER},
        })
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await OpenAILiveClient(client).create_voice_preview(
            api_key=SECRET, sdp=OFFER, voice_id="vesper", language=language,
        )
    session = captured[0]["session"]
    assert session["audio"] == {"output": {"voice": "vesper"}}
    assert session["model"] == "gpt-live-1"
    assert session["delegation"] == {"type": "client"}
    assert "tools" not in session
    assert session["input"] == []
    assert session["store"] is False
    assert OPENAI_LIVE_PREVIEW_PHRASES[language] in session["instructions"]
    assert "Then remain silent" in session["instructions"]
    assert "delegate work, or execute tasks" in session["instructions"]
    assert SECRET not in json.dumps(captured)
    assert session["client"]["data_channel"]["allowed_client_events"] == [
        "session.commentary.append", "session.close",
    ]
