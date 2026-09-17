"""Revocation removes active choices without deleting retained account data."""

from sqlalchemy import select

from hermes_control_api.connector_models import Connector
from hermes_control_api.models import (
    Automation,
    Gateway,
    ProfileRef,
    SessionLink,
    User,
    Workspace,
    utc_now,
)

from .test_connector_pairing import approve, authorize, setup


def seed_metadata(app, gateway_id, *, workspace_id=None, suffix="old"):
    with app.state.session_factory() as db:
        gateway = db.get(Gateway, gateway_id)
        profile = db.scalar(
            select(ProfileRef).where(ProfileRef.gateway_id == gateway_id)
        )
        profile.display_name = "Jarvis"
        if workspace_id is None:
            workspace = Workspace(owner_id=gateway.owner_id, name="Shared workspace")
            db.add(workspace)
            db.flush()
            workspace_id = workspace.id
        session = SessionLink(
            owner_id=gateway.owner_id,
            gateway_id=gateway_id,
            profile_name=profile.profile_name,
            workspace_id=workspace_id,
            stored_session_id="same-hermes-session",
            title=f"visibility-sentinel conversation {suffix}",
        )
        automation = Automation(
            owner_id=gateway.owner_id,
            gateway_id=gateway_id,
            profile_name=profile.profile_name,
            workspace_id=workspace_id,
            name=f"visibility-sentinel automation {suffix}",
            schedule="0 12 * * *",
            prompt="Retained automation configuration",
        )
        db.add_all([session, automation])
        db.commit()
        return {
            "profile": profile.id,
            "session": session.id,
            "automation": automation.id,
            "workspace": workspace_id,
        }


def test_repair_hides_revoked_routes_and_keeps_original_metadata(setup):
    app, client, headers, _ = setup
    old = approve(client, headers, authorize(client))
    old_data = seed_metadata(app, old["gatewayId"])
    before = client.get("/api/v1/bootstrap").json()
    assert {row["id"] for row in before["profiles"]} == {old_data["profile"]}
    assert before["workspaces"][0]["sessionCount"] == 1

    assert client.delete(
        f"/api/v1/connectors/{old['id']}", headers=headers
    ).status_code == 200
    replacement = approve(client, headers, authorize(client))
    assert replacement["name"] == old["name"]
    assert replacement["gatewayId"] != old["gatewayId"]
    new_data = seed_metadata(
        app, replacement["gatewayId"], workspace_id=old_data["workspace"], suffix="new"
    )

    projection = client.get("/api/v1/bootstrap").json()
    assert {row["id"] for row in projection["gateways"]} == {replacement["gatewayId"]}
    assert {row["id"] for row in projection["profiles"]} == {new_data["profile"]}
    assert {row["id"] for row in projection["sessions"]} == {new_data["session"]}
    assert {row["id"] for row in projection["automations"]} == {new_data["automation"]}
    assert projection["sessions"][0]["profileId"] == new_data["profile"]
    assert projection["automations"][0]["profileId"] == new_data["profile"]
    assert projection["workspaces"][0]["sessionCount"] == 1
    assert {row["id"] for row in client.get("/api/v1/gateways").json()} == {
        replacement["gatewayId"]
    }
    assert client.get(
        "/api/v1/profiles", params={"gatewayId": old["gatewayId"]}
    ).json() == []
    assert len(client.get(
        "/api/v1/profiles", params={"gatewayId": replacement["gatewayId"]}
    ).json()) == 1

    for kind, expected in (
        ("session", new_data["session"]),
        ("automation", new_data["automation"]),
    ):
        response = client.get(
            "/api/v1/search", params={"q": "visibility-sentinel", "kind": kind}
        )
        assert response.status_code == 200, response.text
        assert {row["targetId"] for row in response.json()["items"]} == {expected}

    # The active projection is not a deletion or a rewrite of the old route.
    canonical = client.get(
        "/api/v1/sessions", params={"gatewayId": old["gatewayId"]}
    )
    assert canonical.status_code == 200, canonical.text
    assert {row["id"] for row in canonical.json()} == {old_data["session"]}
    computers = {row["id"]: row for row in client.get("/api/v1/connectors").json()["items"]}
    assert computers[old["id"]]["status"] == "revoked"
    assert computers[replacement["id"]]["status"] == "offline"
    with app.state.session_factory() as db:
        assert db.get(Gateway, old["gatewayId"]).enabled is False
        assert db.get(Connector, old["id"]).revoked_at is not None
        assert db.get(ProfileRef, old_data["profile"]).display_name == "Jarvis"
        assert db.get(SessionLink, old_data["session"]).gateway_id == old["gatewayId"]
        assert db.get(Automation, old_data["automation"]).gateway_id == old["gatewayId"]
        assert db.get(Workspace, old_data["workspace"]).archived_at is None


