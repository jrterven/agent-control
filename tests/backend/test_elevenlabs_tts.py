from __future__ import annotations

from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hermes_control_api.integrations import ElevenLabsSpeechClient, IntegrationError
from hermes_control_api.models import AuditEvent, IdempotencyOperation


class FakeSpeechClient:
    def __init__(self) -> None:
        self.api_keys: list[str] = []
        self.generated: list[tuple[str, str, str]] = []

    async def list_voices(self, api_key: str):
        self.api_keys.append(api_key)
        return [
            {"id": "voice_alpha", "name": "Aria", "category": "premade", "labels": {"accent": "American"}},
            {"id": "voice_beta", "name": "Brian", "category": "cloned", "labels": {}},
        ]

    async def issue_realtime_token(self, api_key: str) -> str:
        self.api_keys.append(api_key)
        return "sutkn_tts_single_use_123"

    async def open_audio_stream(self, api_key: str, *, voice_id: str, text: str):
        self.generated.append((api_key, voice_id, text))
        return object(), None

    async def audio_chunks(self, response, own_client):
        del response, own_client
        yield b"ID3"
        yield b"agent-audio"


def put_key(client: TestClient, csrf: str, value: str):
    return client.put(
        "/api/v1/integrations/elevenlabs/key",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
        json={"apiKey": value},
    )


def select_voice(client: TestClient, csrf: str, voice_id: str):
    return client.put(
        "/api/v1/integrations/elevenlabs/voice",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
        json={"voiceId": voice_id},
    )


def test_owner_selects_catalog_voice_and_bootstrap_enables_speech(authenticated, app):
    client, csrf = authenticated
    secret = "sk_tts_owner_123456789_private"
    fake = FakeSpeechClient()
    app.state.elevenlabs_speech_client = fake

    assert put_key(client, csrf, secret).status_code == 200
    catalog = client.get("/api/v1/integrations/elevenlabs/voices")
    assert catalog.status_code == 200, catalog.text
    assert [item["name"] for item in catalog.json()["items"]] == ["Aria", "Brian"]

    selected = select_voice(client, csrf, "voice_alpha")
    assert selected.status_code == 200, selected.text
    assert selected.json()["voiceId"] == "voice_alpha"
    assert selected.json()["voiceName"] == "Aria"
    assert selected.json()["speechAvailable"] is True
    assert secret not in selected.text

    bootstrap = client.get("/api/v1/bootstrap").json()
    assert bootstrap["features"]["speech"] == {
        "available": True,
        "provider": "elevenlabs",
        "modelId": "eleven_flash_v2_5",
        "voiceId": "voice_alpha",
        "voiceName": "Aria",
    }


def test_live_ticket_and_history_audio_use_same_write_only_key(authenticated, app):
    client, csrf = authenticated
    secret = "sk_tts_shared_123456789_private"
    fake = FakeSpeechClient()
    app.state.elevenlabs_speech_client = fake
    assert put_key(client, csrf, secret).status_code == 200
    assert select_voice(client, csrf, "voice_beta").status_code == 200

    ticket = client.post(
        "/api/v1/realtime/speech-token",
        headers={"X-CSRF-Token": csrf},
        json={"sessionId": "session-a"},
    )
    assert ticket.status_code == 200, ticket.text
    assert ticket.json() == {
        "token": "sutkn_tts_single_use_123",
        "expiresAt": ticket.json()["expiresAt"],
        "modelId": "eleven_flash_v2_5",
        "voiceId": "voice_beta",
        "voiceName": "Brian",
    }
    assert secret not in ticket.text

    fake_idempotency_key = uuid4().hex
    audio = client.post(
        "/api/v1/integrations/elevenlabs/speech",
        headers={"X-CSRF-Token": csrf, "Accept": "audio/mpeg", "Idempotency-Key": fake_idempotency_key},
        json={"text": "Read this response"},
    )
    assert audio.status_code == 200, audio.text
    assert audio.headers["content-type"].startswith("audio/mpeg")
    assert audio.content == b"ID3agent-audio"
    assert fake.generated == [(secret, "voice_beta", "Read this response")]
    with app.state.session_factory() as db:
        assert db.scalar(select(IdempotencyOperation).where(
            IdempotencyOperation.idempotency_key == fake_idempotency_key
        )) is None
        speech_audit = db.scalar(select(AuditEvent).where(
            AuditEvent.action == "integration.elevenlabs.speech.generate"
        ))
        assert speech_audit is not None
        assert "Read this response" not in repr(speech_audit.__dict__)


def test_speech_requires_selected_voice_and_csrf(authenticated, app):
    client, csrf = authenticated
    app.state.elevenlabs_speech_client = FakeSpeechClient()
    assert put_key(client, csrf, "sk_tts_missing_voice_123456789").status_code == 200

    missing_voice = client.post(
        "/api/v1/realtime/speech-token",
        headers={"X-CSRF-Token": csrf},
        json={},
    )
    assert missing_voice.status_code == 409
    assert missing_voice.json()["code"] == "SPEECH_VOICE_NOT_CONFIGURED"
    assert client.post(
        "/api/v1/integrations/elevenlabs/speech",
        json={"text": "No CSRF"},
    ).status_code == 403


def test_replacing_key_clears_voice_from_another_elevenlabs_workspace(authenticated, app):
    client, csrf = authenticated
    app.state.elevenlabs_speech_client = FakeSpeechClient()
    assert put_key(client, csrf, "sk_tts_first_workspace_123456").status_code == 200
    assert select_voice(client, csrf, "voice_alpha").json()["speechAvailable"] is True

    replaced = put_key(client, csrf, "sk_tts_second_workspace_12345")
    assert replaced.status_code == 200
    assert replaced.json()["speechAvailable"] is False
    assert replaced.json()["voiceId"] is None


@pytest.mark.asyncio
async def test_official_speech_client_uses_fixed_voice_and_single_use_token_contracts():
    secret = "sk_tts_contract_123456789_private"
    seen: list[tuple[str, str]] = []

    def provider(request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == secret
        seen.append((request.method, str(request.url)))
        if request.url.path.endswith("/v2/voices"):
            return httpx.Response(200, json={
                "voices": [{"voice_id": "voice_safe", "name": "Safe voice", "labels": {"accent": "neutral"}}],
                "has_more": False,
            }, request=request)
        if request.url.path.endswith("/single-use-token/tts_websocket"):
            return httpx.Response(200, json={"token": "single_use_tts_123"}, request=request)
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        speech = ElevenLabsSpeechClient(http_client)
        voices = await speech.list_voices(secret)
        token = await speech.issue_realtime_token(secret)

    assert voices == [{"id": "voice_safe", "name": "Safe voice", "category": None, "labels": {"accent": "neutral"}}]
    assert token == "single_use_tts_123"
    assert seen[0][1].startswith("https://api.elevenlabs.io/v2/voices?")
    assert seen[1][1] == "https://api.elevenlabs.io/v1/single-use-token/tts_websocket"


@pytest.mark.asyncio
async def test_official_speech_client_does_not_follow_provider_redirects():
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://attacker.invalid/steal"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as http_client:
        speech = ElevenLabsSpeechClient(http_client)
        with pytest.raises(IntegrationError) as caught:
            await speech.issue_realtime_token("sk_no_redirect_123456789")

    assert caught.value.code == "SPEECH_PROVIDER_CONFIGURATION_REJECTED"
