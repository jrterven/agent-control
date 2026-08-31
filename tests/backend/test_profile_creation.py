from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from hermes_control_api.models import ProfileRef
from hermes_control_api.schemas import ProfileCreate, PromptRequest
from hermes_control_api.services import GatewayService

from .conftest import mutation_headers


def test_agent_setup_brief_accepts_long_prompts_with_a_bounded_safety_ceiling():
    long_brief = "A" * 50_000
    payload = ProfileCreate(
        gateway_id="gateway-a",
        technical_name="turing",
        display_name="Turing",
        description=long_brief,
    )
    assert payload.description == long_brief
    wrapped_prompt = PromptRequest(content=f"Setup instructions\n\n{('A' * 200_000)}")
    assert len(wrapped_prompt.content) > 200_000

    with pytest.raises(ValidationError):
        ProfileCreate(
            gateway_id="gateway-a",
            technical_name="too-large",
            display_name="Too large",
            description="A" * 200_001,
        )


def test_admin_can_create_use_and_refresh_an_isolated_agent(authenticated, app):
    client, csrf = authenticated
    bootstrap = client.get("/api/v1/bootstrap")
    assert bootstrap.status_code == 200
    body = bootstrap.json()
    gateway_id = body["gateways"][0]["id"]
    assert any(
        profile["capabilities"]["profileCreate"]
        for profile in body["profiles"]
        if profile["gatewayId"] == gateway_id
    )

    payload = {
        "gatewayId": gateway_id,
        "technicalName": "academic-researcher",
        "displayName": "Academic Researcher",
        "description": (
            "Find and compare primary academic sources, then explain the "
            "evidence and uncertainty clearly."
        ),
    }
    headers = mutation_headers(csrf, "create-academic-researcher")
    created = client.post("/api/v1/profiles", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    agent = created.json()
    assert agent["gatewayId"] == gateway_id
    assert agent["technicalName"] == "academic-researcher"
    assert agent["displayName"] == "Academic Researcher"
    assert agent["status"] == "ready"
    assert agent["mutable"] is True
    assert agent["capabilities"]["prompts"] is True
    assert agent["capabilities"]["profileCreate"] is True

    # HTTP idempotency replays the completed response without a second Hermes
    # mutation, and a refresh must preserve the local display name.
    replay = client.post("/api/v1/profiles", json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == agent
    refreshed = client.post(
        f"/api/v1/profiles/refresh?gatewayId={gateway_id}",
        headers=mutation_headers(csrf, "refresh-created-agent"),
    )
    assert refreshed.status_code == 200, refreshed.text

    current = client.get("/api/v1/bootstrap").json()
    matches = [
        profile
        for profile in current["profiles"]
        if profile["technicalName"] == "academic-researcher"
    ]
    assert len(matches) == 1
    assert matches[0]["displayName"] == "Academic Researcher"
    assert matches[0]["mutable"] is True

    with app.state.session_factory() as db:
        row = db.scalar(
            select(ProfileRef).where(
                ProfileRef.gateway_id == gateway_id,
                ProfileRef.profile_name == "academic-researcher",
            )
        )
        assert row is not None
        assert row.managed_by_control is True
        assert row.description == payload["description"]

        # The durable route marker is consulted directly. Reseeding after a
        # simulated restart must not widen the operator's global allowlists.
        app.state.services.settings.mutable_profiles = ["default"]
        app.state.services.settings.interactive_profiles = ["default"]
        GatewayService(app.state.services).seed_environment_gateway(db)
        assert app.state.services.settings.mutable_profiles == ["default"]
        assert app.state.services.settings.interactive_profiles == ["default"]

    usable = client.post(
        "/api/v1/sessions",
        headers=mutation_headers(csrf, "use-managed-agent-after-reseed"),
        json={
            "gatewayId": gateway_id,
            "profileName": "academic-researcher",
            "title": "Managed route remains usable",
        },
    )
    assert usable.status_code == 201, usable.text
    assert usable.json()["gatewayId"] == gateway_id


def test_admin_can_create_and_use_the_same_agent_name_on_two_gateways(
    authenticated, app
):
    client, csrf = authenticated
    primary_gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    secondary = client.post(
        "/api/v1/gateways",
        headers=mutation_headers(csrf, "create-secondary-agent-gateway"),
        json={
            "name": "Secondary agent gateway",
            "restUrl": "http://127.0.0.1:39119",
            "wsUrl": "ws://127.0.0.1:39119/api/ws",
            "connectionMode": "tunnel",
            "trustedSourceSha": "b" * 40,
        },
    )
    assert secondary.status_code == 201, secondary.text
    secondary_gateway_id = secondary.json()["id"]
    refreshed = client.post(
        f"/api/v1/profiles/refresh?gatewayId={secondary_gateway_id}",
        headers=mutation_headers(csrf, "refresh-secondary-agent-gateway"),
    )
    assert refreshed.status_code == 200, refreshed.text
    assert any(item["profileName"] == "default" for item in refreshed.json())

    created = []
    for gateway_id, suffix in (
        (primary_gateway_id, "primary"),
        (secondary_gateway_id, "secondary"),
    ):
        response = client.post(
            "/api/v1/profiles",
            headers=mutation_headers(csrf, f"create-scoped-researcher-{suffix}"),
            json={
                "gatewayId": gateway_id,
                "technicalName": "scoped-researcher",
                "displayName": f"Scoped Researcher {suffix.title()}",
                "description": "Research only within this gateway's isolated profile.",
            },
        )
        assert response.status_code == 201, response.text
        assert response.json()["gatewayId"] == gateway_id
        assert response.json()["mutable"] is True
        created.append(response.json())

        session = client.post(
            "/api/v1/sessions",
            headers=mutation_headers(csrf, f"use-scoped-researcher-{suffix}"),
            json={
                "gatewayId": gateway_id,
                "profileName": "scoped-researcher",
                "title": f"Scoped route {suffix}",
            },
        )
        assert session.status_code == 201, session.text
        assert session.json()["gatewayId"] == gateway_id

    assert created[0]["id"] != created[1]["id"]
    with app.state.session_factory() as db:
        rows = list(
            db.scalars(
                select(ProfileRef).where(
                    ProfileRef.profile_name == "scoped-researcher"
                )
            ).all()
        )
        assert {row.gateway_id for row in rows} == {
            primary_gateway_id,
            secondary_gateway_id,
        }
        assert all(row.managed_by_control for row in rows)


def test_agent_creation_rejects_reserved_and_duplicate_names(authenticated):
    client, csrf = authenticated
    gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    base = {
        "gatewayId": gateway_id,
        "displayName": "Duplicate Agent",
        "description": "A sufficiently detailed description for an agent.",
    }
    reserved = client.post(
        "/api/v1/profiles",
        json={**base, "technicalName": "default"},
        headers=mutation_headers(csrf, "reserved-agent-name"),
    )
    assert reserved.status_code == 422

    existing = client.post(
        "/api/v1/profiles",
        json={**base, "technicalName": "jarvis"},
        headers=mutation_headers(csrf, "duplicate-agent-name"),
    )
    assert existing.status_code == 409
    assert "already exists" in existing.json()["message"]
