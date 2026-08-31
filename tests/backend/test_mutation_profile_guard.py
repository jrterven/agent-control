from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from hermes_client import CapabilitySet, InMemoryHermesProvider
from hermes_control_api.config import Settings
from hermes_control_api.models import Gateway, GatewayCredential, ProfileRef
from hermes_control_api.services import (
    ConflictError,
    GatewayService,
    INTERACTIVE_MUTATION_CAPABILITIES,
    UPSTREAM_MUTATION_CAPABILITIES,
    capabilities_for_profile,
    require_capability,
    require_mutable_profile,
)

from .conftest import mutation_headers


def test_trusted_source_sha_is_exact_and_normalized():
    sha = "A" * 40
    assert Settings(environment="test", hermes_source_sha=sha).hermes_source_sha == sha.lower()

    for invalid in ("abc", "g" * 40, "a" * 39, "a" * 41):
        with pytest.raises(ValidationError, match="40 hexadecimal"):
            Settings(environment="test", hermes_source_sha=invalid)


def test_mutable_profile_allowlist_defaults_full_and_parses_operator_value():
    assert Settings(environment="test").mutable_profiles == [
        "default",
        "jarvis",
        "control-dev",
    ]
    assert Settings(environment="test").interactive_profiles == [
        "default",
        "jarvis",
        "control-dev",
    ]
    configured = Settings(
        environment="test",
        mutable_profiles="staging, control-dev, staging",  # type: ignore[arg-type]
        interactive_profiles="default, jarvis, default",  # type: ignore[arg-type]
    )
    assert configured.mutable_profiles == ["staging", "control-dev"]
    assert configured.interactive_profiles == ["default", "jarvis"]
    with pytest.raises(ValidationError):
        Settings(environment="test", capability_ttl_seconds=0)


def test_environment_seed_revokes_existing_trust_when_setting_is_absent(
    authenticated, app
):
    client, _ = authenticated
    expected_anchor = app.state.services.settings.hermes_source_sha
    assert expected_anchor is not None
    app.state.services.settings.hermes_source_sha = None
    with app.state.session_factory() as db:
        gateway = db.query(Gateway).filter(Gateway.env_managed.is_(True)).one()
        profile = db.query(ProfileRef).filter_by(
            gateway_id=gateway.id, profile_name="control-dev"
        ).one()
        gateway.source_sha = "b" * 40
        gateway.health_status = "online"
        gateway.last_health_at = datetime.now(timezone.utc)
        profile.capabilities = {"methods": ["prompt.submit"]}
        profile.capabilities_checked_at = datetime.now(timezone.utc)
        profile.status = "online"
        profile.last_seen_at = datetime.now(timezone.utc)
        db.commit()
        GatewayService(app.state.services).seed_environment_gateway(db)
        assert gateway.source_sha is None
        assert gateway.health_status == "unknown"
        assert profile.capabilities == {}
        assert profile.capabilities_checked_at is None
        credential = db.query(GatewayCredential).filter_by(gateway_id=gateway.id).one()
        assert credential.trusted_source_sha_ciphertext is None
        gateway_id = gateway.id
        profile_id = profile.id

    bootstrap = client.get("/api/v1/bootstrap")
    assert bootstrap.status_code == 200
    gateway_view = next(
        item for item in bootstrap.json()["gateways"] if item["id"] == gateway_id
    )
    profile_view = next(
        item for item in bootstrap.json()["profiles"] if item["id"] == profile_id
    )
    assert gateway_view["hasTrustedSourceSha"] is False
    assert profile_view["mutable"] is False


