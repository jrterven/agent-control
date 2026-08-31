from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from hermes_client import HermesRunReceipt, InMemoryHermesProvider
from hermes_control_api.models import (
    Automation,
    AutomationRun,
    IdempotencyOperation,
    ProfileRef,
    SessionLink,
    User,
)
from hermes_control_api.security import hash_password
from hermes_control_api.schemas import ProfileDelete, ProfileMove, SessionCreate
from hermes_control_api.api.routes import public_capability_flags
from hermes_control_api.services import (
    AutomationService,
    GatewayService,
    NotFoundError,
    ProfileService,
    SessionService,
    UpstreamUnavailableError,
    _AUDITED_PROFILE_TRANSFER_PAIRS,
    profile_lifecycle_guard,
)

from .conftest import mutation_headers


def test_lifecycle_capabilities_fail_closed_for_hermes_0205():
    flags = public_capability_flags(
        {
            "protocol": "dashboard-jsonrpc",
            "version": "0.20.5",
            "methods": [
                "profiles.delete",
                "profiles.export",
                "profiles.import",
                "profiles.transfer",
            ],
            "features": ["profiles"],
        },
        profile_name="default",
        mutable_profiles=["default"],
        trusted_source_sha_configured=True,
    )
    assert flags["profileDelete"] is False
    assert flags["profileTransfer"] is False
    assert flags["profileExport"] is False
    assert flags["profileImport"] is False
    other_0206 = public_capability_flags(
        {
            "protocol": "dashboard-jsonrpc",
            "version": "0.20.6",
            "methods": ["profiles.delete", "profiles.transfer"],
            "features": ["profiles"],
        },
        profile_name="default",
        mutable_profiles=["default"],
        trusted_source_sha_configured=True,
        trusted_source_sha="9978706e9303dbf990d90e744b131361449d73b9",
    )
    assert other_0206["profileDelete"] is True
    assert other_0206["profileTransfer"] is False
    exact_sha_required = public_capability_flags(
        {
            "protocol": "dashboard-jsonrpc",
            "version": "0.20.6",
            "methods": ["profiles.delete", "profiles.export", "profiles.import"],
            "features": ["profiles"],
        },
        profile_name="default",
        mutable_profiles=["default"],
        trusted_source_sha_configured=True,
    )
    assert exact_sha_required["profileDelete"] is False
    assert exact_sha_required["profileExport"] is False
    assert exact_sha_required["profileImport"] is False
    unsafe_0205 = "791e2ae3257e211d14ca77e654dfe10ee1976a1c"
    assert all(unsafe_0205 not in pair for pair in _AUDITED_PROFILE_TRANSFER_PAIRS)
    assert _AUDITED_PROFILE_TRANSFER_PAIRS == {
        (
            "4209d371aa1bb8840ce8447555bdd863a1a96c38",
            "4209d371aa1bb8840ce8447555bdd863a1a96c38",
        )
    }


