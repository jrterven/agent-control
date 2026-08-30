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

        # Simulate a process restart rebuilding the in-memory guard from the
        # durable managed marker.
        app.state.services.settings.mutable_profiles = ["default"]
        app.state.services.settings.interactive_profiles = ["default"]
        GatewayService(app.state.services).seed_environment_gateway(db)
        assert "academic-researcher" in app.state.services.settings.mutable_profiles
        assert "academic-researcher" in app.state.services.settings.interactive_profiles


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
