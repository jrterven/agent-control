from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient
from sqlalchemy import select

from hermes_client import CapabilitySet, HermesAutomation, HermesGatewayProvider, InMemoryHermesProvider
from hermes_control_api.models import Automation, AutomationRun, Gateway, ProfileRef, User
from hermes_control_api.services import GatewayService

from .conftest import mutation_headers


def gateway_id(client: TestClient) -> str:
    response = client.get("/api/v1/gateways")
    assert response.status_code == 200
    return response.json()[0]["id"]


def automation_payload(client: TestClient, **changes) -> dict:
    payload = {
        "gatewayId": gateway_id(client),
        "profileName": "control-dev",
        "name": "Morning brief",
        "schedule": "0 9 * * MON-FRI",
        "timezone": "America/Mexico_City",
        "prompt": "Prepare the morning brief",
        "enabled": True,
    }
    payload.update(changes)
    return payload


def create_automation(client: TestClient, csrf: str, key: str = "automation-create") -> dict:
    response = client.post(
        "/api/v1/automations",
        headers=mutation_headers(csrf, key),
        json=automation_payload(client),
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_startup_watcher_attaches_saved_automation_route_without_browser(
    authenticated, app
):
    client, _ = authenticated
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        gateway = db.scalar(select(Gateway).where(Gateway.env_managed.is_(True)))
        assert owner is not None and gateway is not None
        db.add(
            Automation(
                owner_id=owner.id,
                gateway_id=gateway.id,
                profile_name="control-dev",
                hermes_automation_id="persisted-job",
                name="Persisted watcher job",
                schedule="0 9 * * *",
                timezone="UTC",
                prompt="Read-only startup listener",
                enabled=True,
                next_runs=[],
            )
        )
        db.commit()

    client.portal.call(app.state.warm_automation_routes_once)

    assert (gateway.id, "control-dev") in app.state.services.provider_pool._providers


def test_startup_watcher_imports_unattended_hermes_run_session(authenticated, app):
    client, _ = authenticated
    with app.state.session_factory() as db:
        owner = db.scalar(select(User).where(User.username == "admin"))
        gateway = db.scalar(select(Gateway).where(Gateway.env_managed.is_(True)))
        assert owner is not None and gateway is not None
        row = Automation(
            owner_id=owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            hermes_automation_id="unattended-job",
            name="Unattended",
            schedule="0 9 * * *",
            timezone="UTC",
            prompt="Run while no browser is open",
            enabled=True,
            next_runs=[],
        )
        db.add(row)
        db.commit()
        automation_id = row.id

    async def seed_upstream_run():
        with app.state.session_factory() as db:
            connection = await GatewayService(app.state.services).connection(
                db, gateway.id, "control-dev"
            )
        provider = await app.state.services.provider_pool.get(connection)
        await provider.create_automation(
            HermesAutomation(
                "unattended-job",
                "Unattended",
                "0 9 * * *",
                "UTC",
                True,
                "Run while no browser is open",
            )
        )
        first = await provider.trigger_automation("unattended-job")
        second = await provider.trigger_automation("unattended-job")
        return [first, second]

    receipts = client.portal.call(seed_upstream_run)
    client.portal.call(app.state.warm_automation_routes_once)

    with app.state.session_factory() as db:
        runs = list(
            db.scalars(
                select(AutomationRun)
                .where(AutomationRun.automation_id == automation_id)
                .order_by(AutomationRun.created_at.desc())
            ).all()
        )
        assert [run.hermes_run_id for run in runs] == [
            receipts[1].run_id,
            receipts[0].run_id,
        ]
        assert all(run.status == "completed" for run in runs)
        assert all(run.session_link_id is not None for run in runs)


def test_cron_and_timezone_are_validated_before_any_mutation(authenticated):
    client, csrf = authenticated
    invalid_payloads = [
        automation_payload(client, schedule="0 0 1 * * *"),
        automation_payload(client, schedule="60 9 * * MON"),
        automation_payload(client, schedule="0 9 * * MON--FRI"),
        automation_payload(client, timezone="Mars/Olympus_Mons"),
    ]

    for index, payload in enumerate(invalid_payloads):
        response = client.post(
            "/api/v1/automations",
            headers=mutation_headers(csrf, f"invalid-automation-{index}"),
            json=payload,
        )
        assert response.status_code == 422, response.text

    assert client.get("/api/v1/automations").json() == []


def test_automation_lifecycle_runs_and_idempotent_replays(authenticated):
    client, csrf = authenticated
    headers = mutation_headers(csrf, "automation-create-once")
    payload = automation_payload(client)
    first = client.post("/api/v1/automations", headers=headers, json=payload)
    replay = client.post("/api/v1/automations", headers=headers, json=payload)

    assert first.status_code == replay.status_code == 201
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json() == first.json()
    automation = first.json()
    automation_id = automation["id"]
    assert len(automation["nextRuns"]) == 5

    paused = client.post(
        f"/api/v1/automations/{automation_id}/pause",
        headers=mutation_headers(csrf, "automation-pause-once"),
    )
    pause_replay = client.post(
        f"/api/v1/automations/{automation_id}/pause",
        headers=mutation_headers(csrf, "automation-pause-once"),
    )
    assert paused.status_code == pause_replay.status_code == 200
    assert paused.json()["enabled"] is False
    assert pause_replay.headers["X-Idempotent-Replay"] == "true"

    resumed = client.post(
        f"/api/v1/automations/{automation_id}/resume",
        headers=mutation_headers(csrf, "automation-resume-once"),
    )
    assert resumed.status_code == 200
    assert resumed.json()["enabled"] is True

    updated = client.patch(
        f"/api/v1/automations/{automation_id}",
        headers=mutation_headers(csrf, "automation-update-once"),
        json={"schedule": "30 7 * JAN-MAR MON", "timezone": "Europe/Madrid"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["schedule"] == "30 7 * JAN-MAR MON"
    assert updated.json()["timezone"] == "Europe/Madrid"

    trigger_headers = mutation_headers(csrf, "automation-trigger-once")
    triggered = client.post(
        f"/api/v1/automations/{automation_id}/trigger", headers=trigger_headers
    )
    trigger_replay = client.post(
        f"/api/v1/automations/{automation_id}/trigger", headers=trigger_headers
    )
    assert triggered.status_code == trigger_replay.status_code == 202
    assert trigger_replay.headers["X-Idempotent-Replay"] == "true"
    assert triggered.json() == trigger_replay.json()

    nested_runs = client.get(f"/api/v1/automations/{automation_id}/runs")
    filtered_runs = client.get(
        "/api/v1/automation-runs", params={"automationId": automation_id}
    )
    assert nested_runs.status_code == filtered_runs.status_code == 200
    assert nested_runs.json() == filtered_runs.json()
    assert len(nested_runs.json()) == 1
    run = nested_runs.json()[0]
    assert run["id"] == triggered.json()["operationId"]
    assert run["hermesRunId"] is not None
    assert run["status"] == "completed"
    assert run["sessionLinkId"] is not None
    assert run["readAt"] is None

    assert client.post(f"/api/v1/automation-runs/{run['id']}/read").status_code == 403
    read_headers = mutation_headers(csrf, "automation-run-read-once")
    marked_read = client.post(
        f"/api/v1/automation-runs/{run['id']}/read",
        headers=read_headers,
    )
    read_replay = client.post(
        f"/api/v1/automation-runs/{run['id']}/read",
        headers=read_headers,
    )
    assert marked_read.status_code == read_replay.status_code == 200
    assert marked_read.json()["readAt"] is not None
    assert read_replay.headers["X-Idempotent-Replay"] == "true"
    assert read_replay.json() == marked_read.json()
    refreshed_runs = client.get(f"/api/v1/automations/{automation_id}/runs").json()
    assert refreshed_runs[0]["readAt"] == marked_read.json()["readAt"]


def test_delete_is_idempotent(authenticated):
    client, csrf = authenticated
    automation = create_automation(client, csrf, "automation-to-delete")
    headers = mutation_headers(csrf, "automation-delete-once")

    first = client.delete(f"/api/v1/automations/{automation['id']}", headers=headers)
    replay = client.delete(f"/api/v1/automations/{automation['id']}", headers=headers)

    assert first.status_code == replay.status_code == 204
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert client.get("/api/v1/automations").json() == []


def test_sync_removes_a_reference_deleted_directly_in_hermes(authenticated, app):
    client, csrf = authenticated
    automation = create_automation(client, csrf, "external-delete-seed")

    async def remove_upstream():
        with app.state.session_factory() as db:
            connection = await GatewayService(app.state.services).connection(
                db, automation["gatewayId"], "control-dev"
            )
        provider = await app.state.services.provider_pool.get(connection)
        await provider.delete_automation(automation["hermesAutomationId"])

    client.portal.call(remove_upstream)
    synchronized = client.post(
        "/api/v1/automations/sync",
        params={
            "gatewayId": automation["gatewayId"],
            "profileName": "control-dev",
        },
        headers=mutation_headers(csrf, "external-delete-sync"),
    )

    assert synchronized.status_code == 200, synchronized.text
    assert synchronized.json() == []
    assert client.get("/api/v1/automations").json() == []


def test_ambiguous_manual_trigger_is_never_retried(authenticated, monkeypatch):
    client, csrf = authenticated
    automation = create_automation(client, csrf, "ambiguous-trigger-seed")
    trigger = AsyncMock(side_effect=RuntimeError("MUTATION_DELIVERY_UNKNOWN"))
    monkeypatch.setattr(InMemoryHermesProvider, "trigger_automation", trigger)

    response = client.post(
        f"/api/v1/automations/{automation['id']}/trigger",
        headers=mutation_headers(csrf, "ambiguous-trigger"),
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    runs = client.get(f"/api/v1/automations/{automation['id']}/runs").json()
    assert runs[0]["id"] == response.json()["operationId"]
    assert runs[0]["status"] == "unknown"
    assert "will not retry" in runs[0]["errorSummary"]
    trigger.assert_awaited_once()


def test_restart_marks_orphaned_local_trigger_unknown_without_retry(
    authenticated, app, monkeypatch
):
    client, csrf = authenticated
    automation = create_automation(client, csrf, "orphaned-trigger-seed")
    with app.state.session_factory() as db:
        row = db.get(Automation, automation["id"])
        run = AutomationRun(automation_id=row.id, status="queued")
        db.add(run)
        db.commit()
        run_id = run.id

    trigger = AsyncMock(side_effect=AssertionError("orphan recovery must not retry"))
    monkeypatch.setattr(InMemoryHermesProvider, "trigger_automation", trigger)
    recovered = app.state.mark_orphaned_local_triggers_unknown()

    assert recovered == 1
    with app.state.session_factory() as db:
        run = db.get(AutomationRun, run_id)
        assert run.status == "unknown"
        assert run.finished_at is not None
        assert "not retried" in run.error_summary
    trigger.assert_not_awaited()


def test_run_listing_is_owner_scoped(authenticated, app):
    client, csrf = authenticated
    owned = create_automation(client, csrf, "owned-automation")
    triggered = client.post(
        f"/api/v1/automations/{owned['id']}/trigger",
        headers=mutation_headers(csrf, "owned-trigger"),
    )
    assert triggered.status_code == 202

    with app.state.session_factory() as db:
        other = User(username="other", password_hash="not-used", is_admin=False)
        db.add(other)
        db.flush()
        foreign_automation = Automation(
            owner_id=other.id,
            gateway_id=owned["gatewayId"],
            profile_name="control-dev",
            hermes_automation_id="foreign-hermes-automation",
            name="Foreign",
            schedule="0 0 * * *",
            timezone="UTC",
            prompt="Foreign prompt",
            enabled=True,
            next_runs=[],
        )
        db.add(foreign_automation)
        db.flush()
        foreign_run = AutomationRun(
            automation_id=foreign_automation.id,
            hermes_run_id="foreign-run",
            status="queued",
        )
        db.add(foreign_run)
        db.commit()
        foreign_automation_id = foreign_automation.id
        foreign_run_id = foreign_run.id

    runs = client.get("/api/v1/automation-runs")
    assert runs.status_code == 200
    assert len(runs.json()) == 1
    assert runs.json()[0]["automationId"] == owned["id"]
    assert client.get(f"/api/v1/automations/{foreign_automation_id}/runs").status_code == 404
    assert client.post(
        f"/api/v1/automation-runs/{foreign_run_id}/read",
        headers=mutation_headers(csrf, "foreign-run-read"),
    ).status_code == 404


def test_every_automation_mutation_is_capability_gated(authenticated, app, monkeypatch):
    client, csrf = authenticated
    automation = create_automation(client, csrf, "capability-seed")

    async def no_mutation_capabilities(self):
        return CapabilitySet(
            version="mock-limited",
            source_sha="limited",
            methods=frozenset({"cron.list"}),
        )

    async def forbidden_mutation(*args, **kwargs):
        raise AssertionError("mutation reached provider without a verified capability")

    monkeypatch.setattr(InMemoryHermesProvider, "capabilities", no_mutation_capabilities)
    monkeypatch.setattr(InMemoryHermesProvider, "create_automation", forbidden_mutation)
    monkeypatch.setattr(InMemoryHermesProvider, "update_automation", forbidden_mutation)
    monkeypatch.setattr(InMemoryHermesProvider, "delete_automation", forbidden_mutation)
    monkeypatch.setattr(InMemoryHermesProvider, "trigger_automation", forbidden_mutation)
    with app.state.session_factory() as db:
        profile = db.scalar(
            select(ProfileRef).where(
                ProfileRef.gateway_id == automation["gatewayId"],
                ProfileRef.profile_name == "control-dev",
            )
        )
        assert profile is not None
        profile.capabilities = {}
        profile.last_seen_at = None
        db.commit()

    attempts = [
        client.post(
            "/api/v1/automations",
            headers=mutation_headers(csrf, "gated-create"),
            json=automation_payload(client, name="Blocked create"),
        ),
        client.patch(
            f"/api/v1/automations/{automation['id']}",
            headers=mutation_headers(csrf, "gated-update"),
            json={"name": "Blocked update"},
        ),
        client.post(
            f"/api/v1/automations/{automation['id']}/pause",
            headers=mutation_headers(csrf, "gated-pause"),
        ),
        client.post(
            f"/api/v1/automations/{automation['id']}/trigger",
            headers=mutation_headers(csrf, "gated-trigger"),
        ),
        client.delete(
            f"/api/v1/automations/{automation['id']}",
            headers=mutation_headers(csrf, "gated-delete"),
        ),
    ]
    assert [response.status_code for response in attempts] == [409, 409, 409, 409, 409]
    assert client.get("/api/v1/automation-runs").json() == []


def test_gateway_automation_parser_preserves_only_five_next_runs():
    raw_values = [
        "2030-01-01T09:00:00Z",
        "2030-01-02T09:00:00+02:00",
        datetime(2030, 1, 3, 9, 0),
        "not-a-date",
        "2030-01-04T09:00:00Z",
        "2030-01-05T09:00:00Z",
        "2030-01-06T09:00:00Z",
    ]
    automation = HermesGatewayProvider._automation(
        {
            "job": {
                "id": "job-1",
                "name": "Parser contract",
                "schedule": "0 9 * * *",
                "timezone": "UTC",
                "enabled": True,
                "prompt": "Run",
                "next_runs": raw_values,
            }
        }
    )

    assert automation.automation_id == "job-1"
    assert len(automation.next_runs) == 5
    assert automation.next_runs[0] == datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)
    assert automation.next_runs[2].tzinfo == timezone.utc
    assert automation.next_runs[-1] == datetime(2030, 1, 5, 9, 0, tzinfo=timezone.utc)


def test_gateway_automation_parser_expands_official_next_run_to_five_occurrences():
    automation = HermesGatewayProvider._automation(
        {
            "id": "official-job",
            "name": "Official contract",
            "schedule": {"kind": "cron", "expr": "0 9 * * 1", "display": "0 9 * * 1"},
            "enabled": True,
            "prompt": "Run",
            "next_run_at": "2030-01-07T09:00:00-06:00",
        },
        timezone_name="America/Mexico_City",
    )

    assert automation.schedule == "0 9 * * 1"
    assert automation.timezone == "America/Mexico_City"
    assert len(automation.next_runs) == 5
    assert automation.next_runs[0].isoformat() == "2030-01-07T09:00:00-06:00"
    assert [value.weekday() for value in automation.next_runs] == [0, 0, 0, 0, 0]
