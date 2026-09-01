from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from hermes_client import (
    CapabilitySet,
    InMemoryHermesProvider,
    contains_secret_fields,
    sanitize_admin_payload,
    writable_config_projection,
)
from hermes_control_api.models import User
from hermes_control_api.security import hash_password

from .conftest import mutation_headers


@pytest.mark.parametrize(
    "document",
    [
        {"accessToken": "private"},
        {"clientSecret": "private"},
        {"refreshToken": "private"},
        {"key": "accessToken", "value": "private"},
        {"name": "clientSecret", "value": "private"},
        {"type": "password", "value": "private"},
        {"type": "apiKey", "value": "private"},
        {"redacted_value": "***"},
        {"nested": [{"is_secret": True, "value": "private"}]},
    ],
)
def test_generic_config_secret_detector_handles_semantic_and_camel_case_shapes(
    document: dict,
):
    assert contains_secret_fields(document)


@pytest.mark.parametrize("discriminator", ["role", "type", "kind"])
def test_admin_sanitizer_replaces_private_reasoning_nodes_with_marker(
    discriminator: str,
):
    private = "PRIVATE-REASONING-MUST-NOT-LEAK"
    sanitized = sanitize_admin_payload(
        {
            discriminator: "thinking",
            "content": private,
            "text": private,
            "delta": private,
            "value": private,
            "payload": {"content": private},
            "data": [private],
            "status": "streaming",
        }
    )

    assert sanitized == {
        discriminator: "thinking",
        "status": "streaming",
        "privateReasoning": True,
    }
    assert private not in str(sanitized)


def test_admin_sanitizer_never_echoes_semantic_secret_values():
    private = "PRIVATE-SECRET-MUST-NOT-LEAK"
    sanitized = sanitize_admin_payload(
        {
            "items": [
                {"key": "accessToken", "value": private},
                {"name": "clientSecret", "value": private},
                {"type": "password", "redacted_value": private},
            ]
        }
    )

    assert private not in str(sanitized)
    assert all(item["configured"] for item in sanitized["items"])
    assert all("value" not in item for item in sanitized["items"])
    assert all("redacted_value" not in item for item in sanitized["items"])


def test_sanitized_config_projection_can_round_trip_without_secret_markers():
    sanitized = sanitize_admin_payload(
        {
            "timezone": "America/Mexico_City",
            "auxiliary": {
                "vision": {"model": "vision-model", "api_key": ""},
                "web": {"enabled": True, "apiKey": "PRIVATE"},
            },
            "credentials": {"provider": "PRIVATE"},
        }
    )
    projected = writable_config_projection(sanitized)

    assert projected == {
        "timezone": "America/Mexico_City",
        "auxiliary": {
            "vision": {"model": "vision-model"},
            "web": {"enabled": True},
        },
    }
    assert contains_secret_fields(projected) is False


def test_redact_secrets_preference_is_preserved_as_non_secret_config():
    sanitized = sanitize_admin_payload(
        {"security": {"redact_secrets": True}, "api_key": "PRIVATE"}
    )
    projected = writable_config_projection(sanitized)

    assert projected == {"security": {"redact_secrets": True}}
    assert contains_secret_fields(projected) is False


def _admin_base(client: TestClient) -> str:
    gateway_id = client.get("/api/v1/gateways").json()[0]["id"]
    return f"/api/v1/admin/gateways/{gateway_id}/profiles/control-dev"


