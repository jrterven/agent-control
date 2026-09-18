"""Exercise the public HTTP API through a real ASGI WebSocket connector peer."""
import asyncio
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
import sys
import threading
from uuid import uuid4

import pytest
from starlette.websockets import WebSocketDisconnect
from hermes_client import InMemoryHermesProvider
from hermes_client.compatibility import HERMES_0212_SHA
from hermes_client.connector_protocol import FrameReader, frames
from hermes_client.types import NormalizedEvent
from hermes_control_api.connector_models import Connector
from hermes_control_api.models import ProfileRef, SessionLink
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages/connector"))
from agent_control_connector.runtime import ConnectorRuntime
from .test_connector_pairing import setup, authorize, approve


class AuditedLocalProvider(InMemoryHermesProvider):
    async def capabilities(self):
        return replace(await super().capabilities(), version="0.21.2", source_sha=HERMES_0212_SHA)


class SocketAdapter:
    def __init__(self, ws):
        self.ws = ws

    async def send(self, frame):
        self.ws.send_bytes(frame)


class LocalPeer:
    def __init__(self, client, runtime, token):
        self.client, self.runtime, self.token = client, runtime, token
        self.ready = threading.Event()
        self.errors = []
        self.sender = None
        self.socket = None
        self.ws = None
        self.thread = threading.Thread(target=self.serve, daemon=True)

    async def activate(self):
        self.runtime.websocket = self.socket
        self.sender = asyncio.create_task(self.runtime._event_sender(self.socket))
        for name, provider in self.runtime.providers.items():
            await self.runtime.on_event(NormalizedEvent.create(type="control.connection", gateway_id=self.runtime.gateway_id,
                profile_name=name, runtime_generation=provider.runtime_generation, data={"state":"connected"}))

    async def deactivate(self):
        if self.runtime.websocket is self.socket:
            self.runtime.websocket = None
        if self.sender:
            self.sender.cancel()
            await asyncio.gather(self.sender, return_exceptions=True)

    def serve(self):
        try:
            with self.client.websocket_connect("/api/v1/connectors/ws",headers={"Authorization":"Bearer "+self.token}) as ws:
                self.ws = ws
                reader = FrameReader()
                welcome = reader.feed(ws.receive_bytes())
                assert welcome["gatewayId"] == self.runtime.gateway_id
                self.socket = SocketAdapter(ws)
                self.client.portal.call(self.activate)
                self.ready.set()
                while True:
                    message = reader.feed(ws.receive_bytes())
                    if message is None:
                        continue
                    if message["type"] == "request":
                        response = self.client.portal.call(self.runtime.execute, message)
                        if self.runtime.websocket is self.socket:
                            for frame in frames(response):
                                ws.send_bytes(frame)
                    elif message["type"] == "ack":
                        self.client.portal.call(self.runtime._acknowledge, message["sequence"])
        except (WebSocketDisconnect, RuntimeError):
            pass
        except BaseException as exc:
            self.errors.append(exc)
            self.ready.set()
        finally:
            self.client.portal.call(self.deactivate)

    def start(self):
        self.thread.start()
        assert self.ready.wait(5), "Connector did not handshake"
        assert not self.errors

    def close(self):
        if self.ws and self.thread.is_alive():
            self.ws.close()
        self.thread.join(timeout=5)
        assert not self.thread.is_alive()
        assert not self.errors


@pytest.fixture
def attached(setup, tmp_path, monkeypatch):
    app, client, headers, other = setup
    authorization = authorize(client)
    view = approve(client, headers, authorization)
    credentials = client.post("/api/v1/connectors/device/token",json={"deviceCode":authorization["deviceCode"]}).json()

    async def initialize():
        return ConnectorRuntime(tmp_path,{"server":"https://control.test", "gatewayId":credentials["gatewayId"],
            "profiles":["selected"],"restUrl":"http://127.0.0.1:9119","wsUrl":"ws://127.0.0.1:9119/api/ws",
            "sourceSha":HERMES_0212_SHA,"hermesHome":str(tmp_path)}, {"hermesToken":"secret"}, AuditedLocalProvider)
    runtime = client.portal.call(initialize)
    # The deterministic in-memory Hermes fixture has no native SQLite ledger.
    # Its empty task inventory is explicit; production still requires SQLite
    # evidence from the selected profile before destructive operations.
    monkeypatch.setattr(runtime, "_background_snapshot", lambda profile, stored_session_id=None: {
        "available": True, "complete": True, "activeCount": 0, "pendingDeliveryCount": 0,
        "tasks": [], "observedAt": datetime.now(timezone.utc).isoformat()})
    peer = LocalPeer(client,runtime,credentials["accessToken"])
    peer.start()
    client.portal.call(app.state.warm_capabilities_once)
    yield app,client,headers,runtime,peer,credentials,view
    peer.close()
    client.portal.call(runtime.ledger.close)


def mutate(headers):
    return {**headers,"Idempotency-Key":uuid4().hex}


def create_session(client,headers):
    bootstrap = client.get("/api/v1/bootstrap").json()
    profile = next(row for row in bootstrap["profiles"] if row["technicalName"] == "selected")
    response = client.post("/api/v1/sessions",json={"profileId":profile["id"],"title":"Cloud conversation"},headers=mutate(headers))
    assert response.status_code == 201, response.text
    return response.json()


