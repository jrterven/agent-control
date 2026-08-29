from __future__ import annotations

from fastapi.testclient import TestClient

from hermes_control_api.models import SessionLink, User
from hermes_control_api.security import hash_password

from .conftest import mutation_headers
from .test_api_sessions import create_session


def test_global_search_reads_authoritative_hermes_messages_not_loaded_ui_state(
    authenticated,
):
    client, csrf = authenticated
    session = create_session(client, csrf, "control-dev", "search-source-session")
    submitted = client.post(
        f"/api/v1/sessions/{session['id']}/prompts",
        headers=mutation_headers(csrf, "search-message-prompt"),
        json={"content": "La palabra ultramarino vive solamente en Hermes"},
    )
    assert submitted.status_code == 202, submitted.text

    response = client.get("/api/v1/search", params={"q": "ultramarino"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["partial"] is False
    hits = [item for item in body["items"] if item["kind"] == "message"]
    assert hits
    assert hits[0]["targetId"] == session["id"]
    assert "ultramarino" in hits[0]["excerpt"].lower()


def test_global_search_intersects_upstream_hits_with_owned_session_links(
    authenticated, app
):
    client, csrf = authenticated
    owned = create_session(client, csrf, "control-dev", "owned-search-session")

    async def add_unowned_upstream_session():
        with app.state.session_factory() as db:
            owned_row = db.get(SessionLink, owned["id"])
            from hermes_control_api.services import GatewayService

            connection = await GatewayService(app.state.services).connection(
                db, owned_row.gateway_id, owned_row.profile_name
            )
            other = User(
                username="search-other",
                password_hash=hash_password("not-used-in-this-test"),
                is_admin=False,
            )
            db.add(other)
            db.flush()
            provider = await app.state.services.provider_pool.get(connection)
            upstream = await provider.create_session(title="Private other session")
            provider._messages[upstream.stored_session_id].append(
                {
                    "id": "other-secret-message",
                    "role": "assistant",
                    "content": "ownership-sentinel must not leak",
                }
            )
            db.add(
                SessionLink(
                    owner_id=other.id,
                    gateway_id=owned_row.gateway_id,
                    profile_name=owned_row.profile_name,
                    stored_session_id=upstream.stored_session_id,
                    title=upstream.title,
                )
            )
            db.commit()

    client.portal.call(add_unowned_upstream_session)
    response = client.get(
        "/api/v1/search", params={"q": "ownership-sentinel", "kind": "message"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


def test_global_search_validates_query_and_searches_local_metadata(authenticated):
    client, csrf = authenticated
    workspace = client.post(
        "/api/v1/workspaces",
        headers=mutation_headers(csrf, "search-workspace"),
        json={"name": "Bitácora polar", "description": "Navegación"},
    )
    assert workspace.status_code == 201
    assert client.get("/api/v1/search", params={"q": "x"}).status_code == 422

    response = client.get(
        "/api/v1/search", params={"q": "polar", "kind": "workspace"}
    )
    assert response.status_code == 200
    assert response.json()["items"][0]["targetId"] == workspace.json()["id"]
