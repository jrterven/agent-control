from __future__ import annotations

import asyncio
import base64
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from hermes_client import NormalizedEvent
from hermes_control_api.models import PushSubscription, SessionLink
from hermes_control_api.notifications import (
    decrypted_subscription,
    derive_vapid_key_pair,
)
from hermes_control_api.realtime import persist_normalized_event
from pywebpush import webpush

from .conftest import mutation_headers
from .test_api_sessions import create_session


def _subscription_payload(endpoint: str = "https://fcm.googleapis.com/fcm/send/agent-control-test") -> dict:
    return {
        "endpoint": endpoint,
        "keys": {
            "p256dh": "B" * 87,
            "auth": "A" * 22,
        },
        "locale": "es",
    }


def test_vapid_key_is_stable_and_config_is_public(
    authenticated, app, tmp_path, monkeypatch
):
    client, _ = authenticated
    private_one, public_one = derive_vapid_key_pair(app.state.services.vault.key)
    private_two, public_two = derive_vapid_key_pair(app.state.services.vault.key)
    assert (private_one, public_one) == (private_two, public_two)
    assert len(base64.urlsafe_b64decode(private_one + "==")) == 32
    assert len(base64.urlsafe_b64decode(public_one + "==")) == 65

    client_key = ec.generate_private_key(ec.SECP256R1())
    client_public = client_key.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    monkeypatch.chdir(tmp_path)
    curl = webpush(
        subscription_info={
            "endpoint": "https://fcm.googleapis.com/fcm/send/cryptographic-contract",
            "keys": {
                "p256dh": base64.urlsafe_b64encode(client_public).decode().rstrip("="),
                "auth": base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("="),
            },
        },
        data="{\"title\":\"Task completed\"}",
        vapid_private_key=private_one,
        vapid_claims={"sub": "mailto:agent-control@localhost.invalid"},
        curl=True,
        ttl=3_600,
    )
    assert "authorization:" in curl.lower()
    assert "content-encoding: aes128gcm" in curl.lower()

    response = client.get("/api/v1/notifications/config")
    assert response.status_code == 200
    assert response.json() == {
        "available": True,
        "applicationServerKey": public_one,
    }


def test_push_subscription_is_owner_scoped_encrypted_and_removable(authenticated, app):
    client, csrf = authenticated
    payload = _subscription_payload()
    created = client.post(
        "/api/v1/notifications/subscriptions",
        headers=mutation_headers(csrf, "subscribe-device"),
        json=payload,
    )
    assert created.status_code == 201, created.text
    assert created.json()["enabled"] is True

    with app.state.session_factory() as db:
        row = db.get(PushSubscription, created.json()["id"])
        assert row is not None
        assert payload["endpoint"] not in row.subscription_ciphertext
        assert decrypted_subscription(app.state.services.vault, row) == payload

    rejected = client.post(
        "/api/v1/notifications/subscriptions",
        headers=mutation_headers(csrf, "reject-localhost"),
        json=_subscription_payload("https://localhost/private-push-endpoint"),
    )
    assert rejected.status_code == 422

    removed = client.request(
        "DELETE",
        "/api/v1/notifications/subscriptions/current",
        headers=mutation_headers(csrf, "unsubscribe-device"),
        json={"endpoint": payload["endpoint"]},
    )
    assert removed.status_code == 204
    with app.state.session_factory() as db:
        assert db.get(PushSubscription, created.json()["id"]) is None


def test_terminal_message_marks_chat_unread_once_and_read_endpoint_clears_it(
    authenticated, app
):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "notification-session")
    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        stored_session_id = row.stored_session_id
        runtime_session_id = row.runtime_session_id
        gateway_id = row.gateway_id

    event = NormalizedEvent.create(
        type="message.complete",
        gateway_id=gateway_id,
        profile_name="control-dev",
        stored_session_id=stored_session_id,
        runtime_session_id=runtime_session_id,
        runtime_generation="notification-generation",
        sequence=12,
        data={"status": "completed"},
    )
    completion = persist_normalized_event(app.state.session_factory, event)
    assert completion is not None
    assert completion.session_id == session["id"]
    assert completion.status == "completed"

    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        assert row.unread is True
        activity_at = row.last_activity_at

    assert persist_normalized_event(app.state.session_factory, event) is None
    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
        assert row.last_activity_at == activity_at

    cleared = client.post(
        f"/api/v1/sessions/{session['id']}/read",
        headers=mutation_headers(csrf, "read-notification-session"),
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["unread"] is False
    assert cleared.json()["updatedAt"] == activity_at.isoformat()


def test_push_service_sends_bounded_localized_completion(authenticated, app, monkeypatch):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "push-delivery-session")
    created = client.post(
        "/api/v1/notifications/subscriptions",
        headers=mutation_headers(csrf, "subscribe-delivery-device"),
        json=_subscription_payload(),
    )
    assert created.status_code == 201, created.text
    with app.state.session_factory() as db:
        row = db.get(SessionLink, session["id"])
    event = NormalizedEvent.create(
        type="message.complete",
        gateway_id=row.gateway_id,
        profile_name=row.profile_name,
        stored_session_id=row.stored_session_id,
        runtime_session_id=row.runtime_session_id,
        runtime_generation="push-generation",
        sequence=9,
        data={"status": "completed"},
    )
    completion = persist_normalized_event(app.state.session_factory, event)
    calls: list[dict] = []

    def fake_webpush(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("hermes_control_api.notifications.webpush", fake_webpush)
    asyncio.run(app.state.push_notification_service.send_completion(completion))
    assert len(calls) == 1
    assert "Tarea terminada" in calls[0]["data"]
    assert calls[0]["ttl"] == 3_600
    assert calls[0]["subscription_info"]["endpoint"].startswith("https://fcm.googleapis.com/")
