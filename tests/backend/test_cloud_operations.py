from __future__ import annotations

import base64
import pytest
from fastapi.testclient import TestClient

from hermes_control_api.auth import issue_session
from hermes_control_api.config import Settings
from hermes_control_api.main import create_app
from hermes_control_api.models import User
from hermes_control_api.security import hash_password


@pytest.fixture
def cloud_operator():
    app = create_app(Settings(environment="test", deployment_mode="cloud", public_base_url="https://control.test",
        database_url="sqlite://", vault_key_b64=base64.urlsafe_b64encode(b"o" * 32).decode(),
        create_schema_on_start=True, allowed_origins=["http://testserver"]))
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            admin = User(username="operator", password_hash=hash_password("operator-test-password"), is_admin=True)
            db.add(admin)
            db.commit()
            token, csrf, _ = issue_session(db, admin, ttl_hours=1)
        client.cookies.set("hc_session", token)
        yield app, client, csrf


def test_drain_blocks_new_mutations_and_resume_restores_service(cloud_operator):
    app, client, csrf = cloud_operator
    response = client.post("/api/v1/platform/drain", headers={"X-CSRF-Token": csrf, "Idempotency-Key": "drain"})
    assert response.status_code == 200, response.text
    assert response.json() == {"safeToRestart": True, "draining": True}
    blocked = client.post("/api/v1/sessions", json={}, headers={"X-CSRF-Token": csrf, "Idempotency-Key": "prompt"})
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "RELEASE_MAINTENANCE"
    assert client.get("/api/v1/ready").status_code == 200
    response = client.post("/api/v1/platform/resume", headers={"X-CSRF-Token": csrf, "Idempotency-Key": "resume"})
    assert response.status_code == 200
    assert not app.state.cloud_draining


@pytest.mark.asyncio
@pytest.mark.parametrize("counts", [(1, 0), (0, 1), (None, None)])
async def test_drain_refuses_children_even_when_foreground_sessions_are_idle(cloud_operator, monkeypatch, counts):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi import HTTPException
    from hermes_control_api.cloud_operations import drain
    from hermes_control_api.models import Gateway
    from hermes_control_api.connector_models import Connector
    from sqlalchemy import select
    app, _, _ = cloud_operator
    with app.state.session_factory() as db:
        owner = db.scalar(select(User))
        gateway = Gateway(name="worker-device", owner_id=owner.id, transport_kind="connector",
            rest_url="connector://test", ws_url="connector://test")
        db.add(gateway)
        db.flush()
        db.add(Connector(owner_id=owner.id, gateway_id=gateway.id, name="device", token_hash="e" * 64, profiles=["default"]))
        db.commit()
        gateway_id = gateway.id
    link = SimpleNamespace(online=True, pending={}, background_task_profiles={"default"})
    app.state.connector_registry.links[gateway_id] = link
    provider = SimpleNamespace(session_inventory_complete=True, list_sessions=AsyncMock(return_value=[]),
        list_background_tasks=AsyncMock(return_value={"complete": True, "activeCount": counts[0], "pendingDeliveryCount": counts[1]}))
    monkeypatch.setattr(app.state.services.provider_pool, "get", AsyncMock(return_value=provider))
    try:
        with pytest.raises(HTTPException) as error:
            await drain(SimpleNamespace(app=app))
        assert error.value.status_code == 409
        assert not app.state.cloud_draining
        provider.list_background_tasks.assert_awaited_once_with()
    finally:
        app.state.connector_registry.links.pop(gateway_id)


def test_platform_status_requires_admin_and_never_records_literal_paths(cloud_operator):
    app, client, _ = cloud_operator
    client.get("/api/v1/sessions/private-test-id/messages?secret=should-never-appear")
    response = client.get("/api/v1/platform/status")
    assert response.status_code == 200
    assert "private-test-id" not in response.text and "should-never-appear" not in response.text
    with app.state.session_factory() as db:
        user = User(username="beta-user", password_hash=hash_password("user-test-password"), is_admin=False)
        db.add(user)
        db.commit()
        token, _, _ = issue_session(db, user, ttl_hours=1)
    client.cookies.set("hc_session", token)
    assert client.get("/api/v1/platform/status").status_code == 403


def test_inflight_work_refuses_drain_without_leaving_service_paused(cloud_operator):
    from types import SimpleNamespace
    app, client, csrf = cloud_operator
    app.state.connector_registry.links["test-gateway"] = SimpleNamespace(pending={"active": object()}, online=True)
    try:
        response = client.post("/api/v1/platform/drain", headers={"X-CSRF-Token": csrf, "Idempotency-Key": "drain-active"})
        assert response.status_code == 409
        assert not app.state.cloud_draining
    finally:
        app.state.connector_registry.links.clear()


@pytest.mark.asyncio
async def test_drain_refuses_mutation_accepted_before_registry_dispatch(cloud_operator):
    from types import SimpleNamespace
    from hermes_control_api.cloud_operations import CloudOperationsMiddleware, CloudMetrics, drain
    from fastapi import HTTPException
    import asyncio
    app, _, _ = cloud_operator
    entered, finish = asyncio.Event(), asyncio.Event()
    async def slow_mutation(scope, receive, send):
        entered.set()
        await finish.wait()
        await send({"type":"http.response.start","status":200,"headers":[]})
        await send({"type":"http.response.body","body":b"{}"})
    middleware = CloudOperationsMiddleware(slow_mutation, app.state, CloudMetrics())
    async def send(message):
        pass
    operation = asyncio.create_task(middleware({"type":"http","method":"POST","path":"/api/v1/sessions"}, None, send))
    await entered.wait()
    try:
        with pytest.raises(HTTPException) as error:
            await drain(SimpleNamespace(app=app))
        assert error.value.status_code == 409
        assert not app.state.cloud_draining
    finally:
        finish.set()
        await operation
    assert app.state.cloud_mutations_inflight == 0


@pytest.mark.asyncio
async def test_resume_during_async_inventory_cancels_restart_permission(cloud_operator, monkeypatch):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from hermes_control_api.cloud_operations import drain, resume
    from hermes_control_api.models import Gateway
    from hermes_control_api.connector_models import Connector
    from sqlalchemy import select
    app, _, _ = cloud_operator
    request = SimpleNamespace(app=app)
    with app.state.session_factory() as db:
        admin = db.scalar(select(User))
        gateway = Gateway(name="drain-device", owner_id=admin.id, transport_kind="connector", rest_url="connector://test", ws_url="connector://test")
        db.add(gateway)
        db.flush()
        db.add(Connector(owner_id=admin.id, gateway_id=gateway.id, name="device", token_hash="d" * 64, profiles=["default"]))
        db.commit()
    monkeypatch.setattr(app.state.connector_registry, "online", lambda gateway: True)
    async def sessions():
        resume(request)
        return []
    provider = SimpleNamespace(session_inventory_complete=True, list_sessions=sessions)
    async def get(connection):
        return provider
    monkeypatch.setattr(app.state.services.provider_pool, "get", get)
    with pytest.raises(HTTPException) as error:
        await drain(request)
    assert error.value.status_code == 409
    assert not app.state.cloud_draining
