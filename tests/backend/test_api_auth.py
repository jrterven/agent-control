from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from hermes_control_api.models import ProfileRef

from .conftest import mutation_headers


def test_login_cookie_csrf_and_write_only_gateway_metadata(client: TestClient):
    assert client.get("/api/v1/auth/me").status_code == 401
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200
    assert "HttpOnly" in login.headers["set-cookie"]
    assert login.json()["id"] == login.json()["userId"]
    assert login.json()["name"] == login.json()["username"] == "admin"
    csrf = login.json()["csrfToken"]

    rejected = client.post(
        "/api/v1/workspaces",
        headers={"Idempotency-Key": "workspace-no-csrf"},
        json={"name": "Should fail"},
    )
    assert rejected.status_code == 403

    created = client.post(
        "/api/v1/workspaces",
        headers=mutation_headers(csrf, "workspace-one"),
        json={"name": "Research", "color": "#7C5CFC"},
    )
    assert created.status_code == 201, created.text

    gateways = client.get("/api/v1/gateways")
    assert gateways.status_code == 200
    body = gateways.text.lower()
    assert "resturl" not in body
    assert "wsurl" not in body
    assert "http://127.0.0.1:19119" not in body
    assert "replace_server_side_only" not in body


def test_mobile_bootstrap_and_profile_id_session_alias(authenticated):
    client, csrf = authenticated
    bootstrap = client.get("/api/v1/bootstrap")
    assert bootstrap.status_code == 200
    body = bootstrap.json()
    control_dev_profile = next(
        profile
        for profile in body["profiles"]
        if profile["technicalName"] == "control-dev"
    )
    created = client.post(
        "/api/v1/sessions",
        headers=mutation_headers(csrf, "profile-id-session"),
        json={"profileId": control_dev_profile["id"], "title": "Compat"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["profileId"] == control_dev_profile["id"]


def test_csrf_is_stable_across_tabs_and_generic_mutations_are_idempotent(authenticated):
    client, csrf = authenticated
    first_tab = client.get("/api/v1/auth/me")
    second_tab = client.get("/api/v1/auth/csrf")
    assert first_tab.status_code == second_tab.status_code == 200
    assert first_tab.json()["csrfToken"] == second_tab.json()["csrfToken"] == csrf

    headers = mutation_headers(csrf, "workspace-replay")
    first = client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Idempotent workspace", "color": "#14B8A6"},
    )
    replay = client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Idempotent workspace", "color": "#14B8A6"},
    )
    assert first.status_code == replay.status_code == 201
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json() == first.json()

    conflict = client.post(
        "/api/v1/workspaces",
        headers=headers,
        json={"name": "Different payload", "color": "#14B8A6"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_CONFLICT"

    workspaces = client.get("/api/v1/workspaces").json()
    assert sum(item["name"] == "Idempotent workspace" for item in workspaces) == 1


def test_ticket_is_one_use(authenticated):
    client, csrf = authenticated
    issued = client.post(
        "/api/v1/realtime/tickets",
        headers=mutation_headers(csrf, "ticket-one"),
    )
    assert issued.status_code == 201, issued.text
    ticket = issued.json()["ticket"]
    with client.websocket_connect(f"/api/v1/realtime?ticket={ticket}") as websocket:
        websocket.send_json({"type": "ping", "at": 123})
        assert websocket.receive_json() == {"type": "control.pong", "at": 123}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/api/v1/realtime?ticket={ticket}"):
            pass


def test_realtime_rejects_oversized_or_non_primitive_ping_frames(authenticated):
    client, csrf = authenticated

    def ticket(key: str) -> str:
        response = client.post(
            "/api/v1/realtime/tickets",
            headers=mutation_headers(csrf, key),
        )
        assert response.status_code == 201
        return response.json()["ticket"]

    with client.websocket_connect(f"/api/v1/realtime?ticket={ticket('ticket-large')}") as websocket:
        websocket.send_text('{"type":"ping","at":1,"padding":"' + ("x" * 5_000) + '"}')
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()
        assert closed.value.code == 4409

    with client.websocket_connect(f"/api/v1/realtime?ticket={ticket('ticket-nested')}") as websocket:
        websocket.send_json({"type": "ping", "at": {"nested": ["not echoed"]}})
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()
        assert closed.value.code == 4400


def test_gateway_connection_changes_invalidate_all_profile_capabilities(authenticated, app):
    client, csrf = authenticated
    created = client.post(
        "/api/v1/gateways",
        headers=mutation_headers(csrf, "gateway-capability-create"),
        json={
            "name": "Mutable tunnel",
            "restUrl": "http://127.0.0.1:29119",
            "wsUrl": "ws://127.0.0.1:29119/api/ws",
            "apiUrl": "http://127.0.0.1:28642",
            "connectionMode": "tunnel",
            "dashboardToken": "old-dashboard-token",
            "apiKey": "old-api-key",
        },
    )
    assert created.status_code == 201, created.text
    gateway_id = created.json()["id"]

    with app.state.session_factory() as db:
        for profile_name in ("default", "jarvis", "control-dev"):
            db.add(
                ProfileRef(
                    gateway_id=gateway_id,
                    profile_name=profile_name,
                    display_name=profile_name,
                    status="online",
                    capabilities={"methods": ["cron.trigger", "prompt.submit"]},
                    last_seen_at=datetime.now(timezone.utc),
                )
            )
        db.commit()

    updated = client.patch(
        f"/api/v1/gateways/{gateway_id}",
        headers=mutation_headers(csrf, "gateway-endpoint-change"),
        json={
            "restUrl": "http://127.0.0.1:39119",
            "wsUrl": "ws://127.0.0.1:39119/api/ws",
            "dashboardToken": "new-dashboard-token",
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["healthStatus"] == "unknown"

    with app.state.session_factory() as db:
        profiles = db.scalars(
            select(ProfileRef).where(ProfileRef.gateway_id == gateway_id)
        ).all()
        assert len(profiles) == 3
        assert all(profile.capabilities == {} for profile in profiles)
        assert all(profile.last_seen_at is None for profile in profiles)
        assert all(profile.status == "unknown" for profile in profiles)


def test_upstream_transport_failure_is_retryable_503(authenticated, monkeypatch):
    client, _ = authenticated
    gateway = client.get("/api/v1/gateways").json()[0]
    monkeypatch.setattr(
        client.app.state.services.provider_pool,
        "get",
        AsyncMock(side_effect=ConnectionError("tunnel down")),
    )
    response = client.get(
        "/api/v1/diagnostics/capabilities",
        params={"gatewayId": gateway["id"], "profileName": "default"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "HERMES_UNAVAILABLE"
    assert response.json()["retryable"] is True


def test_liveness_and_database_readiness_are_distinct(client, app):
    live = client.get("/api/v1/health")
    ready = client.get("/api/v1/ready")
    assert live.status_code == 200
    assert live.json()["status"] == "ok"
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["database"] == "ready"
    assert "restUrl" not in ready.text
    assert "token" not in ready.text.lower()

    original_factory = app.state.session_factory

    class BrokenDatabase:
        def __enter__(self):
            raise OSError("simulated sqlite failure")

        def __exit__(self, *_args):
            return False

    app.state.session_factory = lambda: BrokenDatabase()
    try:
        unavailable = client.get("/api/v1/ready")
    finally:
        app.state.session_factory = original_factory
    assert unavailable.status_code == 503
    assert unavailable.json() == {
        "status": "not_ready",
        "database": "unavailable",
        "upstream": "unknown",
        "time": unavailable.json()["time"],
    }
