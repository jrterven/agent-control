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


def test_managed_metadata_is_optional_diagnostic_and_owner_scoped(setup):
    app, client, headers, other = setup
    response = client.post("/api/v1/connectors/device/authorize", json={"name": "Managed Mac",
        "profiles": ["selected"], "version": "0.1.0", "sourceSha": HERMES_0212_SHA,
        "installationKind": "managed", "hermesVersion": "0.21.2"})
    assert response.status_code == 200
    connector = approve(client, headers, response.json())
    assert connector["installationKind"] == "managed"
    assert connector["hermesVersion"] == "0.21.2"
    assert connector["gatewayId"]
    client.cookies.set("hc_session", other[0])
    assert client.get("/api/v1/connectors").json()["items"] == []


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


def test_revoked_computer_can_pair_again_without_reactivating_old_credential(setup):
    _, client, headers, _ = setup
    original = authorize(client)
    old_computer = approve(client, headers, original)
    old_token = client.post("/api/v1/connectors/device/token", json={"deviceCode": original["deviceCode"]}).json()
    assert client.delete(f"/api/v1/connectors/{old_computer['id']}", headers=headers).status_code == 200

    replacement = authorize(client)
    # The same hostname and profiles still require a new browser approval.
    assert client.post("/api/v1/connectors/device/token", json={"deviceCode": replacement["deviceCode"]}).status_code == 428
    new_computer = approve(client, headers, replacement)
    new_token = client.post("/api/v1/connectors/device/token", json={"deviceCode": replacement["deviceCode"]}).json()
    assert new_computer["id"] != old_computer["id"]
    assert new_token["gatewayId"] != old_token["gatewayId"]
    assert new_token["accessToken"] != old_token["accessToken"]
    assert new_token["profiles"] == ["selected"]
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/v1/connectors/ws", headers={"Authorization": "Bearer " + old_token["accessToken"]}):
            pass
    with client.websocket_connect("/api/v1/connectors/ws", headers={"Authorization": "Bearer " + new_token["accessToken"]}) as websocket:
        welcome = FrameReader().feed(websocket.receive_bytes())
        assert welcome["gatewayId"] == new_token["gatewayId"]
        listing = {item["id"]: item for item in client.get("/api/v1/connectors").json()["items"]}
        assert listing[old_computer["id"]]["status"] == "revoked"
        assert listing[new_computer["id"]]["status"] == "online"


def test_release_telemetry_and_update_intent_are_owner_scoped(setup):
    app, client, headers, (other_token, other_headers) = setup
    authorization = authorize(client)
    connector = approve(client, headers, authorization)
    endpoint = f"/api/v1/connectors/{connector['id']}/update"
    assert client.post(endpoint, json={"action": "now"}, headers=headers).status_code == 409
    token = client.post("/api/v1/connectors/device/token", json={"deviceCode": authorization["deviceCode"]}).json()
    with client.websocket_connect("/api/v1/connectors/ws", headers={"Authorization": "Bearer " + token["accessToken"]}) as websocket:
        reader = FrameReader()
        assert reader.feed(websocket.receive_bytes())["type"] == "welcome"
        heartbeat = {"v": 1, "type": "heartbeat", "version": "0.1.0", "profiles": {}, "updater": {
            "protocol": 1, "supported": True, "release": "a" * 40, "state": "available", "availableRelease": "b" * 40}}
        for frame in frames(heartbeat):
            websocket.send_bytes(frame)
        control = reader.feed(websocket.receive_bytes())
        assert control == {"v": 1, "type": "update.control", "automatic": True, "requestId": None, "pausedUntil": 0}
        item = client.get("/api/v1/connectors").json()["items"][0]
        assert item["version"] == "a" * 40
        assert item["update"]["availableRelease"] == "b" * 40
        assert client.post(endpoint, json={"action": "now"}).status_code == 403
        assert client.post(endpoint, json={"action": "now", "command": "rm -rf /"}, headers=headers).status_code == 422
        assert client.post(endpoint, json={"action": "preferences", "automatic": False}, headers=headers).status_code == 200
        assert client.post(endpoint, json={"action": "now"}, headers=headers).status_code == 200
        for frame in frames(heartbeat):
            websocket.send_bytes(frame)
        control = reader.feed(websocket.receive_bytes())
        assert control["automatic"] is False and len(control["requestId"]) == 32
        assert client.post(endpoint, json={"action": "postpone"}, headers=headers).status_code == 200
        for frame in frames(heartbeat):
            websocket.send_bytes(frame)
        postponed = reader.feed(websocket.receive_bytes())
        assert postponed["requestId"] is None and postponed["pausedUntil"] > utc_now().timestamp()
        client.cookies.set("hc_session", other_token)
        assert client.post(endpoint, json={"action": "now"}, headers=other_headers).status_code == 404
        assert client.get("/api/v1/connectors").json()["items"] == []