def test_environment_connection_change_invalidates_cached_capabilities(
    authenticated, app
):
    next_anchor = "c" * 40
    now = datetime.now(timezone.utc)
    with app.state.session_factory() as db:
        gateway = db.query(Gateway).filter(Gateway.env_managed.is_(True)).one()
        profile = db.query(ProfileRef).filter_by(
            gateway_id=gateway.id, profile_name="control-dev"
        ).one()
        gateway.source_sha = app.state.services.settings.hermes_source_sha
        profile.capabilities = {"methods": ["prompt.submit"]}
        profile.capabilities_checked_at = now
        profile.last_seen_at = now
        profile.status = "online"
        db.commit()

        app.state.services.settings.hermes_source_sha = next_anchor
        GatewayService(app.state.services).seed_environment_gateway(db)

        assert profile.capabilities == {}
        assert profile.capabilities_checked_at is None
        assert profile.last_seen_at is None
        assert profile.status == "unknown"
        assert gateway.source_sha is None
        assert gateway.health_status == "unknown"

        # Reseeding unchanged settings preserves a freshly certified cache,
        # while a diagnostic SHA distinct from the trust anchor stays public.
        profile.capabilities = {"methods": ["prompt.submit"]}
        profile.capabilities_checked_at = now
        profile.last_seen_at = now
        profile.status = "online"
        gateway.source_sha = "d" * 40
        db.commit()
        GatewayService(app.state.services).seed_environment_gateway(db)
        assert profile.capabilities == {"methods": ["prompt.submit"]}
        assert profile.capabilities_checked_at is not None
        assert gateway.source_sha == "d" * 40


def test_operator_sha_trust_is_write_only_revocable_and_gateway_scoped(
    authenticated, app
):
    client, csrf = authenticated
    environment_sha = "c" * 40
    reported_sha = "d" * 40
    manual_sha = "E" * 40
    app.state.services.settings.hermes_source_sha = environment_sha
    with app.state.session_factory() as db:
        GatewayService(app.state.services).seed_environment_gateway(db)
        canonical = db.query(Gateway).filter(Gateway.env_managed.is_(True)).one()
        canonical_id = canonical.id

    created = client.post(
        "/api/v1/gateways",
        headers=mutation_headers(csrf, "manual-gateway-no-trust"),
        json={
            "name": "Second gateway",
            "restUrl": "http://127.0.0.1:29119",
            "wsUrl": "ws://127.0.0.1:29119/api/ws",
            "connectionMode": "tunnel",
        },
    )
    assert created.status_code == 201, created.text
    manual_id = created.json()["id"]
    assert created.json()["hasTrustedSourceSha"] is False

    # A server-reported SHA is diagnostic only and must never become trust.
    with app.state.session_factory() as db:
        manual = db.get(Gateway, manual_id)
        manual.source_sha = reported_sha
        db.add(
            ProfileRef(
                gateway_id=manual_id,
                profile_name="control-dev",
                display_name="control-dev",
                status="online",
                capabilities={"methods": ["prompt.submit"]},
            )
        )
        db.commit()

    untrusted_bootstrap = client.get("/api/v1/bootstrap").json()
    untrusted_profile = next(
        item
        for item in untrusted_bootstrap["profiles"]
        if item["gatewayId"] == manual_id
        and item["technicalName"] == "control-dev"
    )
    assert untrusted_profile["mutable"] is False
    assert "prompt.submit" not in untrusted_profile["capabilitySet"]["methods"]

    async def load_connections():
        with app.state.session_factory() as db:
            service = GatewayService(app.state.services)
            return (
                await service.connection(db, canonical_id, "control-dev"),
                await service.connection(db, manual_id, "control-dev"),
            )

    canonical_connection, manual_connection = client.portal.call(load_connections)
    assert canonical_connection.trusted_source_sha == environment_sha
    assert manual_connection.trusted_source_sha is None

    configured = client.patch(
        f"/api/v1/gateways/{manual_id}",
        headers=mutation_headers(csrf, "manual-gateway-add-trust"),
        json={"trustedSourceSha": manual_sha},
    )
    assert configured.status_code == 200, configured.text
    assert configured.json()["hasTrustedSourceSha"] is True
    assert manual_sha.lower() not in configured.text.lower()

    _, trusted_manual_connection = client.portal.call(load_connections)
    assert trusted_manual_connection.trusted_source_sha == manual_sha.lower()
    with app.state.session_factory() as db:
        credential = db.query(GatewayCredential).filter_by(gateway_id=manual_id).one()
        assert credential.trusted_source_sha_ciphertext
        assert manual_sha.lower() not in credential.trusted_source_sha_ciphertext.lower()
        profile = db.query(ProfileRef).filter_by(
            gateway_id=manual_id, profile_name="control-dev"
        ).one()
        assert profile.capabilities == {}
        assert profile.status == "unknown"
        assert db.get(Gateway, manual_id).source_sha is None
        profile.capabilities = {"methods": ["prompt.submit"]}
        profile.capabilities_checked_at = datetime.now(timezone.utc)
        db.commit()

    listed = client.get("/api/v1/gateways")
    bootstrapped = client.get("/api/v1/bootstrap")
    assert manual_sha.lower() not in listed.text.lower()
    assert manual_sha.lower() not in bootstrapped.text.lower()
    assert next(
        item for item in bootstrapped.json()["gateways"] if item["id"] == manual_id
    )["hasTrustedSourceSha"] is True
    configured_profile = next(
        item
        for item in bootstrapped.json()["profiles"]
        if item["gatewayId"] == manual_id
        and item["technicalName"] == "control-dev"
    )
    assert configured_profile["mutable"] is True
    assert "prompt.submit" in configured_profile["capabilitySet"]["methods"]

    revoked = client.patch(
        f"/api/v1/gateways/{manual_id}",
        headers=mutation_headers(csrf, "manual-gateway-revoke-trust"),
        json={"trustedSourceSha": None},
    )
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["hasTrustedSourceSha"] is False
    _, revoked_connection = client.portal.call(load_connections)
    assert revoked_connection.trusted_source_sha is None
    revoked_profile = next(
        item
        for item in client.get("/api/v1/bootstrap").json()["profiles"]
        if item["gatewayId"] == manual_id
        and item["technicalName"] == "control-dev"
    )
    assert revoked_profile["mutable"] is False
    assert "prompt.submit" not in revoked_profile["capabilitySet"]["methods"]


