"""A scheduled run publishes without a browser; cloud images survive its host."""
from hermes_client.connector_protocol import FrameReader, frames
from hermes_control_api.connector_models import Connector
from hermes_control_api.models import SessionLink, User
from hermes_control_api.services import SessionService
from hermes_control_api.visual_media import VisualMediaService

from .test_connector_pairing import setup, authorize, approve
from .test_visual_media import FakeStore, picture


def test_binary_publication_history_offline_and_restored_backup(setup, tmp_path):
    app, client, headers, other = setup
    store = FakeStore()
    service = VisualMediaService(app.state.settings, store)
    app.state.services.visual_media = service
    request = authorize(client)
    view = approve(client, headers, request)
    token = client.post("/api/v1/connectors/device/token", json={"deviceCode": request["deviceCode"]}).json()
    identifier = "9" * 32
    text = f"Daily briefing\n\n![Paper figure](ac-media:{identifier})\n\nInterpretation and source."
    publication = dict(v=1, type="media.publish", id=identifier, profile="selected", sessionId="scheduled-hermes-session",
        content=picture(), metadata=dict(alt="Paper figure", provenance="web", sourceUrl="https://example.org/paper",
                                         width=80, height=60, mediaType="image/png"))
    with client.websocket_connect("/api/v1/connectors/ws", headers={"Authorization": "Bearer " + token["accessToken"]}) as ws:
        reader = FrameReader()
        welcome = reader.feed(ws.receive_bytes())
        assert welcome["capabilities"]["visualMediaV1"] is True
        # Bearer-only connector publications do not depend on an open browser,
        # an already-imported Control session or a browser-authenticated upload.
        client.cookies.clear()
        for frame in frames(publication):
            ws.send_bytes(frame)
        ack = None
        while ack is None:
            ack = reader.feed(ws.receive_bytes())
        assert ack == {"v": 1, "type": "media.ack", "id": identifier, "status": "ready"}
        for frame in frames(publication):
            ws.send_bytes(frame)
        repeated = None
        while repeated is None:
            repeated = reader.feed(ws.receive_bytes())
        assert repeated == ack and store.puts == 2
        with app.state.session_factory() as db:
            connector = db.get(Connector, view["id"])
            session = SessionLink(owner_id=connector.owner_id, gateway_id=connector.gateway_id,
                                  profile_name="selected", stored_session_id="scheduled-hermes-session")
            db.add(session)
            db.commit()
            session_id = session.id
            projected = SessionService(app.state.services)._project_history(db, session, [{"role": "assistant", "content": text}])
            assert projected[0]["content"] == text
            assert projected[0]["controlMedia"][0]["status"] == "ready"
            from hermes_control_api.auth import issue_session
            browser_token, _, _ = issue_session(db, db.get(User, connector.owner_id), ttl_hours=1)
    assert not app.state.connector_registry.online(token["gatewayId"])
    client.cookies.set("hc_session", browser_token)
    base = f"/api/v1/sessions/{session_id}/media/{identifier}"
    assert client.get(base).status_code == 200
    assert client.get(base).headers["content-disposition"] == f'inline; filename="{identifier}.png"'
    assert client.get(base + "?variant=thumbnail").headers["content-type"] == "image/webp"
    assert client.get(base + "/metadata").json()["sourceUrl"] == "https://example.org/paper"
    with app.state.session_factory() as db:
        service.backup(db, tmp_path)
        assert service.verify_backup(db, tmp_path) == 1
        store.objects.clear()
        assert service.restore(db, tmp_path) == 1
    assert client.get(base).status_code == 200
    client.cookies.set("hc_session", other[0])
    assert client.get(base).status_code == 404
    assert client.get(base + "/metadata").status_code == 404