def _create_agent(client, csrf: str, gateway_id: str, name: str) -> dict:
    response = client.post(
        "/api/v1/profiles",
        headers=mutation_headers(csrf, f"create-{name}-{gateway_id}"),
        json={
            "gatewayId": gateway_id,
            "technicalName": name,
            "displayName": name.replace("-", " ").title(),
            "description": "A managed test agent with a complete setup description.",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_destination_gateway(client, csrf: str, suffix: str) -> str:
    response = client.post(
        "/api/v1/gateways",
        headers=mutation_headers(csrf, f"create-destination-{suffix}"),
        json={
            "name": f"Destination {suffix}",
            "restUrl": f"http://127.0.0.1:{41000 + len(suffix)}",
            "wsUrl": f"ws://127.0.0.1:{41000 + len(suffix)}/api/ws",
            "connectionMode": "tunnel",
            "trustedSourceSha": "791e2ae3257e211d14ca77e654dfe10ee1976a1c",
        },
    )
    assert response.status_code == 201, response.text
    gateway_id = response.json()["id"]
    refreshed = client.post(
        f"/api/v1/profiles/refresh?gatewayId={gateway_id}",
        headers=mutation_headers(csrf, f"refresh-destination-{suffix}"),
    )
    assert refreshed.status_code == 200, refreshed.text
    return gateway_id


def test_delete_profile_requires_exact_confirmation_and_cleans_global_route_data(
    authenticated, app
):
    client, csrf = authenticated
    gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    agent = _create_agent(client, csrf, gateway_id, "delete-global")

    rejected = client.request(
        "DELETE",
        f"/api/v1/profiles/{agent['id']}",
        headers=mutation_headers(csrf, "delete-global-wrong-confirmation"),
        json={"confirmation": "Delete Global"},
    )
    assert rejected.status_code == 409

    with app.state.session_factory() as db:
        second_user = User(
            username="profile-delete-second-user",
            password_hash=hash_password("a sufficiently long test password"),
            is_admin=False,
        )
        db.add(second_user)
        db.flush()
        session = SessionLink(
            owner_id=second_user.id,
            gateway_id=gateway_id,
            profile_name="delete-global",
            stored_session_id="foreign-owner-session",
            status="idle",
        )
        automation = Automation(
            owner_id=second_user.id,
            gateway_id=gateway_id,
            profile_name="delete-global",
            hermes_automation_id="delete-global-cron",
            name="Completed cron",
            schedule="0 8 * * *",
            timezone="UTC",
            prompt="Run a completed deterministic task.",
            enabled=True,
        )
        db.add_all([session, automation])
        db.flush()
        run = AutomationRun(
            automation_id=automation.id,
            session_link_id=session.id,
            hermes_run_id="completed-run",
            status="completed",
        )
        operation = IdempotencyOperation(
            user_id=second_user.id,
            scope=f"session:{session.id}:prompt",
            idempotency_key="completed-prompt",
            status="completed",
            response_json={"ok": True},
        )
        db.add_all([run, operation])
        db.commit()
        run_id = run.id
        operation_id = operation.id

    headers = mutation_headers(csrf, "delete-global-confirmed")
    deleted = client.request(
        "DELETE",
        f"/api/v1/profiles/{agent['id']}",
        headers=headers,
        json={"confirmation": "delete-global"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"
    assert deleted.json()["deleted"] == {
        "sessions": 1,
        "automations": 1,
        "automationRuns": 1,
        "idempotencyOperations": 1,
    }
    replay = client.request(
        "DELETE",
        f"/api/v1/profiles/{agent['id']}",
        headers=headers,
        json={"confirmation": "delete-global"},
    )
    assert replay.status_code == 200
    assert replay.json() == deleted.json()

    with app.state.session_factory() as db:
        assert db.get(ProfileRef, agent["id"]) is None
        assert db.get(AutomationRun, run_id) is None
        assert db.get(IdempotencyOperation, operation_id) is None
        assert db.scalar(
            select(SessionLink.id).where(
                SessionLink.gateway_id == gateway_id,
                SessionLink.profile_name == "delete-global",
            )
        ) is None


def test_delete_reconciles_cancelled_response_after_hermes_accepted_it(
    authenticated, app, monkeypatch
):
    client, csrf = authenticated
    gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    agent = _create_agent(client, csrf, gateway_id, "delete-cancelled-response")
    original_delete = InMemoryHermesProvider.delete_profile

    async def accepted_then_cancelled(self, name: str) -> None:
        await original_delete(self, name)
        if name == "delete-cancelled-response":
            raise asyncio.CancelledError

    monkeypatch.setattr(
        InMemoryHermesProvider,
        "delete_profile",
        accepted_then_cancelled,
    )
    deleted = client.request(
        "DELETE",
        f"/api/v1/profiles/{agent['id']}",
        headers=mutation_headers(csrf, "delete-cancelled-response-confirmed"),
        json={"confirmation": "delete-cancelled-response"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["status"] == "deleted"
    with app.state.session_factory() as db:
        assert db.get(ProfileRef, agent["id"]) is None


def test_delete_replays_local_cleanup_after_transient_commit_failure(
    authenticated, app
):
    client, csrf = authenticated
    gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    agent = _create_agent(client, csrf, gateway_id, "delete-commit-retry")

    async def delete_with_flaky_commit():
        with app.state.session_factory() as db:
            actor = db.scalar(select(User).where(User.username == "admin"))
            assert actor is not None
            original_commit = db.commit
            failures = 0

            def flaky_commit() -> None:
                nonlocal failures
                if failures == 0:
                    failures += 1
                    raise RuntimeError("simulated transient cleanup commit failure")
                original_commit()

            db.commit = flaky_commit  # type: ignore[method-assign]
            outcome = await ProfileService(app.state.services).delete(
                db,
                actor,
                agent["id"],
                ProfileDelete(confirmation="delete-commit-retry"),
            )
            assert failures == 1
            return outcome

    outcome = client.portal.call(delete_with_flaky_commit)
    assert any("transient local cleanup" in item for item in outcome.warnings)
    with app.state.session_factory() as db:
        assert db.get(ProfileRef, agent["id"]) is None


def test_move_profile_preserves_control_identity_and_metadata(authenticated, app):
    client, csrf = authenticated
    source_gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    destination_gateway_id = _create_destination_gateway(client, csrf, "move-ok")
    agent = _create_agent(client, csrf, source_gateway_id, "move-preserved")
    with app.state.session_factory() as db:
        row = db.get(ProfileRef, agent["id"])
        assert row is not None
        row.avatar_mime_type = "image/png"
        row.avatar_data = b"preserved-avatar"
        original_description = row.description
        db.commit()

    moved = client.post(
        f"/api/v1/profiles/{agent['id']}/move",
        headers=mutation_headers(csrf, "move-preserved-confirmed"),
        json={
            "destinationGatewayId": destination_gateway_id,
            "confirmation": "move-preserved",
        },
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["status"] == "moved"
    assert moved.json()["sourceGatewayId"] == source_gateway_id
    assert moved.json()["destinationGatewayId"] == destination_gateway_id

    with app.state.session_factory() as db:
        row = db.get(ProfileRef, agent["id"])
        assert row is not None
        assert row.gateway_id == destination_gateway_id
        assert row.profile_name == "move-preserved"
        assert row.display_name == "Move Preserved"
        assert row.description == original_description
        assert row.avatar_data == b"preserved-avatar"
        assert db.scalar(
            select(ProfileRef.id).where(
                ProfileRef.gateway_id == source_gateway_id,
                ProfileRef.profile_name == "move-preserved",
            )
        ) is None


def test_move_reconciles_cancelled_source_delete_without_losing_destination(
    authenticated, app, monkeypatch
):
    client, csrf = authenticated
    source_gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    destination_gateway_id = _create_destination_gateway(
        client, csrf, "move-delete-cancelled"
    )
    agent = _create_agent(
        client, csrf, source_gateway_id, "move-delete-cancelled"
    )
    original_delete = InMemoryHermesProvider.delete_profile

    async def accepted_then_cancelled(self, name: str) -> None:
        await original_delete(self, name)
        if name == "move-delete-cancelled":
            raise asyncio.CancelledError

    monkeypatch.setattr(
        InMemoryHermesProvider,
        "delete_profile",
        accepted_then_cancelled,
    )
    moved = client.post(
        f"/api/v1/profiles/{agent['id']}/move",
        headers=mutation_headers(csrf, "move-delete-cancelled-confirmed"),
        json={
            "destinationGatewayId": destination_gateway_id,
            "confirmation": "move-delete-cancelled",
        },
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["destinationGatewayId"] == destination_gateway_id
    with app.state.session_factory() as db:
        row = db.get(ProfileRef, agent["id"])
        assert row is not None
        assert row.gateway_id == destination_gateway_id


def test_move_replays_local_cutover_after_transient_commit_failure(
    authenticated, app
):
    client, csrf = authenticated
    source_gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    destination_gateway_id = _create_destination_gateway(
        client, csrf, "move-commit-retry"
    )
    agent = _create_agent(client, csrf, source_gateway_id, "move-commit-retry")

    async def move_with_flaky_commit():
        with app.state.session_factory() as db:
            actor = db.scalar(select(User).where(User.username == "admin"))
            assert actor is not None
            original_commit = db.commit
            failures = 0

            def flaky_commit() -> None:
                nonlocal failures
                if failures == 0:
                    failures += 1
                    raise RuntimeError("simulated transient commit failure")
                original_commit()

            db.commit = flaky_commit  # type: ignore[method-assign]
            outcome = await ProfileService(app.state.services).move(
                db,
                actor,
                agent["id"],
                ProfileMove(
                    destination_gateway_id=destination_gateway_id,
                    confirmation="move-commit-retry",
                ),
            )
            assert failures == 1
            return outcome

    outcome = client.portal.call(move_with_flaky_commit)
    assert outcome.destination_gateway_id == destination_gateway_id
    assert any("transient local cutover" in item for item in outcome.warnings)
    with app.state.session_factory() as db:
        row = db.get(ProfileRef, agent["id"])
        assert row is not None
        assert row.gateway_id == destination_gateway_id


def test_move_does_not_publish_destination_when_source_delete_is_unknown(
    authenticated, app, monkeypatch
):
    client, csrf = authenticated
    source_gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    destination_gateway_id = _create_destination_gateway(
        client, csrf, "move-delete-unknown"
    )
    agent = _create_agent(client, csrf, source_gateway_id, "move-delete-unknown")
    original_delete = InMemoryHermesProvider.delete_profile
    original_list = InMemoryHermesProvider.list_profiles
    delete_sent = False

    async def ambiguous_delete(self, name: str) -> None:
        nonlocal delete_sent
        await original_delete(self, name)
        if name == "move-delete-unknown":
            delete_sent = True
            raise ConnectionError("delete response was lost")

    async def unavailable_reconcile(self):
        if delete_sent and self.connection.gateway_id == source_gateway_id:
            raise ConnectionError("source reconcile unavailable")
        return await original_list(self)

    monkeypatch.setattr(InMemoryHermesProvider, "delete_profile", ambiguous_delete)
    monkeypatch.setattr(InMemoryHermesProvider, "list_profiles", unavailable_reconcile)

    async def attempt_move() -> None:
        with app.state.session_factory() as db:
            actor = db.scalar(select(User).where(User.username == "admin"))
            assert actor is not None
            with pytest.raises(
                UpstreamUnavailableError, match="operator reconciliation"
            ):
                await ProfileService(app.state.services).move(
                    db,
                    actor,
                    agent["id"],
                    ProfileMove(
                        destination_gateway_id=destination_gateway_id,
                        confirmation="move-delete-unknown",
                    ),
                )

    client.portal.call(attempt_move)
    with app.state.session_factory() as db:
        row = db.get(ProfileRef, agent["id"])
        assert row is not None
        assert row.gateway_id == source_gateway_id


def test_move_accepts_verified_import_when_transfer_cleanup_raises(
    authenticated, app, monkeypatch
):
    client, csrf = authenticated
    source_gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    destination_gateway_id = _create_destination_gateway(
        client, csrf, "move-import-cleanup"
    )
    agent = _create_agent(client, csrf, source_gateway_id, "move-import-cleanup")
    original_transfer = InMemoryHermesProvider.transfer_profile_to

    async def imported_then_cleanup_failed(self, destination, *, name: str):
        await original_transfer(self, destination, name=name)
        raise RuntimeError("temporary archive cleanup failed after import")

    monkeypatch.setattr(
        InMemoryHermesProvider,
        "transfer_profile_to",
        imported_then_cleanup_failed,
    )
    moved = client.post(
        f"/api/v1/profiles/{agent['id']}/move",
        headers=mutation_headers(csrf, "move-import-cleanup-confirmed"),
        json={
            "destinationGatewayId": destination_gateway_id,
            "confirmation": "move-import-cleanup",
        },
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["destinationGatewayId"] == destination_gateway_id


def test_move_rolls_back_destination_when_imported_sessions_do_not_verify(
    authenticated, app
):
    client, csrf = authenticated
    source_gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    destination_gateway_id = _create_destination_gateway(client, csrf, "move-rollback")
    agent = _create_agent(client, csrf, source_gateway_id, "move-rollback-agent")
    session = client.post(
        "/api/v1/sessions",
        headers=mutation_headers(csrf, "create-move-rollback-session"),
        json={
            "gatewayId": source_gateway_id,
            "profileName": "move-rollback-agent",
            "title": "This session must survive a failed move",
        },
    )
    assert session.status_code == 201, session.text

    moved = client.post(
        f"/api/v1/profiles/{agent['id']}/move",
        headers=mutation_headers(csrf, "move-rollback-confirmed"),
        json={
            "destinationGatewayId": destination_gateway_id,
            "confirmation": "move-rollback-agent",
        },
    )
    # The HTTP idempotency boundary intentionally turns a failed post-import
    # mutation into a non-retryable reconcile response, even though the
    # service has already rolled the destination back.
    assert moved.status_code == 409, moved.text
    assert moved.json()["code"] == "MUTATION_DELIVERY_UNKNOWN"

    with app.state.session_factory() as db:
        row = db.get(ProfileRef, agent["id"])
        assert row is not None
        assert row.gateway_id == source_gateway_id
        linked = db.get(SessionLink, session.json()["id"])
        assert linked is not None
        assert linked.gateway_id == source_gateway_id
        assert db.scalar(
            select(ProfileRef.id).where(
                ProfileRef.gateway_id == destination_gateway_id,
                ProfileRef.profile_name == "move-rollback-agent",
            )
        ) is None


def test_profile_lifecycle_blocks_default_and_active_routes(authenticated, app):
    client, csrf = authenticated
    bootstrap = client.get("/api/v1/bootstrap").json()
    assert bootstrap["gateways"][0]["capabilities"]["profileImport"] is True
    assert bootstrap["gateways"][0]["capabilities"]["profileTransfer"] is True
    default_profile = next(
        item for item in bootstrap["profiles"] if item["technicalName"] == "default"
    )
    assert default_profile["capabilities"]["profileDelete"] is True
    assert default_profile["capabilities"]["profileExport"] is True
    blocked_default = client.request(
        "DELETE",
        f"/api/v1/profiles/{default_profile['id']}",
        headers=mutation_headers(csrf, "delete-default-blocked"),
        json={"confirmation": "default"},
    )
    assert blocked_default.status_code == 409

    gateway_id = bootstrap["gateways"][0]["id"]
    agent = _create_agent(client, csrf, gateway_id, "active-agent")
    with app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        db.add(
            SessionLink(
                owner_id=admin.id,
                gateway_id=gateway_id,
                profile_name="active-agent",
                stored_session_id="active-session",
                status="streaming",
            )
        )
        db.commit()

    blocked_active = client.request(
        "DELETE",
        f"/api/v1/profiles/{agent['id']}",
        headers=mutation_headers(csrf, "delete-active-blocked"),
        json={"confirmation": "active-agent"},
    )
    assert blocked_active.status_code == 409
    assert "active Control session" in blocked_active.json()["message"]


def test_move_rejects_an_unmanaged_agent_outside_the_operator_allowlist(
    authenticated, app
):
    client, csrf = authenticated
    source_gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    destination_gateway_id = _create_destination_gateway(
        client, csrf, "move-read-only"
    )
    agent = _create_agent(client, csrf, source_gateway_id, "read-only-agent")
    with app.state.session_factory() as db:
        row = db.get(ProfileRef, agent["id"])
        assert row is not None
        row.managed_by_control = False
        db.commit()

    blocked = client.post(
        f"/api/v1/profiles/{agent['id']}/move",
        headers=mutation_headers(csrf, "move-read-only-blocked"),
        json={
            "destinationGatewayId": destination_gateway_id,
            "confirmation": "read-only-agent",
        },
    )
    assert blocked.status_code == 409, blocked.text
    assert "not allowed" in blocked.json()["message"]


def test_stale_raw_route_waiter_cannot_mutate_source_after_cutover(
    authenticated, app
):
    client, csrf = authenticated
    source_gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    destination_gateway_id = _create_destination_gateway(
        client, csrf, "route-race"
    )
    agent = _create_agent(client, csrf, source_gateway_id, "route-race-agent")

    async def race_cutover_and_stale_create() -> None:
        async with profile_lifecycle_guard(
            app.state.services,
            {source_gateway_id, destination_gateway_id},
            profile_name="route-race-agent",
        ):
            with app.state.session_factory() as move_db:
                row = move_db.get(ProfileRef, agent["id"])
                assert row is not None
                row.gateway_id = destination_gateway_id
                move_db.commit()

            async def stale_create() -> None:
                with app.state.session_factory() as create_db:
                    actor = create_db.scalar(
                        select(User).where(User.username == "admin")
                    )
                    assert actor is not None
                    await SessionService(app.state.services).create(
                        create_db,
                        actor,
                        SessionCreate(
                            gateway_id=source_gateway_id,
                            profile_name="route-race-agent",
                            title="Must not be created on the stale source",
                        ),
                    )

            waiter = asyncio.create_task(stale_create())
            await asyncio.sleep(0)
            assert not waiter.done()
        with pytest.raises(NotFoundError, match="route no longer exists"):
            await waiter

    client.portal.call(race_cutover_and_stale_create)
    with app.state.session_factory() as db:
        assert db.scalar(
            select(SessionLink.id).where(
                SessionLink.gateway_id == source_gateway_id,
                SessionLink.profile_name == "route-race-agent",
            )
        ) is None


def test_stale_probe_waiter_cannot_recreate_source_provider_after_cutover(
    authenticated, app
):
    client, csrf = authenticated
    source_gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    destination_gateway_id = _create_destination_gateway(
        client, csrf, "probe-route-race"
    )
    agent = _create_agent(client, csrf, source_gateway_id, "probe-route-agent")

    async def race_cutover_and_stale_probe() -> None:
        async with profile_lifecycle_guard(
            app.state.services,
            {source_gateway_id, destination_gateway_id},
            profile_name="probe-route-agent",
        ):
            async def stale_probe() -> None:
                with app.state.session_factory() as probe_db:
                    actor = probe_db.scalar(
                        select(User).where(User.username == "admin")
                    )
                    assert actor is not None
                    await GatewayService(app.state.services).probe(
                        probe_db,
                        actor,
                        source_gateway_id,
                        "probe-route-agent",
                    )

            waiter = asyncio.create_task(stale_probe())
            await asyncio.sleep(0)
            assert not waiter.done()
            with app.state.session_factory() as move_db:
                row = move_db.get(ProfileRef, agent["id"])
                assert row is not None
                row.gateway_id = destination_gateway_id
                move_db.commit()
        with pytest.raises(NotFoundError, match="route no longer exists"):
            await waiter

    client.portal.call(race_cutover_and_stale_probe)


def test_lifecycle_preflight_waits_for_inflight_automation_watcher(
    authenticated, app
):
    client, csrf = authenticated
    gateway_id = client.get("/api/v1/bootstrap").json()["gateways"][0]["id"]
    agent = _create_agent(client, csrf, gateway_id, "watcher-lifecycle-race")
    with app.state.session_factory() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin is not None
        automation = Automation(
            owner_id=admin.id,
            gateway_id=gateway_id,
            profile_name="watcher-lifecycle-race",
            hermes_automation_id="watcher-cron",
            name="Watcher race",
            schedule="0 8 * * *",
            timezone="UTC",
            prompt="Create a terminal watcher receipt.",
            enabled=False,
        )
        db.add(automation)
        db.commit()
        automation_id = automation.id

    async def race_watcher_and_lifecycle() -> None:
        watcher_started = asyncio.Event()
        allow_watcher_to_finish = asyncio.Event()
        lifecycle_entered = asyncio.Event()

        class BlockingWatcherProvider:
            runtime_generation = "watcher-generation"

            def __init__(self) -> None:
                self.connection = type(
                    "Connection",
                    (),
                    {
                        "gateway_id": gateway_id,
                        "profile_name": "watcher-lifecycle-race",
                    },
                )()

            async def list_automation_runs(self, automation_id, *, limit=100):
                assert automation_id == "watcher-cron"
                watcher_started.set()
                await allow_watcher_to_finish.wait()
                return [
                    HermesRunReceipt(
                        run_id="watcher-terminal-run",
                        status="completed",
                        stored_session_id="watcher-terminal-session",
                    )
                ]

        async def reconcile() -> None:
            with app.state.session_factory() as reconcile_db:
                row = reconcile_db.get(Automation, automation_id)
                assert row is not None
                await AutomationService(
                    app.state.services
                ).reconcile_upstream_runs(
                    reconcile_db,
                    row,
                    provider=BlockingWatcherProvider(),
                )

        async def lifecycle_preflight() -> None:
            async with profile_lifecycle_guard(
                app.state.services,
                {gateway_id},
                profile_name="watcher-lifecycle-race",
            ):
                lifecycle_entered.set()
                with app.state.session_factory() as lifecycle_db:
                    profile = lifecycle_db.get(ProfileRef, agent["id"])
                    assert profile is not None
                    sessions, _, runs, _ = ProfileService._route_rows(
                        lifecycle_db, profile
                    )
                    assert {
                        item.stored_session_id for item in sessions
                    } == {"watcher-terminal-session"}
                    assert {item.hermes_run_id for item in runs} == {
                        "watcher-terminal-run"
                    }

        watcher_task = asyncio.create_task(reconcile())
        await watcher_started.wait()
        lifecycle_task = asyncio.create_task(lifecycle_preflight())
        await asyncio.sleep(0)
        assert not lifecycle_entered.is_set()
        allow_watcher_to_finish.set()
        await watcher_task
        await lifecycle_task

    client.portal.call(race_watcher_and_lifecycle)