@pytest.mark.parametrize("invalid", ["abc", "g" * 40, "a" * 39, "a" * 41, "   "])
def test_manual_gateway_rejects_non_exact_trusted_sha(authenticated, invalid: str):
    client, csrf = authenticated
    response = client.post(
        "/api/v1/gateways",
        headers=mutation_headers(csrf, f"invalid-manual-sha-{len(invalid)}-{invalid[:1]}"),
        json={
            "name": f"Invalid SHA {len(invalid)} {invalid[:1]}",
            "restUrl": "http://127.0.0.1:29119",
            "wsUrl": "ws://127.0.0.1:29119/api/ws",
            "connectionMode": "tunnel",
            "trustedSourceSha": invalid,
        },
    )
    assert response.status_code == 422


def test_write_only_trusted_sha_is_never_projected_when_upstream_reports_none(
    authenticated, monkeypatch
):
    client, csrf = authenticated
    trusted_sha = "f" * 40

    async def capabilities_without_reported_sha(_provider):
        return CapabilitySet(
            protocol="dashboard-jsonrpc",
            version="0.20.6",
            source_sha=None,
            methods=frozenset({"gateway.ping", "session.list"}),
        )

    monkeypatch.setattr(
        InMemoryHermesProvider,
        "capabilities",
        capabilities_without_reported_sha,
    )
    created = client.post(
        "/api/v1/gateways",
        headers=mutation_headers(csrf, "unreported-sha-gateway"),
        json={
            "name": "Unreported SHA gateway",
            "restUrl": "http://127.0.0.1:39119",
            "wsUrl": "ws://127.0.0.1:39119/api/ws",
            "connectionMode": "tunnel",
            "trustedSourceSha": trusted_sha,
        },
    )
    assert created.status_code == 201, created.text
    gateway_id = created.json()["id"]
    assert created.json()["sourceSha"] is None
    # Simulate an old deployment that copied the trust anchor into public
    # diagnostics; the next successful probe must scrub it when Hermes omits
    # a reported revision.
    with client.app.state.session_factory() as db:
        db.get(Gateway, gateway_id).source_sha = trusted_sha
        db.commit()

    probed = client.post(
        f"/api/v1/gateways/{gateway_id}/probe",
        params={"profileName": "control-dev"},
        headers=mutation_headers(csrf, "probe-unreported-sha"),
    )
    diagnostics = client.get(
        "/api/v1/diagnostics/capabilities",
        params={"gatewayId": gateway_id, "profileName": "control-dev"},
    )
    bootstrap = client.get("/api/v1/bootstrap")

    assert probed.status_code == 200, probed.text
    assert diagnostics.status_code == 200, diagnostics.text
    assert bootstrap.status_code == 200, bootstrap.text
    assert probed.json()["sourceSha"] is None
    assert diagnostics.json()["sourceSha"] is None
    gateway = next(
        item for item in bootstrap.json()["gateways"] if item["id"] == gateway_id
    )
    assert gateway["sha"] is None
    for response in (created, probed, diagnostics, bootstrap):
        assert trusted_sha not in response.text.lower()


