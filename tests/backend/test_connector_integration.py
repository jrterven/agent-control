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
        self.requests = []
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
                assert set(self.runtime.providers) <= set(welcome["profiles"])
                self.runtime.profile_transfer_supported = welcome.get("capabilities", {}).get("profileTransferV1") is True
                self.socket = SocketAdapter(ws)
                self.client.portal.call(self.activate)
                self.ready.set()
                while True:
                    message = reader.feed(ws.receive_bytes())
                    if message is None:
                        continue
                    if message["type"] == "request":
                        self.requests.append(message)
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


@pytest.fixture
def transfer_peers(setup, tmp_path, monkeypatch):
    """Two real owner-paired sockets; only the native Hermes adapter is synthetic."""
    from types import SimpleNamespace
    app, client, headers, _ = setup
    peers = []
    try:
        for label in ("source", "destination"):
            authorization = authorize(client)
            view = approve(client, headers, authorization)
            credentials = client.post("/api/v1/connectors/device/token", json={"deviceCode": authorization["deviceCode"]}).json()

            async def initialize(directory=tmp_path / label, credentials=credentials):
                return ConnectorRuntime(directory, {"server": "https://control.test", "gatewayId": credentials["gatewayId"],
                    "profiles": ["selected"], "restUrl": "http://127.0.0.1:9119", "wsUrl": "ws://127.0.0.1:9119/api/ws",
                    "sourceSha": HERMES_0212_SHA, "hermesHome": str(directory)}, {"hermesToken": "local-only"}, AuditedLocalProvider)
            runtime = client.portal.call(initialize)
            monkeypatch.setattr(runtime, "_background_snapshot", lambda profile, stored_session_id=None: {
                "available": True, "complete": True, "activeCount": 0, "pendingDeliveryCount": 0,
                "tasks": [], "observedAt": datetime.now(timezone.utc).isoformat()})
            peer = LocalPeer(client, runtime, credentials["accessToken"])
            peer.start()
            peers.append(SimpleNamespace(runtime=runtime, peer=peer, credentials=credentials, view=view))
        client.portal.call(app.state.warm_capabilities_once)
        source = peers[0]
        created = client.post("/api/v1/profiles", json={"gatewayId": source.credentials["gatewayId"],
            "technicalName": "control-dev", "displayName": "Control dev", "description": "A synthetic transfer test agent."}, headers=mutate(headers))
        assert created.status_code == 201, created.text
        yield app, client, headers, peers
    finally:
        for item in reversed(peers):
            item.peer.close()
            client.portal.call(item.runtime.ledger.close)


