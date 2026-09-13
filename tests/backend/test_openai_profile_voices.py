from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from hermes_control_api.models import (
    AuditEvent, Gateway, OpenAIProfileVoicePreference, ProfileRef, User,
)
from hermes_control_api.security import hash_password

from .test_openai_live import OFFER, FakeLiveClient, configure, headers, profile_id


def path(pid: str) -> str:
    return f"/api/v1/integrations/openai/profiles/{pid}/voice"


def test_profile_voice_inherits_until_overridden_and_reset_uses_latest_default(authenticated, app):
    client, csrf = authenticated
    pid = profile_id(app)
    assert client.get(path(pid)).json() == {"profileId": pid, "voiceId": "marin", "inherited": True}
    assert client.put(path(pid), headers=headers(csrf), json={"voiceId": "cedar"}).json() == {"profileId": pid, "voiceId": "cedar", "inherited": False}
    assert client.put("/api/v1/integrations/openai/voice", headers=headers(csrf), json={"voiceId": "stone"}).status_code == 200
    assert client.get(path(pid)).json()["voiceId"] == "cedar"
    assert client.delete(path(pid), headers=headers(csrf)).json() == {"profileId": pid, "voiceId": "stone", "inherited": True}
    assert client.get("/api/v1/integrations/voice").json() == {"provider": "elevenlabs"}
    assert client.get("/api/v1/integrations/openai").json()["configured"] is False
    # Pinning the same voice as the current default still creates an override.
    assert client.put(path(pid), headers=headers(csrf), json={"voiceId": "stone"}).json()["inherited"] is False
    client.put("/api/v1/integrations/openai/voice", headers=headers(csrf), json={"voiceId": "marin"})
    assert client.get(path(pid)).json()["voiceId"] == "stone"


def test_profile_voice_is_owner_scoped_and_independent_of_keys_and_modes(authenticated, app):
    client, csrf = authenticated
    pid = profile_id(app)
    client.put(path(pid), headers=headers(csrf), json={"voiceId": "ash"})
    configure(client, csrf)
    client.put("/api/v1/integrations/openai/key", headers=headers(csrf), json={"apiKey": "sk-other_key_1234567890123"})
    client.delete("/api/v1/integrations/openai/key", headers=headers(csrf))
    assert client.get(path(pid)).json()["voiceId"] == "ash"
    with app.state.session_factory() as db:
        db.add(User(username="voice-reader", password_hash=hash_password("reader password long enough"), is_admin=False))
        db.commit()
    response = client.post("/api/v1/auth/login", json={"username": "voice-reader", "password": "reader password long enough"})
    reader_csrf = response.json()["csrfToken"]
    assert client.get(path(pid)).json() == {"profileId": pid, "voiceId": "marin", "inherited": True}
    assert client.put(path(pid), headers=headers(reader_csrf), json={"voiceId": "willow"}).status_code == 200
    assert client.delete(path(pid), headers=headers(reader_csrf)).json()["inherited"] is True
    with app.state.session_factory() as db:
        remaining = db.scalars(select(OpenAIProfileVoicePreference)).all()
        assert len(remaining) == 1
        assert remaining[0].openai_voice_id == "ash"


def test_profile_id_separates_same_named_agents_on_different_gateways(authenticated, app):
    client, csrf = authenticated
    pid = profile_id(app)
    with app.state.session_factory() as db:
        original = db.get(ProfileRef, pid)
        gateway = Gateway(name="Second gateway", rest_url="http://127.0.0.1:9119", ws_url="ws://127.0.0.1:9119/ws")
        db.add(gateway)
        db.flush()
        other = ProfileRef(gateway_id=gateway.id, profile_name=original.profile_name, display_name=original.display_name)
        db.add(other)
        db.commit()
        other_id = other.id
    assert client.put(path(pid), headers=headers(csrf), json={"voiceId": "cedar"}).status_code == 200
    assert client.get(path(other_id)).json()["inherited"] is True
    assert client.put(path(other_id), headers=headers(csrf), json={"voiceId": "coral"}).status_code == 200
    assert client.get(path(pid)).json()["voiceId"] == "cedar"
    assert client.get(path(other_id)).json()["voiceId"] == "coral"


