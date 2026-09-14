import base64
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.websockets import WebSocketDisconnect

from hermes_client.compatibility import HERMES_0212_SHA
from hermes_client.connector_protocol import FrameReader, frames
from hermes_control_api.auth import issue_session
from hermes_control_api.config import Settings
from hermes_control_api.connector_models import Connector, DeviceAuthorization
from hermes_control_api.main import create_app
from hermes_control_api.models import Gateway, GatewayCredential, User, utc_now
from hermes_control_api.services import trusted_gateway_source_sha


@pytest.fixture
def setup():
    app = create_app(Settings(environment="test", deployment_mode="cloud", public_base_url="https://control.test",
        database_url="sqlite://", vault_key_b64=base64.urlsafe_b64encode(b"c"*32).decode(), create_schema_on_start=True,
        provider_mode="mock", mock_fallback_enabled=True, allowed_origins=["http://testserver"]))
    with TestClient(app) as client:
        with app.state.session_factory() as db:
            alice = User(username="alice", password_hash="unusable", is_admin=False)
            bob = User(username="bob", password_hash="unusable", is_admin=False)
            db.add_all([alice,bob])
            db.commit()
            token, csrf, _ = issue_session(db, alice, ttl_hours=1)
            other_token, other_csrf, _ = issue_session(db, bob, ttl_hours=1)
        client.cookies.set("hc_session", token)
        yield app, client, {"X-CSRF-Token":csrf}, (other_token, {"X-CSRF-Token":other_csrf})


def authorize(client):
    response = client.post("/api/v1/connectors/device/authorize", json={"name":"Personal Mac", "profiles":["selected", "other.profile"], "version":"0.1.0", "sourceSha":HERMES_0212_SHA})
    assert response.status_code == 200, response.text
    return response.json()


def approve(client, headers, authorization):
    code = authorization["userCode"]
    inspected = client.post("/api/v1/connectors/pair/inspect", json={"code":code}, headers=headers)
    assert inspected.status_code == 200, inspected.text
    response = client.post("/api/v1/connectors/pair/approve", json={"code":code, "profiles":["selected"]}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_pairing_one_use_profile_selection_and_trusted_anchor(setup):
    app, client, headers, _ = setup
    authorization = authorize(client)
    poll = client.post("/api/v1/connectors/device/token", json={"deviceCode":authorization["deviceCode"]})
    assert poll.status_code == 428
    connector = approve(client, headers, authorization)
    assert connector["profiles"] == ["selected"]
    token = client.post("/api/v1/connectors/device/token", json={"deviceCode":authorization["deviceCode"]})
    assert token.status_code == 200, token.text
    assert "accessToken" in token.json()
    assert client.post("/api/v1/connectors/device/token", json={"deviceCode":authorization["deviceCode"]}).status_code == 400
    assert client.post("/api/v1/connectors/pair/approve", json={"code":authorization["userCode"],"profiles":["selected"]},headers=headers).status_code == 409
    with app.state.session_factory() as db:
        row = db.get(Connector, connector["id"])
        assert trusted_gateway_source_sha(db, app.state.services, row.gateway_id) == HERMES_0212_SHA
    listing = client.get("/api/v1/connectors").json()
    assert listing["items"][0]["id"] == connector["id"]
    assert "/downloads/connector/install.sh" in listing["installCommand"]
    assert "accessToken" not in str(listing)


def test_pair_requires_same_browser_session_and_csrf(setup):
    _, client, headers, (other_token, other_headers) = setup
    authorization = authorize(client)
    body = {"code":authorization["userCode"], "profiles":["selected"]}
    assert client.post("/api/v1/connectors/pair/approve", json=body, headers=headers).status_code == 403
    assert client.post("/api/v1/connectors/pair/inspect", json={"code":authorization["userCode"]}).status_code == 403
    assert client.post("/api/v1/connectors/pair/inspect", json={"code":authorization["userCode"]}, headers=headers).status_code == 200
    client.cookies.set("hc_session", other_token)
    assert client.post("/api/v1/connectors/pair/approve", json=body, headers=other_headers).status_code == 403


def test_expiry_unoffered_profile_and_cross_account_revocation(setup):
    app, client, headers, (other_token, other_headers) = setup
    authorization = authorize(client)
    client.post("/api/v1/connectors/pair/inspect", json={"code":authorization["userCode"]}, headers=headers)
    assert client.post("/api/v1/connectors/pair/approve", json={"code":authorization["userCode"],"profiles":["secret-profile"]},headers=headers).status_code == 422
    connector = approve(client, headers, authorization)
    client.cookies.set("hc_session", other_token)
    assert client.get("/api/v1/connectors").json()["items"] == []
    assert client.delete(f"/api/v1/connectors/{connector['id']}",headers=other_headers).status_code == 404
    expired = authorize(client)
    with app.state.session_factory() as db:
        for row in db.scalars(select(DeviceAuthorization).where(DeviceAuthorization.consumed_at.is_(None))):
            row.expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
    assert client.post("/api/v1/connectors/device/token",json={"deviceCode":expired["deviceCode"]}).status_code == 400


def test_websocket_authentication_and_revocation_cut_existing_link(setup):
    app, client, headers, _ = setup
    authorization = authorize(client)
    connector = approve(client, headers, authorization)
    token = client.post("/api/v1/connectors/device/token",json={"deviceCode":authorization["deviceCode"]}).json()
    bearer = {"Authorization":"Bearer " + token["accessToken"]}
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/connectors/ws"):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/connectors/ws",headers={**bearer,"Origin":"http://testserver"}):
            pass
    with client.websocket_connect("/api/v1/connectors/ws",headers=bearer) as websocket:
        reader = FrameReader()
        welcome = reader.feed(websocket.receive_bytes())
        assert welcome["gatewayId"] == token["gatewayId"]
        assert app.state.connector_registry.online(token["gatewayId"])
        assert client.delete(f"/api/v1/connectors/{connector['id']}",headers=headers).status_code == 200
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_bytes()
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/connectors/ws",headers=bearer):
            pass