@pytest.mark.parametrize("private_collision", [False, True])
def test_native_archive_transfer_crosses_real_connector_wire_and_persists_only_confirmed_sharing(transfer_peers, private_collision):
    import io
    import os
    import tarfile
    from unittest.mock import AsyncMock
    from agent_control_connector.storage import read_json
    from hermes_client import ProviderConnection
    from hermes_client.types import HermesProfile
    from hermes_control_api.remote_provider import RemoteProvider, ProfileTransferNotImported

    app, client, headers, peers = transfer_peers
    source, destination = peers
    source_manager = source.runtime.providers["selected"]
    destination_manager = destination.runtime.providers["selected"]
    payload = os.urandom(1024 * 1024 + 97)  # Forces multiple binary protocol chunks.
    exported = io.BytesIO()
    with tarfile.open(fileobj=exported, mode="w:gz") as archive:
        root = tarfile.TarInfo("control-dev")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for name, content in {"control-dev/SOUL.md": b"Synthetic control-dev", "control-dev/state.db": payload,
                              "control-dev/.env": b"API_KEY=local-source-secret"}.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))

    async def export_archive(name, path):
        assert name == "control-dev"
        path.write_bytes(exported.getvalue())
        path.chmod(0o600)

    async def import_archive(name, path):
        assert name == "control-dev"
        with tarfile.open(path) as archive:
            assert "control-dev/.env" not in archive.getnames()
            assert archive.extractfile("control-dev/state.db").read() == payload
        result = HermesProfile(name=name, display_name="Control dev")
        destination_manager._created_profiles[name] = result
        return result

    source_manager.export_profile_archive_to = AsyncMock(side_effect=export_archive)
    destination_manager.import_profile_archive_from = AsyncMock(side_effect=import_archive)
    private = HermesProfile(name="control-dev", display_name="Private existing agent")
    if private_collision:
        destination_manager._created_profiles["control-dev"] = private
    registry = app.state.connector_registry
    remote_source = RemoteProvider(ProviderConnection(source.runtime.gateway_id, "selected", "connector://source", "connector://source"), registry)
    remote_destination = RemoteProvider(ProviderConnection(destination.runtime.gateway_id, "selected", "connector://destination", "connector://destination"), registry)

    async def transfer_profile():
        assert "connector.profileTransferV1" in (await remote_source.capabilities()).features
        assert "connector.profileTransferV1" in (await remote_destination.capabilities()).features
        return await remote_source.transfer_profile_to(remote_destination, name="control-dev")

    if private_collision:
        with pytest.raises(ProfileTransferNotImported):
            client.portal.call(transfer_profile)
        destination_manager.import_profile_archive_from.assert_not_called()
        assert destination_manager._created_profiles["control-dev"] is private
        assert "control-dev" not in destination.runtime.providers
        assert "control-dev" not in destination.runtime.config["profiles"]
        assert {profile.name for profile in client.portal.call(remote_destination.list_profiles)} == {"selected"}
        with app.state.session_factory() as db:
            assert db.scalar(select(ProfileRef.id).where(ProfileRef.gateway_id == destination.runtime.gateway_id,
                                                        ProfileRef.profile_name == "control-dev")) is None
        assert "profile_import_finish" not in {request["operation"] for request in destination.peer.requests}
        return

    imported = client.portal.call(transfer_profile)
    assert imported.name == "control-dev"
    source_manager.export_profile_archive_to.assert_awaited_once()
    destination_manager.import_profile_archive_from.assert_awaited_once()
    assert "control-dev" in {profile.name for profile in client.portal.call(source_manager.list_profiles)}
    assert "control-dev" in destination.runtime.providers
    assert "control-dev" in read_json(destination.runtime.directory / "config.json")["profiles"]
    with app.state.session_factory() as db:
        source_connector = db.get(Connector, source.view["id"])
        destination_connector = db.get(Connector, destination.view["id"])
        assert destination_connector.owner_id == source_connector.owner_id
        assert "control-dev" in destination_connector.profiles
    assert "control-dev" in registry.get(destination.runtime.gateway_id).profiles
    requests = [request for item in peers for request in item.peer.requests if request["operation"].startswith("profile_")]
    assert {request["operation"] for request in requests} == {"profile_export", "profile_archive_read", "profile_import_begin",
        "profile_archive_write", "profile_import_finish", "profile_archive_cleanup"}
    writes = [request for request in destination.peer.requests if request["operation"] == "profile_archive_write"]
    assert len(writes) > 1
    assert max(len(request["kwargs"]["chunk"]) for request in writes) <= 1024 * 1024
    assert len({request["operationId"] for request in writes}) == len(writes)
    # Replaying a real wire mutation reuses the durable receipt, even after the
    # archive payload has been cleaned; it must never issue another native import.
    finish = next(request for request in destination.peer.requests if request["operation"] == "profile_import_finish")

    async def replay_finish():
        return await registry.get(destination.runtime.gateway_id).call("selected", "profile_import_finish", (),
            finish["kwargs"], operation_id=finish["operationId"])
    assert client.portal.call(replay_finish).name == "control-dev"
    destination_manager.import_profile_archive_from.assert_awaited_once()
    # A new socket gets its grants from the database and the local persisted
    # config, rather than an in-memory temporary allowance in ConnectorLink.
    destination.peer.close()
    replacement = LocalPeer(client, destination.runtime, destination.credentials["accessToken"])
    replacement.start()
    try:
        assert "control-dev" in registry.get(destination.runtime.gateway_id).profiles
        assert {profile.name for profile in client.portal.call(remote_destination.list_profiles)} == {"selected", "control-dev"}
    finally:
        replacement.close()
