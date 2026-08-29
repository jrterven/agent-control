from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from hermes_client import (
    EventNormalizer,
    HermesGatewayProvider,
    HermesProfile,
    InMemoryHermesProvider,
    NormalizedEvent,
    ProviderConnection,
)
from hermes_control_api.models import Automation, Gateway, ProfileRef, User, utc_now
from hermes_control_api.realtime import persist_normalized_event
from hermes_control_api.services import GatewayService, require_capability
from hermes_control_api.supervision import SupervisorHealth, supervise_periodic

from .conftest import mutation_headers


@pytest.mark.asyncio
async def test_reconnect_supervisor_survives_persistent_event_sink_failure(caplog):
    delivered = 0
    fail_delivery = True
    recovered_states: list[str] = []

    async def unavailable_sqlite_sink(event: NormalizedEvent) -> None:
        nonlocal delivered, fail_delivery
        delivered += 1
        if fail_delivery:
            raise OperationalError(
                "INSERT redacted",
                {},
                sqlite3.OperationalError("database is temporarily unavailable"),
            )
        recovered_states.append(str(event.data.get("state")))

    class RecoveringRpc:
        connected = False
        generation = 1

        async def connect(self) -> None:
            self.connected = True
            self.generation += 1

        async def close(self) -> None:
            self.connected = False

    provider = HermesGatewayProvider(
        ProviderConnection("gateway-1", "control-dev", "http://x", "ws://x"),
        unavailable_sqlite_sink,
    )
    provider.rpc = RecoveringRpc()  # type: ignore[assignment]
    provider._ensure_supervisor()
    try:
        for _ in range(100):
            if provider.rpc.connected and delivered >= 2:
                break
            await asyncio.sleep(0.01)

        assert provider.rpc.connected
        assert delivered >= 2
        assert provider._supervisor is not None
        assert not provider._supervisor.done()
        fail_delivery = False
        for _ in range(150):
            if "connected" in recovered_states:
                break
            await asyncio.sleep(0.01)
        assert "connected" in recovered_states
        assert "database is temporarily unavailable" not in caplog.text
        assert "reconnect supervisor continues" in caplog.text
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_periodic_automation_supervisor_survives_table_error_and_recovers():
    health = SupervisorHealth(stale_after_seconds=1)
    second_attempt_started = asyncio.Event()
    permit_recovery = asyncio.Event()
    calls = 0

    async def reconcile_routes() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OperationalError(
                "SELECT automations",
                {},
                sqlite3.OperationalError("no such table: automations"),
            )
        second_attempt_started.set()
        await permit_recovery.wait()

    task = asyncio.create_task(
        supervise_periodic(
            reconcile_routes,
            health=health,
            interval_seconds=0.01,
        )
    )
    try:
        await asyncio.wait_for(second_attempt_started.wait(), timeout=1)
        failed = health.snapshot()
        assert failed["status"] == "failed"
        assert failed["consecutiveFailures"] == 1
        assert not task.done()

        permit_recovery.set()
        for _ in range(100):
            if health.snapshot()["status"] == "healthy":
                break
            await asyncio.sleep(0.01)
        recovered = health.snapshot()
        assert recovered["status"] == "healthy"
        assert recovered["consecutiveFailures"] == 0
        assert recovered["totalFailures"] == 1
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


def test_automation_route_pass_does_not_hide_local_database_errors(
    authenticated, app, monkeypatch
):
    _client, _csrf = authenticated
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        gateway = db.scalar(select(Gateway).where(Gateway.enabled.is_(True)))
        assert owner is not None and gateway is not None
        db.add(
            Automation(
                owner_id=owner.id,
                gateway_id=gateway.id,
                profile_name="control-dev",
                hermes_automation_id="db-failure-job",
                name="Database failure probe",
                schedule="0 9 * * *",
                timezone="UTC",
                prompt="Read-only reconciliation",
                enabled=True,
                next_runs=[],
            )
        )
        db.commit()

    async def database_unavailable(*_args, **_kwargs):
        raise OperationalError(
            "SELECT gateways",
            {},
            sqlite3.OperationalError("no such table: gateways"),
        )

    monkeypatch.setattr(GatewayService, "connection", database_unavailable)
    with pytest.raises(OperationalError, match="no such table"):
        _client.portal.call(app.state.warm_automation_routes_once)


def test_readiness_reports_watcher_failure_staleness_and_recovery(client, app):
    health = app.state.automation_route_health
    now = datetime.now(timezone.utc)

    health.mark_failure(at=now)
    failed = client.get("/api/v1/ready")
    assert failed.status_code == 200
    assert failed.json()["status"] == "degraded"
    assert failed.json()["automationRoutes"] == "failed"

    health.mark_success(at=now)
    recovered = client.get("/api/v1/ready")
    assert recovered.json()["status"] == "ready"
    assert recovered.json()["automationRoutes"] == "healthy"

    health.mark_success(
        at=now - timedelta(seconds=health.stale_after_seconds + 1)
    )
    stale = client.get("/api/v1/ready")
    assert stale.json()["status"] == "degraded"
    assert stale.json()["automationRoutes"] == "stale"