def test_existing_revoked_rows_hidden_but_same_named_offline_computers_remain(setup):
    app, client, headers, _ = setup
    revoked = approve(client, headers, authorize(client))
    active = [approve(client, headers, authorize(client)) for _ in range(2)]
    # Cover an already-revoked stored row without relying on a new DELETE call
    # (or on the gateway's legacy enabled flag) to repair the projection.
    with app.state.session_factory() as db:
        db.get(Connector, revoked["id"]).revoked_at = utc_now()
        assert db.get(Gateway, revoked["gatewayId"]).enabled is True
        db.commit()
    assert len({row["name"] for row in [revoked, *active]}) == 1
    projection = client.get("/api/v1/bootstrap").json()
    expected = {row["gatewayId"] for row in active}
    assert {row["id"] for row in projection["gateways"]} == expected
    assert {row["gatewayId"] for row in projection["profiles"]} == expected
    assert all(row["status"] == "offline" for row in projection["gateways"])
    assert all(row["status"] == "offline" for row in projection["profiles"])


def test_revoked_and_replacement_metadata_stays_owner_scoped(setup):
    app, client, headers, (other_token, _) = setup
    old = approve(client, headers, authorize(client))
    seed_metadata(app, old["gatewayId"])
    assert client.delete(
        f"/api/v1/connectors/{old['id']}", headers=headers
    ).status_code == 200
    replacement = approve(client, headers, authorize(client))
    seed_metadata(app, replacement["gatewayId"], suffix="replacement")

    client.cookies.set("hc_session", other_token)
    projection = client.get("/api/v1/bootstrap").json()
    for collection in ("gateways", "profiles", "sessions", "automations", "workspaces"):
        assert projection[collection] == []
    assert client.get("/api/v1/gateways").json() == []
    assert client.get("/api/v1/connectors").json()["items"] == []
    assert client.get("/api/v1/search", params={"q": "visibility-sentinel"}).json()["items"] == []
    for computer in (old, replacement):
        for endpoint in ("profiles", "sessions"):
            assert client.get(
                f"/api/v1/{endpoint}", params={"gatewayId": computer["gatewayId"]}
            ).json() == []


def test_private_disabled_gateway_and_its_resources_remain_visible(authenticated, app):
    client, _ = authenticated
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        gateway = Gateway(
            owner_id=owner.id,
            name="Disabled private installation",
            rest_url="http://127.0.0.1:19876",
            ws_url="ws://127.0.0.1:19876",
            enabled=False,
        )
        db.add(gateway)
        db.flush()
        db.add(ProfileRef(
            gateway_id=gateway.id, profile_name="selected", display_name="Jarvis"
        ))
        db.commit()
        gateway_id = gateway.id
    data = seed_metadata(app, gateway_id)

    projection = client.get("/api/v1/bootstrap").json()
    assert gateway_id in {row["id"] for row in projection["gateways"]}
    assert data["profile"] in {row["id"] for row in projection["profiles"]}
    assert data["session"] in {row["id"] for row in projection["sessions"]}
    assert data["automation"] in {row["id"] for row in projection["automations"]}
    assert gateway_id in {row["id"] for row in client.get("/api/v1/gateways").json()}
    assert len(client.get("/api/v1/profiles", params={"gatewayId": gateway_id}).json()) == 1
    response = client.get(
        "/api/v1/search", params={"q": "visibility-sentinel", "kind": "session"}
    )
    assert {row["targetId"] for row in response.json()["items"]} == {data["session"]}
