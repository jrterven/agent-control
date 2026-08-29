from __future__ import annotations

import json
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
        self.generated: list[tuple[str, str, str, str]] = []
        self.previewed: list[str] = []

    async def list_voices(self, api_key: str):
        self.api_keys.append(api_key)
        return [
            {"id": "voice_alpha", "name": "Aria", "category": "premade", "labels": {"accent": "American"}, "preview_available": True, "preview_url": "https://storage.googleapis.com/eleven-public-prod/aria.mp3"},
            {"id": "voice_beta", "name": "Brian", "category": "cloned", "labels": {}, "preview_available": False, "preview_url": None},
        ]

    async def issue_realtime_token(self, api_key: str) -> str:
        self.api_keys.append(api_key)
        return "sutkn_tts_single_use_123"

    async def open_audio_stream(
        self, api_key: str, *, voice_id: str, model_id: str, text: str
    ):
        self.generated.append((api_key, voice_id, model_id, text))
        return object(), None

    async def open_preview_stream(self, preview_url: str):
        self.previewed.append(preview_url)
        return object(), None

    async def audio_chunks(self, response, own_client, maximum=None):
        del response, own_client, maximum
        yield b"ID3"
        yield b"agent-audio"


def put_key(client: TestClient, csrf: str, value: str):
    return client.put(
        "/api/v1/integrations/elevenlabs/key",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
        json={"apiKey": value},
    )