def test_capability_watcher_renews_expired_profile_permissions(authenticated, app):
    client, _csrf = authenticated
    with app.state.session_factory() as db:
        profile = db.scalar(
            select(ProfileRef).where(ProfileRef.profile_name == "default")
        )
        assert profile is not None
        profile.capabilities = {}
        profile.capabilities_checked_at = utc_now() - timedelta(
            seconds=app.state.services.settings.capability_ttl_seconds + 1
        )
        db.commit()

    expired = client.get("/api/v1/bootstrap")
    assert expired.status_code == 200
    newton = next(
        row for row in expired.json()["profiles"] if row["technicalName"] == "default"
    )
    assert newton["mutable"] is False
    assert newton["capabilities"]["prompts"] is False

    client.portal.call(app.state.warm_capabilities_once)

    renewed = client.get("/api/v1/bootstrap")
    assert renewed.status_code == 200
    newton = next(
        row for row in renewed.json()["profiles"] if row["technicalName"] == "default"
    )
    assert newton["mutable"] is True
    assert newton["capabilities"]["sessions"] is True
    assert newton["capabilities"]["prompts"] is True
    assert newton["capabilities"]["interrupt"] is True
    assert "approval.respond" in newton["capabilitySet"]["methods"]
    assert "clarify.respond" in newton["capabilitySet"]["methods"]


def test_readiness_reports_capability_watcher_failure_and_recovery(client, app):
    health = app.state.capability_refresh_health
    now = datetime.now(timezone.utc)

    health.mark_failure(at=now)
    failed = client.get("/api/v1/ready")
    assert failed.status_code == 200
    assert failed.json()["status"] == "degraded"
    assert failed.json()["capabilityRefresh"] == "failed"

    health.mark_success(at=now)
    recovered = client.get("/api/v1/ready")
    assert recovered.json()["status"] == "ready"
    assert recovered.json()["capabilityRefresh"] == "healthy"