@pytest.mark.parametrize("profile_name", ["default", "jarvis"])
def test_operator_can_restrict_every_upstream_mutation_to_control_dev(
    profile_name: str,
):
    for capability in UPSTREAM_MUTATION_CAPABILITIES:
        with pytest.raises(ConflictError, match="operator"):
            require_mutable_profile(profile_name, capability, ["control-dev"])


@pytest.mark.parametrize("profile_name", ["default", "jarvis"])
def test_interactive_allowlist_authorizes_only_conversation_methods(profile_name: str):
    for capability in INTERACTIVE_MUTATION_CAPABILITIES:
        require_mutable_profile(
            profile_name,
            capability,
            ["control-dev"],
            ["default", "jarvis", "control-dev"],
        )
    for capability in UPSTREAM_MUTATION_CAPABILITIES - INTERACTIVE_MUTATION_CAPABILITIES:
        with pytest.raises(ConflictError, match="operator"):
            require_mutable_profile(
                profile_name,
                capability,
                ["control-dev"],
                ["default", "jarvis", "control-dev"],
            )


def test_capability_projection_strips_writes_only_for_read_only_profiles():
    advertised = CapabilitySet(
        protocol="test",
        version="0.20.6",
        source_sha="a" * 40,
        methods=frozenset(
            {"session.list", "models.list", *UPSTREAM_MUTATION_CAPABILITIES}
        ),
        features=frozenset({"streaming"}),
    )

    for profile_name in ("default", "jarvis"):
        projected = capabilities_for_profile(
            advertised,
            profile_name,
            ["control-dev"],
            trusted_source_sha_configured=True,
        )
        assert projected.methods == frozenset({"session.list", "models.list"})
        assert projected.source_sha == advertised.source_sha
        assert projected.features == advertised.features

    assert capabilities_for_profile(
        advertised,
        "control-dev",
        ["control-dev"],
        trusted_source_sha_configured=True,
    ) == advertised

    interactive = capabilities_for_profile(
        advertised,
        "default",
        ["control-dev"],
        interactive_profiles=["default", "jarvis", "control-dev"],
        trusted_source_sha_configured=True,
    )
    assert INTERACTIVE_MUTATION_CAPABILITIES.issubset(interactive.methods)
    assert interactive.methods.isdisjoint(
        UPSTREAM_MUTATION_CAPABILITIES - INTERACTIVE_MUTATION_CAPABILITIES
    )
    assert capabilities_for_profile(
        advertised,
        "control-dev",
        ["control-dev"],
        trusted_source_sha_configured=False,
    ).methods == frozenset({"session.list", "models.list"})
    assert capabilities_for_profile(
        advertised,
        "staging",
        ["staging"],
        trusted_source_sha_configured=True,
    ) == advertised