def test_live_handshake_uses_selected_agent_voice_with_global_fallback(authenticated, app):
    client, csrf = authenticated
    configure(client, csrf)
    pid = profile_id(app)
    with app.state.session_factory() as db:
        other_id = db.scalar(select(ProfileRef).where(ProfileRef.profile_name == "control-dev")).id
    client.put("/api/v1/integrations/openai/voice", headers=headers(csrf), json={"voiceId": "stone"})
    client.put(path(pid), headers=headers(csrf), json={"voiceId": "cedar"})
    fake = FakeLiveClient()
    app.state.openai_live_client = fake
    for selected_id, expected in [(pid, "cedar"), (other_id, "stone")]:
        response = client.post("/api/v1/realtime/live-session", headers=headers(csrf), json={"sdp": OFFER, "profileId": selected_id})
        assert response.status_code == 201, response.text
        assert fake.requests[-1]["voice_id"] == expected
    client.delete(path(pid), headers=headers(csrf))
    assert client.post("/api/v1/realtime/live-session", headers=headers(csrf), json={"sdp": OFFER, "profileId": pid}).status_code == 201
    assert fake.requests[-1]["voice_id"] == "stone"


def test_profile_voice_auth_validation_missing_profiles_and_idempotency(authenticated, app):
    client, csrf = authenticated
    pid = profile_id(app)
    for method in ("put", "delete"):
        kwargs = {"json": {"voiceId": "ash"}} if method == "put" else {}
        assert getattr(client, method)(path(pid), **kwargs).status_code == 403
        assert getattr(client, method)(path(pid), headers={"X-CSRF-Token": csrf}, **kwargs).status_code == 400
        assert getattr(client, method)(path(str(uuid4())), headers=headers(csrf), **kwargs).status_code == 404
    for payload in ({"voiceId": "unknown"}, {"voiceId": "marin", "ownerId": "other"}, {"voiceId": None}):
        assert client.put(path(pid), headers=headers(csrf), json=payload).status_code == 422
    replay_headers = headers(csrf)
    for _ in range(2):
        assert client.put(path(pid), headers=replay_headers, json={"voiceId": "ash"}).status_code == 200
    with app.state.session_factory() as db:
        assert len(db.scalars(select(AuditEvent).where(AuditEvent.action == "integration.openai.profile-voice.set")).all()) == 1
    client.cookies.clear()
    assert client.get(path(pid)).status_code == 401
    assert client.put(path(pid), headers=headers(csrf), json={"voiceId": "ash"}).status_code == 401
    assert client.delete(path(pid), headers=headers(csrf)).status_code == 401


def test_profile_voice_foreign_keys_cascade_and_catalog_check(app, authenticated):
    with app.state.session_factory() as db:
        gateway = db.scalar(select(Gateway))
        owner = User(username="voice-cascade", password_hash="unused")
        profile = ProfileRef(gateway_id=gateway.id, profile_name="voice-cascade", display_name="Voice cascade")
        db.add_all([owner, profile])
        db.commit()
        owner_id, pid = owner.id, profile.id
        db.add(OpenAIProfileVoicePreference(owner_id=owner_id, profile_id=pid, openai_voice_id="unknown"))
        with pytest.raises(IntegrityError):
            db.commit()
        db.rollback()
        db.add(OpenAIProfileVoicePreference(owner_id=owner_id, profile_id=pid, openai_voice_id="stone"))
        db.commit()
        db.delete(db.get(ProfileRef, pid))
        db.commit()
        assert db.scalar(select(OpenAIProfileVoicePreference)) is None
        other = db.scalar(select(ProfileRef))
        db.add(OpenAIProfileVoicePreference(owner_id=owner_id, profile_id=other.id, openai_voice_id="ash"))
        db.commit()
        db.delete(db.get(User, owner_id))
        db.commit()
        assert db.scalar(select(OpenAIProfileVoicePreference)) is None