def test_readiness_and_gateway_projections_expire_cached_upstream_health_by_ttl(
    authenticated, app
):
    client, _csrf = authenticated
    settings = app.state.services.settings
    with app.state.session_factory() as db:
        gateway = db.scalar(select(Gateway).where(Gateway.enabled.is_(True)))
        assert gateway is not None
        gateway.health_status = "online"
        gateway.last_health_at = utc_now() - timedelta(
            seconds=settings.upstream_health_ttl_seconds + 1
        )
        db.commit()

    stale = client.get("/api/v1/ready")
    assert stale.status_code == 200
    assert stale.json()["upstream"] == "stale"
    assert stale.json()["staleGateways"] == 1
    assert stale.json()["upstreamHealthTtlSeconds"] == settings.upstream_health_ttl_seconds
    gateways = client.get("/api/v1/gateways")
    assert gateways.status_code == 200
    assert gateways.json()[0]["healthStatus"] == "stale"
    bootstrap = client.get("/api/v1/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["gateways"][0]["status"] == "offline"

    with app.state.session_factory() as db:
        gateway = db.scalar(select(Gateway).where(Gateway.enabled.is_(True)))
        gateway.health_status = "online"
        gateway.last_health_at = utc_now()
        db.commit()

    fresh = client.get("/api/v1/ready")
    assert fresh.json()["upstream"] == "online"
    assert fresh.json()["staleGateways"] == 0
    assert client.get("/api/v1/gateways").json()[0]["healthStatus"] == "online"
    assert client.get("/api/v1/bootstrap").json()["gateways"][0]["status"] == "connected"


def test_profile_refresh_treats_successful_scoped_probe_as_online_when_list_status_missing(
    authenticated, app, monkeypatch
):
    client, csrf = authenticated
    gateway = client.get("/api/v1/gateways").json()[0]

    async def official_profiles_without_status(_provider):
        return [
            HermesProfile("default", "Newton", "unknown"),
            HermesProfile("jarvis", "Jarvis", "unknown"),
            HermesProfile("control-dev", "Control Dev", "unknown"),
        ]

    monkeypatch.setattr(
        InMemoryHermesProvider,
        "list_profiles",
        official_profiles_without_status,
    )
    refreshed = client.post(
        "/api/v1/profiles/refresh",
        params={"gatewayId": gateway["id"]},
        headers=mutation_headers(csrf, "unknown-status-profile-refresh"),
    )
    assert refreshed.status_code == 200, refreshed.text
    assert {row["status"] for row in refreshed.json()} == {"online"}
    assert client.get("/api/v1/ready").json()["upstream"] == "online"


def test_failed_on_demand_capability_probe_degrades_profile_and_readiness(
    authenticated, app, monkeypatch
):
    client, csrf = authenticated
    gateway = client.get("/api/v1/gateways").json()[0]

    async def all_configured_profiles(_provider):
        return [
            HermesProfile("default", "Newton", "unknown"),
            HermesProfile("jarvis", "Jarvis", "unknown"),
            HermesProfile("control-dev", "Control Dev", "unknown"),
        ]

    monkeypatch.setattr(
        InMemoryHermesProvider,
        "list_profiles",
        all_configured_profiles,
    )
    refreshed = client.post(
        "/api/v1/profiles/refresh",
        params={"gatewayId": gateway["id"]},
        headers=mutation_headers(csrf, "health-before-capability-failure"),
    )
    assert refreshed.status_code == 200
    assert client.get("/api/v1/ready").json()["upstream"] == "online"

    with app.state.session_factory() as db:
        profile = db.scalar(
            select(ProfileRef).where(
                ProfileRef.gateway_id == gateway["id"],
                ProfileRef.profile_name == "control-dev",
            )
        )
        assert profile is not None
        profile.capabilities = {}
        profile.capabilities_checked_at = None
        db.commit()

    async def unavailable_capabilities(_provider):
        raise OSError("simulated scoped capability outage")

    monkeypatch.setattr(
        InMemoryHermesProvider,
        "capabilities",
        unavailable_capabilities,
    )

    async def check_capability() -> None:
        with app.state.session_factory() as db:
            await require_capability(
                db,
                app.state.services,
                gateway_id=gateway["id"],
                profile_name="control-dev",
                method="gateway.ping",
            )

    with pytest.raises(OSError, match="simulated scoped capability outage"):
        client.portal.call(check_capability)

    ready = client.get("/api/v1/ready")
    assert ready.json()["upstream"] == "degraded"
    with app.state.session_factory() as db:
        profile = db.scalar(
            select(ProfileRef).where(
                ProfileRef.gateway_id == gateway["id"],
                ProfileRef.profile_name == "control-dev",
            )
        )
        assert profile.status == "degraded"
        assert profile.last_seen_at is not None
        assert profile.capabilities_checked_at is None


def test_gateway_connection_events_aggregate_profiles_without_last_writer_wins(
    client, app
):
    with app.state.session_factory() as db:
        gateway = db.scalar(select(Gateway).where(Gateway.enabled.is_(True)))
        assert gateway is not None
        gateway_id = gateway.id
        control_dev = db.scalar(
            select(ProfileRef).where(
                ProfileRef.gateway_id == gateway_id,
                ProfileRef.profile_name == "control-dev",
            )
        )
        assert control_dev is not None
        capability_clock = datetime(2024, 1, 2, tzinfo=timezone.utc)
        control_dev.capabilities = {"methods": ["prompt.submit"]}
        control_dev.capabilities_checked_at = capability_clock
        db.commit()

    def connection(profile_name: str, state: str) -> None:
        persist_normalized_event(
            app.state.session_factory,
            NormalizedEvent.create(
                type="control.connection",
                gateway_id=gateway_id,
                profile_name=profile_name,
                data={"state": state, "attempt": 0},
            ),
            gateway_health_ttl_seconds=app.state.services.settings.upstream_health_ttl_seconds,
        )

    connection("default", "connected")
    assert client.get("/api/v1/ready").json()["upstream"] == "degraded"
    connection("jarvis", "connected")
    connection("control-dev", "connected")
    assert client.get("/api/v1/ready").json()["upstream"] == "online"

    # A single offline profile must not overwrite two live profiles.
    connection("default", "offline")
    assert client.get("/api/v1/ready").json()["upstream"] == "degraded"
    connection("jarvis", "offline")
    assert client.get("/api/v1/ready").json()["upstream"] == "degraded"
    connection("control-dev", "offline")
    assert client.get("/api/v1/ready").json()["upstream"] == "offline"

    with app.state.session_factory() as db:
        gateway = db.get(Gateway, gateway_id)
        assert gateway is not None
        assert gateway.health_status == "offline"
        assert gateway.last_health_at is not None
        control_dev = db.scalar(
            select(ProfileRef).where(
                ProfileRef.gateway_id == gateway_id,
                ProfileRef.profile_name == "control-dev",
            )
        )
        assert control_dev is not None
        assert control_dev.status == "offline"
        assert control_dev.last_seen_at is not None
        # Connectivity cannot extend a capability assertion's independent TTL.
        checked_at = control_dev.capabilities_checked_at
        assert checked_at is not None
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        assert checked_at == capability_clock
        profile_count = len(
            list(
                db.scalars(
                    select(ProfileRef).where(ProfileRef.gateway_id == gateway_id)
                ).all()
            )
        )
        last_health_at = gateway.last_health_at

    # A route Hermes never configured/discovered cannot create or affect
    # aggregate health.
    connection("untrusted-unknown-profile", "connected")
    with app.state.session_factory() as db:
        gateway = db.get(Gateway, gateway_id)
        assert gateway.health_status == "offline"
        assert gateway.last_health_at == last_health_at
        assert len(
            list(
                db.scalars(
                    select(ProfileRef).where(ProfileRef.gateway_id == gateway_id)
                ).all()
            )
        ) == profile_count

    forged = EventNormalizer(
        gateway_id=gateway_id,
        profile_name="control-dev",
    ).normalize(
        {"method": "control.connection", "params": {"state": "connected"}}
    )
    assert forged.type == "hermes.unknown"
    persist_normalized_event(app.state.session_factory, forged)
    with app.state.session_factory() as db:
        assert db.get(Gateway, gateway_id).health_status == "offline"
