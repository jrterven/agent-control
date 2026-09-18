"""Image uploads share connector authentication and release-drain admission."""
import asyncio
import threading
from types import SimpleNamespace

import pytest
from hermes_client.connector_protocol import FrameReader, ProtocolError, VERSION, frames
from hermes_control_api.api import connector_routes
from hermes_control_api.config import Settings
from .test_connector_pairing import setup, authorize, approve  # noqa: F401


def publication(**overrides):
    return {"v": VERSION, "type": "media.publish", "id": "a" * 32,
            "profile": "selected", "sessionId": "stored-123", "metadata": {},
            "content": b"test-image", **overrides}


@pytest.mark.asyncio
async def test_transport_rejects_unshared_profile_and_drain_before_storage(monkeypatch):
    state = SimpleNamespace(settings=Settings(), cloud_draining=False, cloud_mutations_inflight=0)
    messages = []
    async def send(payload):
        messages.append(FrameReader().feed(payload))
    link = SimpleNamespace(profiles={"selected"}, lock=asyncio.Lock(), send_bytes=send)
    monkeypatch.setattr(connector_routes, "_ingest_image", lambda *_: pytest.fail("Must not ingest"))
    await connector_routes._accept_image(state, link, "connector", publication(profile="unshared"), set())
    assert messages[-1]["errorCode"] == "forbidden"
    state.cloud_draining = True
    await connector_routes._accept_image(state, link, "connector", publication(), set())
    assert messages[-1]["errorCode"] == "draining"
    assert state.cloud_mutations_inflight == 0
    with pytest.raises(ProtocolError):
        await connector_routes._accept_image(state, link, "connector", publication(content="not-binary"), set())


@pytest.mark.asyncio
async def test_publication_is_bounded_tracked_and_retryable_after_storage_failure(monkeypatch):
    state = SimpleNamespace(settings=Settings(), cloud_draining=False, cloud_mutations_inflight=0)
    messages, tasks = [], set()
    release = threading.Event()
    async def send(payload):
        messages.append(FrameReader().feed(payload))
    link = SimpleNamespace(profiles={"selected"}, lock=asyncio.Lock(), send_bytes=send)
    def held_ingest(*_):
        release.wait(5)
        raise RuntimeError("secret credentials must never be sent")
    monkeypatch.setattr(connector_routes, "_ingest_image", held_ingest)
    try:
        await connector_routes._accept_image(state, link, "connector", publication(), tasks)
        await connector_routes._accept_image(state, link, "connector", publication(id="b"*32), tasks)
        assert state.cloud_mutations_inflight == 2
        await connector_routes._accept_image(state, link, "connector", publication(id="c"*32), tasks)
        assert messages[-1]["errorCode"] == "storage_unavailable"
        assert len(tasks) == 2
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert state.cloud_mutations_inflight == state.visual_publications_inflight == 0
    assert all(message["errorCode"] == "storage_unavailable" for message in messages)
    assert "secret" not in str(messages)


def test_real_socket_media_ack_and_old_server_capability_default(setup, monkeypatch):
    app, client, headers, _ = setup
    authorization = authorize(client)
    approve(client, headers, authorization)
    credentials = client.post("/api/v1/connectors/device/token", json={"deviceCode": authorization["deviceCode"]}).json()
    received = []
    def ingest(state, connector_id, message):
        received.append((connector_id, message))
        return {"id": message["id"], "status": "ready"}
    monkeypatch.setattr(connector_routes, "_ingest_image", ingest)
    with client.websocket_connect("/api/v1/connectors/ws", headers={"Authorization": "Bearer " + credentials["accessToken"]}) as ws:
        reader = FrameReader()
        assert reader.feed(ws.receive_bytes())["capabilities"]["visualMediaV1"] is False
        for frame in frames(publication()):
            ws.send_bytes(frame)
        assert reader.feed(ws.receive_bytes()) == {"v": VERSION, "type": "media.ack", "id": "a"*32, "status": "ready"}
    assert received[0][0] == credentials["connectorId"]
    assert received[0][1]["content"] == b"test-image"