def test_operator_allowlist_can_select_custom_profile_without_enabling_8642(
    authenticated, app
):
    client, csrf = authenticated
    app.state.services.settings.mutable_profiles = ["staging"]
    app.state.services.settings.interactive_profiles = []
    with app.state.session_factory() as db:
        gateway = db.query(Gateway).filter(Gateway.env_managed.is_(True)).one()
        db.add(
            ProfileRef(
                gateway_id=gateway.id,
                profile_name="staging",
                display_name="Staging",
                status="online",
                capabilities={"methods": ["session.create"]},
                last_seen_at=datetime.now(timezone.utc),
                capabilities_checked_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
        gateway_id = gateway.id

    created = client.post(
        "/api/v1/sessions",
        headers=mutation_headers(csrf, "custom-mutable-profile"),
        json={
            "gatewayId": gateway_id,
            "profileName": "staging",
            "title": "Custom mutable profile",
        },
    )
    blocked_default = client.post(
        "/api/v1/sessions",
        headers=mutation_headers(csrf, "default-no-longer-mutable"),
        json={
            "gatewayId": gateway_id,
            "profileName": "control-dev",
            "title": "Not allowlisted",
        },
    )
    assert created.status_code == 201, created.text
    assert blocked_default.status_code == 409

    bootstrap = client.get("/api/v1/bootstrap").json()
    staging = next(
        item for item in bootstrap["profiles"] if item["technicalName"] == "staging"
    )
    control_dev = next(
        item
        for item in bootstrap["profiles"]
        if item["technicalName"] == "control-dev"
    )
    assert staging["mutable"] is True
    assert control_dev["mutable"] is False

    async def staging_connection():
        with app.state.session_factory() as db:
            return await GatewayService(app.state.services).connection(
                db, gateway_id, "staging"
            )

    connection = client.portal.call(staging_connection)
    assert connection.api_url is None
    assert connection.api_key is None


def test_control_managed_authorization_is_scoped_to_gateway_route(
    authenticated, app
):
    client, csrf = authenticated
    app.state.services.settings.mutable_profiles = ["control-dev"]
    app.state.services.settings.interactive_profiles = ["default"]
    with app.state.session_factory() as db:
        primary_gateway = db.query(Gateway).filter(Gateway.env_managed.is_(True)).one()
        primary_gateway_id = primary_gateway.id

    secondary = client.post(
        "/api/v1/gateways",
        headers=mutation_headers(csrf, "create-homonym-secondary-gateway"),
        json={
            "name": "Homonym secondary gateway",
            "restUrl": "http://127.0.0.1:49119",
            "wsUrl": "ws://127.0.0.1:49119/api/ws",
            "connectionMode": "tunnel",
            "trustedSourceSha": "e" * 40,
        },
    )
    assert secondary.status_code == 201, secondary.text
    secondary_gateway_id = secondary.json()["id"]

    now = datetime.now(timezone.utc)
    advertised = {
        "protocol": "dashboard-jsonrpc",
        "version": "mock-1",
        "sourceSha": "in-memory",
        "methods": ["profiles.create"],
        "features": [],
    }
    with app.state.session_factory() as db:
        db.add_all(
            [
                ProfileRef(
                    gateway_id=primary_gateway_id,
                    profile_name="homonymous-managed",
                    display_name="Managed on primary",
                    managed_by_control=True,
                    status="online",
                    capabilities=advertised,
                    last_seen_at=now,
                    capabilities_checked_at=now,
                ),
                ProfileRef(
                    gateway_id=secondary_gateway_id,
                    profile_name="homonymous-managed",
                    display_name="Unmanaged on secondary",
                    managed_by_control=False,
                    status="online",
                    capabilities=advertised,
                    last_seen_at=now,
                    capabilities_checked_at=now,
                ),
            ]
        )
        db.commit()
        GatewayService(app.state.services).seed_environment_gateway(db)

    assert app.state.services.settings.mutable_profiles == ["control-dev"]
    assert app.state.services.settings.interactive_profiles == ["default"]
    bootstrap_profiles = [
        item
        for item in client.get("/api/v1/bootstrap").json()["profiles"]
        if item["technicalName"] == "homonymous-managed"
    ]
    by_gateway = {item["gatewayId"]: item for item in bootstrap_profiles}
    assert by_gateway[primary_gateway_id]["mutable"] is True
    assert by_gateway[primary_gateway_id]["capabilities"]["profileCreate"] is True
    assert by_gateway[secondary_gateway_id]["mutable"] is False
    assert by_gateway[secondary_gateway_id]["capabilities"]["profileCreate"] is False

    async def require_profile_create(gateway_id: str):
        with app.state.session_factory() as db:
            await require_capability(
                db,
                app.state.services,
                gateway_id=gateway_id,
                profile_name="homonymous-managed",
                method="profiles.create",
            )

    client.portal.call(require_profile_create, primary_gateway_id)
    with pytest.raises(ConflictError, match="operator"):
        client.portal.call(require_profile_create, secondary_gateway_id)


def test_stale_capability_cache_never_announces_mutability(authenticated, app):
    client, _ = authenticated
    with app.state.session_factory() as db:
        gateway = db.query(Gateway).filter(Gateway.env_managed.is_(True)).one()
        profile = db.query(ProfileRef).filter_by(
            gateway_id=gateway.id, profile_name="control-dev"
        ).one()
        profile.capabilities = {
            "protocol": "dashboard-jsonrpc",
            "methods": ["session.create", "prompt.submit"],
            "features": ["streaming"],
        }
        profile.capabilities_checked_at = datetime.now(timezone.utc) - timedelta(
            seconds=app.state.services.settings.capability_ttl_seconds + 1
        )
        profile.last_seen_at = datetime.now(timezone.utc) - timedelta(
            seconds=app.state.services.settings.upstream_health_ttl_seconds + 1
        )
        profile.status = "online"
        db.commit()
        gateway_id = gateway.id

    bootstrap = client.get("/api/v1/bootstrap").json()
    listed = client.get(
        "/api/v1/profiles", params={"gatewayId": gateway_id}
    )
    assert listed.status_code == 200, listed.text
    bootstrap_profile = next(
        item
        for item in bootstrap["profiles"]
        if item["gatewayId"] == gateway_id
        and item["technicalName"] == "control-dev"
    )
    listed_profile = next(
        item for item in listed.json() if item["profileName"] == "control-dev"
    )
    assert bootstrap_profile["mutable"] is False
    assert bootstrap_profile["status"] == "offline"
    assert bootstrap_profile["capabilitySet"]["methods"] == []
    assert bootstrap_profile["capabilities"]["prompts"] is False
    assert listed_profile["mutable"] is False
    assert listed_profile["status"] == "stale"
    assert listed_profile["capabilitySet"]["methods"] == []
    assert listed_profile["capabilities"]["methods"] == []


@pytest.mark.parametrize("profile_name", ["default", "jarvis"])
def test_session_create_guard_runs_before_provider(
    authenticated, app, monkeypatch, profile_name: str
):
    client, csrf = authenticated
    app.state.services.settings.mutable_profiles = ["control-dev"]
    app.state.services.settings.interactive_profiles = []
    forbidden = AsyncMock(side_effect=AssertionError("mutation reached provider"))
    monkeypatch.setattr(InMemoryHermesProvider, "create_session", forbidden)
    gateway_id = client.get("/api/v1/gateways").json()[0]["id"]

    response = client.post(
        "/api/v1/sessions",
        headers=mutation_headers(csrf, f"blocked-{profile_name}"),
        json={
            "gatewayId": gateway_id,
            "profileName": profile_name,
            "title": "must remain read-only",
        },
    )

    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"
    forbidden.assert_not_awaited()


def test_bootstrap_announces_verified_admin_writes_for_newton_and_jarvis(
    authenticated, app
):
    client, _ = authenticated
    now = datetime.now(timezone.utc)
    with app.state.session_factory() as db:
        for profile_name in ("default", "jarvis"):
            profile = db.query(ProfileRef).filter_by(profile_name=profile_name).one()
            profile.status = "online"
            profile.last_seen_at = now
            profile.capabilities_checked_at = now
            profile.capabilities = {
                "protocol": "dashboard-jsonrpc",
                "version": "0.20.5",
                "methods": sorted(UPSTREAM_MUTATION_CAPABILITIES),
                "features": [],
            }
        db.commit()

    by_name = {
        item["technicalName"]: item
        for item in client.get("/api/v1/bootstrap").json()["profiles"]
    }
    for profile_name in ("default", "jarvis"):
        methods = set(by_name[profile_name]["capabilitySet"]["methods"])
        assert UPSTREAM_MUTATION_CAPABILITIES.issubset(methods)
        assert by_name[profile_name]["mutable"] is True