def test_http_chat_stream_attachment_and_automation_over_connector(attached):
    app,client,headers,runtime,peer,credentials,_ = attached
    session = create_session(client,headers)
    submitted = client.post(f"/api/v1/sessions/{session['id']}/prompts",json={"content":"Hello through the connector"},headers=mutate(headers))
    assert submitted.status_code in {200,202}, submitted.text
    client.portal.call(asyncio.sleep,0.12)
    messages = client.get(f"/api/v1/sessions/{session['id']}/messages")
    assert messages.status_code == 200, messages.text
    assert "Hello through the connector" in messages.text
    assert "Respuesta simulada" in messages.text
    attachment = client.post(f"/api/v1/sessions/{session['id']}/prompts-with-attachments",data={"content":"Read this attachment"},
        files=[("attachments",("note.txt",b"local context","text/plain"))],headers=mutate(headers))
    assert attachment.status_code in {200,202}, attachment.text
    automation = client.post("/api/v1/automations",json={"gatewayId":credentials["gatewayId"],"profileName":"selected", "name":"Weekly check",
        "schedule":"0 9 * * MON", "timezone":"UTC", "prompt":"Summarize the week", "enabled":False},headers=mutate(headers))
    assert automation.status_code == 201, automation.text
    assert len(runtime.providers["selected"]._automations) == 1
    assert not peer.errors


def test_new_profile_is_shared_and_reconnect_retains_local_hermes(attached):
    app,client,headers,runtime,peer,credentials,view = attached
    response = client.post("/api/v1/profiles",json={"gatewayId":credentials["gatewayId"],"technicalName":"new-agent","displayName":"New agent","description":"A useful agent for integration testing."},headers=mutate(headers))
    assert response.status_code == 201, response.text
    assert "new-agent" in runtime.providers
    assert "new-agent" in runtime.config["profiles"]
    with app.state.session_factory() as db:
        connector = db.get(Connector,view["id"])
        assert "new-agent" in connector.profiles
    original = runtime.providers["selected"]
    generation = original.runtime_generation
    peer.close()
    assert runtime.providers["selected"] is original
    assert original.runtime_generation == generation
    replacement = LocalPeer(client,runtime,credentials["accessToken"])
    replacement.start()
    try:
        client.portal.call(app.state.warm_capabilities_once)
        assert {row["technicalName"] for row in client.get("/api/v1/bootstrap").json()["profiles"]} == {"selected","new-agent"}
    finally:
        replacement.close()


def test_disconnect_after_dispatch_reconciles_without_repeating_prompt(attached):
    app,client,headers,runtime,peer,credentials,_ = attached
    session = create_session(client,headers)
    provider = runtime.providers["selected"]
    accepted = threading.Event()
    gate = client.portal.call(asyncio.Event)
    original_submit = provider.submit_prompt
    calls = []

    async def held_submit(*args,**kwargs):
        calls.append(kwargs["operation_id"])
        result = await original_submit(*args,**kwargs)
        accepted.set()
        await gate.wait()
        return result
    provider.submit_prompt = held_submit
    result = []
    headers_once = mutate(headers)
    thread = threading.Thread(target=lambda: result.append(client.post(f"/api/v1/sessions/{session['id']}/prompts",json={"content":"Only once"},headers=headers_once)),daemon=True)
    thread.start()
    assert accepted.wait(5)
    # Closing just the cloud side leaves the local dispatch coroutine running.
    client.portal.call(app.state.connector_registry.disconnect,credentials["gatewayId"])
    thread.join(timeout=5)
    assert not thread.is_alive()
    client.portal.call(gate.set)
    peer.close()
    assert len(calls) == 1
    replacement = LocalPeer(client,runtime,credentials["accessToken"])
    replacement.start()
    try:
        client.portal.call(app.state.warm_capabilities_once)
        repeated = client.post(f"/api/v1/sessions/{session['id']}/prompts",json={"content":"Only once"},headers=headers_once)
        assert repeated.status_code in {200,202,409,502}, repeated.text
        assert len(calls) == 1
        stored = client.get(f"/api/v1/sessions/{session['id']}/messages")
        assert stored.status_code == 200, stored.text
        assert sum(1 for item in provider._messages[next(iter(provider._sessions))] if item["role"] == "user" and item["content"] == "Only once") == 1
    finally:
        replacement.close()


@pytest.mark.parametrize("active_children", [0, 1])
def test_shared_profile_can_be_deleted_through_another_shared_manager(attached, active_children, monkeypatch):
    app,client,headers,runtime,peer,credentials,view = attached
    # A later-sorting target makes the original selected profile its manager.
    created = client.post("/api/v1/profiles",json={"gatewayId":credentials["gatewayId"],"technicalName":"z-new-agent",
        "displayName":"New agent","description":"A useful agent for integration testing."},headers=mutate(headers))
    assert created.status_code == 201, created.text
    client.portal.call(asyncio.sleep,0.15)
    with app.state.session_factory() as db:
        profile = db.scalar(select(ProfileRef).where(ProfileRef.gateway_id == credentials["gatewayId"],ProfileRef.profile_name == "z-new-agent"))
        identifier = profile.id
    if active_children:
        monkeypatch.setattr(runtime, "_background_snapshot", lambda profile, stored_session_id=None: {
            "complete": True, "activeCount": active_children, "pendingDeliveryCount": 0})
    deleted = client.request("DELETE",f"/api/v1/profiles/{identifier}",json={"confirmation":"z-new-agent"},headers=mutate(headers))
    if active_children:
        assert deleted.status_code == 409 and deleted.json()["code"] == "BACKGROUND_TASKS_BUSY", deleted.text
        assert "z-new-agent" in {row["technicalName"] for row in client.get("/api/v1/bootstrap").json()["profiles"]}
        return
    assert deleted.status_code == 200, deleted.text
    assert "z-new-agent" not in {row["technicalName"] for row in client.get("/api/v1/bootstrap").json()["profiles"]}