def select_voice(
    client: TestClient,
    csrf: str,
    voice_id: str,
    tts_model_id: str = "eleven_flash_v2_5",
):
    return client.put(
        "/api/v1/integrations/elevenlabs/voice",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
        json={"voiceId": voice_id, "ttsModelId": tts_model_id},
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
    assert catalog.json()["items"][0]["previewAvailable"] is True
    assert "previewUrl" not in catalog.text

    preview = client.get("/api/v1/integrations/elevenlabs/voice-preview/voice_alpha")
    assert preview.status_code == 200, preview.text
    assert preview.headers["content-type"].startswith("audio/mpeg")
    assert preview.content == b"ID3agent-audio"
    assert fake.previewed == ["https://storage.googleapis.com/eleven-public-prod/aria.mp3"]
    unavailable = client.get("/api/v1/integrations/elevenlabs/voice-preview/voice_beta")
    assert unavailable.status_code == 422
    assert unavailable.json()["code"] == "SPEECH_VOICE_PREVIEW_UNAVAILABLE"

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


def test_live_ticket_and_history_audio_use_same_write_only_key_and_model(
    authenticated, app
):
    client, csrf = authenticated
    secret = "sk_tts_shared_123456789_private"
    fake = FakeSpeechClient()
    app.state.elevenlabs_speech_client = fake
    assert put_key(client, csrf, secret).status_code == 200
    selected = select_voice(
        client, csrf, "voice_beta", "eleven_multilingual_v2"
    )
    assert selected.status_code == 200
    assert selected.json()["ttsModelId"] == "eleven_multilingual_v2"

    ticket = client.post(
        "/api/v1/realtime/speech-token",
        headers={"X-CSRF-Token": csrf},
        json={"sessionId": "session-a"},
    )
    assert ticket.status_code == 200, ticket.text
    assert ticket.json() == {
        "token": "sutkn_tts_single_use_123",
        "expiresAt": ticket.json()["expiresAt"],
        "modelId": "eleven_multilingual_v2",
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
    assert fake.generated == [
        (secret, "voice_beta", "eleven_multilingual_v2", "Read this response")
    ]
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


def test_voice_settings_reject_every_model_outside_the_explicit_allowlist(
    authenticated, app
):
    client, csrf = authenticated
    app.state.elevenlabs_speech_client = FakeSpeechClient()
    assert put_key(client, csrf, "sk_tts_model_allowlist_123456789").status_code == 200

    rejected = select_voice(client, csrf, "voice_alpha", "eleven_v3")

    assert rejected.status_code == 422
    assert "eleven_v3" not in client.get("/api/v1/integrations/elevenlabs").text


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
        if request.url.host == "storage.googleapis.com":
            assert "xi-api-key" not in request.headers
            seen.append((request.method, str(request.url)))
            return httpx.Response(200, content=b"ID3preview", headers={"Content-Type": "audio/mpeg"}, request=request)
        assert request.headers["xi-api-key"] == secret
        seen.append((request.method, str(request.url)))
        if request.url.path.endswith("/v2/voices"):
            return httpx.Response(200, json={
                "voices": [{"voice_id": "voice_safe", "name": "Safe voice", "labels": {"accent": "neutral"}, "preview_url": "https://storage.googleapis.com/eleven-public-prod/safe.mp3"}],
                "has_more": False,
            }, request=request)
        if request.url.path.endswith("/single-use-token/tts_websocket"):
            return httpx.Response(200, json={"token": "single_use_tts_123"}, request=request)
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        speech = ElevenLabsSpeechClient(http_client)
        voices = await speech.list_voices(secret)
        token = await speech.issue_realtime_token(secret)
        preview_response, preview_client = await speech.open_preview_stream(str(voices[0]["preview_url"]))
        preview = b"".join([chunk async for chunk in speech.audio_chunks(preview_response, preview_client, 1024)])

    assert voices == [{"id": "voice_safe", "name": "Safe voice", "category": None, "labels": {"accent": "neutral"}, "preview_available": True, "preview_url": "https://storage.googleapis.com/eleven-public-prod/safe.mp3"}]
    assert token == "single_use_tts_123"
    assert preview == b"ID3preview"
    assert seen[0][1].startswith("https://api.elevenlabs.io/v2/voices?")
    assert seen[1][1] == "https://api.elevenlabs.io/v1/single-use-token/tts_websocket"


@pytest.mark.parametrize(
    "model_id", ["eleven_flash_v2_5", "eleven_multilingual_v2"]
)
@pytest.mark.asyncio
async def test_official_speech_client_forwards_only_the_selected_tts_model(model_id):
    secret = "sk_tts_model_contract_123456789_private"
    observed: dict[str, object] = {}

    def provider(request: httpx.Request) -> httpx.Response:
        observed.update(json.loads(request.content))
        return httpx.Response(
            200,
            content=b"ID3selected-model",
            headers={"Content-Type": "audio/mpeg"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        speech = ElevenLabsSpeechClient(http_client)
        response, own_client = await speech.open_audio_stream(
            secret,
            voice_id="voice_safe",
            model_id=model_id,
            text="A bounded test response",
        )
        body = b"".join(
            [chunk async for chunk in speech.audio_chunks(response, own_client)]
        )

    assert observed == {
        "text": "A bounded test response",
        "model_id": model_id,
    }
    assert body == b"ID3selected-model"


@pytest.mark.asyncio
async def test_voice_preview_accepts_legacy_gcs_text_plain_mp3_metadata():
    requested = False

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(
            200,
            content=b"\xff\xfbP\xc4preview",
            headers={"Content-Type": "text/plain"},
            request=request,
        )

    preview_url = (
        "https://storage.googleapis.com/eleven-public-prod/premade/voices/"
        "pNInz6obpgDQGcFmaJgB/sample.mp3"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        speech = ElevenLabsSpeechClient(http_client)
        response, own_client = await speech.open_preview_stream(preview_url)
        body = b"".join(
            [chunk async for chunk in speech.audio_chunks(response, own_client, 1024)]
        )

    assert requested is True
    assert body.startswith(b"\xff\xfb")


@pytest.mark.asyncio
async def test_voice_preview_rejects_non_mp3_path_before_network():
    requested = False

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, content=b"not audio", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        speech = ElevenLabsSpeechClient(http_client)
        with pytest.raises(IntegrationError) as caught:
            await speech.open_preview_stream(
                "https://storage.googleapis.com/eleven-public-prod/not-audio.txt"
            )

    assert caught.value.code == "SPEECH_VOICE_PREVIEW_UNAVAILABLE"
    assert requested is False


@pytest.mark.asyncio
async def test_official_speech_client_does_not_follow_provider_redirects():
    def redirect(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://attacker.invalid/steal"}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as http_client:
        speech = ElevenLabsSpeechClient(http_client)
        with pytest.raises(IntegrationError) as caught:
            await speech.issue_realtime_token("sk_no_redirect_123456789")

    assert caught.value.code == "SPEECH_PROVIDER_CONFIGURATION_REJECTED"


@pytest.mark.asyncio
async def test_voice_preview_rejects_non_elevenlabs_storage_before_network():
    requested = False

    def provider(request: httpx.Request) -> httpx.Response:
        nonlocal requested
        requested = True
        return httpx.Response(200, content=b"unexpected", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http_client:
        speech = ElevenLabsSpeechClient(http_client)
        with pytest.raises(IntegrationError) as caught:
            await speech.open_preview_stream("https://attacker.invalid/private.mp3")

    assert caught.value.code == "SPEECH_VOICE_PREVIEW_UNAVAILABLE"
    assert requested is False