@pytest.mark.parametrize(
    "suffix,resource",
    [
        ("models", "models"),
        ("config", "config"),
        ("soul", "soul"),
        ("memory", "memory"),
        ("skills", "skills"),
        ("toolsets", "toolsets"),
        ("mcp/servers", "mcp"),
        ("channels", "channels"),
        ("usage?days=7", "usage"),
        ("secrets", "secrets"),
    ],
)
def test_all_phase2_reads_have_one_stable_sanitized_envelope(
    authenticated, suffix: str, resource: str
):
    client, _ = authenticated
    response = client.get(f"{_admin_base(client)}/{suffix}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["profileName"] == "control-dev"
    assert body["resource"] == resource
    assert isinstance(body["data"], dict)
    assert "PRIVATE" not in response.text


@pytest.mark.parametrize(
    "config",
    [
        {"integration": {"accessToken": "do-not-store-here"}},
        {"integration": {"key": "accessToken", "value": "do-not-store-here"}},
        {"integration": {"name": "clientSecret", "value": "do-not-store-here"}},
        {"integration": {"type": "password", "value": "do-not-store-here"}},
    ],
)
def test_config_endpoint_rejects_all_secret_shaped_writes(authenticated, config: dict):
    client, csrf = authenticated
    response = client.patch(
        f"{_admin_base(client)}/config",
        headers=mutation_headers(csrf, "reject-generic-secret"),
        json={"config": config},
    )

    assert response.status_code == 422
    assert "do-not-store-here" not in response.text


def test_write_only_secrets_are_never_returned_and_are_idempotent(authenticated):
    client, csrf = authenticated
    secret = "PRIVATE-OPENAI-KEY-MUST-NOT-LEAK"
    headers = mutation_headers(csrf, "set-openai-secret")
    first = client.patch(
        f"{_admin_base(client)}/secrets/OPENAI_API_KEY",
        headers=headers,
        json={"value": secret},
    )
    replay = client.patch(
        f"{_admin_base(client)}/secrets/OPENAI_API_KEY",
        headers=headers,
        json={"value": secret},
    )
    inventory = client.get(f"{_admin_base(client)}/secrets")

    assert first.status_code == replay.status_code == inventory.status_code == 200
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert secret not in first.text + replay.text + inventory.text
    assert first.json()["data"] == {
        "name": "OPENAI_API_KEY",
        "configured": True,
        "status": "applied",
    }


def test_unknown_model_mutation_is_reconciled_instead_of_returning_internal_error(
    authenticated, monkeypatch
):
    client, csrf = authenticated
    monkeypatch.setattr(
        InMemoryHermesProvider,
        "set_model",
        AsyncMock(side_effect=RuntimeError("MUTATION_DELIVERY_UNKNOWN")),
    )

    response = client.patch(
        f"{_admin_base(client)}/models",
        headers=mutation_headers(csrf, "unknown-model-mutation"),
        json={
            "provider": "mock",
            "model": "mock-model",
            "confirmExpensiveModel": False,
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "MUTATION_DELIVERY_UNKNOWN"
    assert "internal error" not in response.text.casefold()


def test_mcp_and_channel_secret_fields_are_write_only(authenticated):
    client, csrf = authenticated
    mcp_secret = "PRIVATE-MCP-KEY-MUST-NOT-LEAK"
    created = client.post(
        f"{_admin_base(client)}/mcp/servers",
        headers=mutation_headers(csrf, "create-secret-mcp"),
        json={
            "name": "private-mcp",
            "url": "https://mcp.invalid/sse",
            "bearerToken": mcp_secret,
        },
    )
    mcp_list = client.get(f"{_admin_base(client)}/mcp/servers")
    channel_secret = "PRIVATE-TELEGRAM-TOKEN-MUST-NOT-LEAK"
    channel = client.patch(
        f"{_admin_base(client)}/channels/telegram",
        headers=mutation_headers(csrf, "configure-telegram"),
        json={"enabled": True, "env": {"TELEGRAM_BOT_TOKEN": channel_secret}},
    )
    channel_list = client.get(f"{_admin_base(client)}/channels")

    assert created.status_code == 201, created.text
    assert mcp_list.status_code == channel.status_code == channel_list.status_code == 200
    combined = created.text + mcp_list.text + channel.text + channel_list.text
    assert mcp_secret not in combined
    assert channel_secret not in combined


def test_phase2_mutations_require_both_csrf_and_idempotency(authenticated):
    client, csrf = authenticated
    url = f"{_admin_base(client)}/soul"
    assert client.patch(url, json={"content": "safe"}).status_code == 403
    assert (
        client.patch(
            url,
            headers={"X-CSRF-Token": csrf},
            json={"content": "safe"},
        ).status_code
        == 400
    )


def test_phase2_is_admin_only_and_checks_authorization_before_provider(client, app, monkeypatch):
    with app.state.session_factory() as db:
        db.add(
            User(
                username="phase2-reader",
                password_hash=hash_password("phase two non admin password"),
                is_admin=False,
            )
        )
        db.commit()
    login = client.post(
        "/api/v1/auth/login",
        json={"username": "phase2-reader", "password": "phase two non admin password"},
    )
    assert login.status_code == 200
    monkeypatch.setattr(
        InMemoryHermesProvider,
        "list_models",
        AsyncMock(side_effect=AssertionError("non-admin reached Hermes")),
    )

    assert client.get(f"{_admin_base(client)}/models").status_code == 403


def test_unverified_exact_capability_returns_409(authenticated, monkeypatch):
    client, _ = authenticated
    monkeypatch.setattr(
        InMemoryHermesProvider,
        "capabilities",
        AsyncMock(
            return_value=CapabilitySet(
                protocol="mock",
                version="mock",
                source_sha="mock",
                methods=frozenset(),
                features=frozenset(),
            )
        ),
    )

    response = client.get(f"{_admin_base(client)}/models")
    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"


def test_profile_capability_set_exposes_only_exact_safe_contract(authenticated):
    client, _ = authenticated
    # Populate the scoped cache through a harmless read.
    assert client.get(f"{_admin_base(client)}/models").status_code == 200
    bootstrap = client.get("/api/v1/bootstrap").json()
    profile = next(
        item for item in bootstrap["profiles"] if item["technicalName"] == "control-dev"
    )
    capability = profile["capabilitySet"]

    assert set(capability) == {"protocol", "version", "sourceSha", "methods", "features"}
    assert "models.list" in capability["methods"]
    assert "models.set" in capability["methods"]
    assert all("token" not in key.casefold() for key in capability)
