from __future__ import annotations

import pytest
from sqlalchemy import select

from hermes_client import NormalizedEvent
from hermes_control_api.api.routes import bind_owned_realtime_event
from hermes_control_api.models import (
    Automation,
    AutomationRun,
    Gateway,
    IdempotencyOperation,
    SessionLink,
    User,
)
from hermes_control_api.realtime import persist_normalized_event

from .conftest import mutation_headers


def _gateway_and_admin(db) -> tuple[Gateway, User]:
    gateway = db.scalar(select(Gateway).order_by(Gateway.created_at))
    admin = db.scalar(select(User).where(User.username == "admin"))
    assert gateway is not None and admin is not None
    return gateway, admin


def test_runtime_reuse_requires_generation_then_rebinds_by_stored_identity(
    authenticated, app
):
    with app.state.session_factory() as db:
        gateway, stale_owner = _gateway_and_admin(db)
        target_owner = User(
            username="runtime-target-owner",
            password_hash="unused",
            is_admin=False,
        )
        db.add(target_owner)
        db.flush()
        stale = SessionLink(
            id="session-stale-runtime",
            owner_id=stale_owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-stale-runtime",
            runtime_session_id="runtime-reused",
            runtime_generation="generation-old",
            status="streaming",
        )
        target = SessionLink(
            id="session-target-runtime",
            owner_id=target_owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-target-runtime",
            status="idle",
        )
        db.add_all([stale, target])
        db.commit()
        gateway_id = gateway.id
        stale_owner_id = stale_owner.id
        target_owner_id = target_owner.id

    base_payload = {
        "type": "message.delta",
        "gatewayId": gateway_id,
        "profileName": "control-dev",
        "runtimeSessionId": "runtime-reused",
        "seq": 7,
        "data": {"delta": "must not reach stale owner"},
    }
    assert (
        bind_owned_realtime_event(
            app.state.session_factory,
            user_id=stale_owner_id,
            payload=base_payload,
        )
        is None
    )
    assert (
        bind_owned_realtime_event(
            app.state.session_factory,
            user_id=stale_owner_id,
            payload={**base_payload, "_runtimeGeneration": "generation-new"},
        )
        is None
    )

    persist_normalized_event(
        app.state.session_factory,
        NormalizedEvent.create(
            type="message.delta",
            gateway_id=gateway_id,
            profile_name="control-dev",
            runtime_session_id="runtime-reused",
            runtime_generation="generation-new",
            sequence=7,
            data={"delta": "runtime-only cannot acquire a new owner"},
        ),
    )
    with app.state.session_factory() as db:
        stale = db.get(SessionLink, "session-stale-runtime")
        target = db.get(SessionLink, "session-target-runtime")
        assert (stale.runtime_session_id, stale.runtime_generation) == (
            "runtime-reused",
            "generation-old",
        )
        assert target.runtime_session_id is None
        assert target.last_sequence == 0

    durable = NormalizedEvent.create(
        type="message.delta",
        gateway_id=gateway_id,
        profile_name="control-dev",
        stored_session_id="stored-target-runtime",
        runtime_session_id="runtime-reused",
        runtime_generation="generation-new",
        sequence=8,
        replay_epoch="epoch-new",
        data={"delta": "belongs to target"},
    )
    persist_normalized_event(app.state.session_factory, durable)

    with app.state.session_factory() as db:
        stale = db.get(SessionLink, "session-stale-runtime")
        target = db.get(SessionLink, "session-target-runtime")
        assert stale.runtime_session_id is None
        assert stale.runtime_generation is None
        assert (target.runtime_session_id, target.runtime_generation) == (
            "runtime-reused",
            "generation-new",
        )
        assert target.last_sequence == 8
        assert target.replay_epoch == "epoch-new"

    public_payload = durable.to_dict()
    bound = bind_owned_realtime_event(
        app.state.session_factory,
        user_id=target_owner_id,
        payload=public_payload,
    )
    assert bound is not None
    assert bound["controlSessionId"] == "session-target-runtime"
    assert bound["storedSessionId"] == "stored-target-runtime"
    assert bound["runtimeSessionId"] == "runtime-reused"
    assert "_runtimeGeneration" not in bound
    assert (
        bind_owned_realtime_event(
            app.state.session_factory,
            user_id=stale_owner_id,
            payload=public_payload,
        )
        is None
    )


def test_message_complete_with_interrupted_status_closes_prompt_durably(
    authenticated, app
):
    with app.state.session_factory() as db:
        gateway, owner = _gateway_and_admin(db)
        session = SessionLink(
            owner_id=owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-interrupted-message",
            runtime_session_id="runtime-interrupted-message",
            runtime_generation="generation-interrupted",
            status="streaming",
        )
        db.add(session)
        db.flush()
        operation = IdempotencyOperation(
            user_id=owner.id,
            scope=f"session:{session.id}:prompt",
            idempotency_key="operation-interrupted-message",
            status="streaming",
            response_json={
                "operationId": "operation-interrupted-message",
                "status": "streaming",
            },
        )
        db.add(operation)
        db.commit()
        session_id = session.id

    persist_normalized_event(
        app.state.session_factory,
        NormalizedEvent.create(
            type="message.complete",
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-interrupted-message",
            runtime_session_id="runtime-interrupted-message",
            runtime_generation="generation-interrupted",
            correlation_id="operation-interrupted-message",
            data={"status": "interrupted"},
        ),
    )

    with app.state.session_factory() as db:
        session = db.get(SessionLink, session_id)
        operation = db.scalar(
            select(IdempotencyOperation).where(
                IdempotencyOperation.scope == f"session:{session_id}:prompt",
                IdempotencyOperation.idempotency_key
                == "operation-interrupted-message",
            )
        )
        assert session.status == "ready"
        assert operation.status == "interrupted"
        assert operation.response_json["status"] == "interrupted"


def test_fresh_official_terminal_event_closes_the_only_active_prompt_without_request_id(
    authenticated, app
):
    with app.state.session_factory() as db:
        gateway, owner = _gateway_and_admin(db)
        session = SessionLink(
            owner_id=owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-official-no-request-id",
            runtime_session_id="runtime-official-no-request-id",
            runtime_generation="generation-official",
            replay_epoch="epoch-official",
            last_sequence=40,
            status="streaming",
        )
        db.add(session)
        db.flush()
        operation = IdempotencyOperation(
            user_id=owner.id,
            scope=f"session:{session.id}:prompt",
            idempotency_key="operation-official-no-request-id",
            status="streaming",
            response_json={
                "operationId": "operation-official-no-request-id",
                "status": "streaming",
            },
        )
        db.add(operation)
        db.commit()
        session_id = session.id

    persist_normalized_event(
        app.state.session_factory,
        NormalizedEvent.create(
            type="message.complete",
            gateway_id=gateway.id,
            profile_name="control-dev",
            runtime_session_id="runtime-official-no-request-id",
            runtime_generation="generation-official",
            sequence=41,
            replay_epoch="epoch-official",
            data={"status": "completed"},
        ),
    )

    with app.state.session_factory() as db:
        session = db.get(SessionLink, session_id)
        operation = db.scalar(
            select(IdempotencyOperation).where(
                IdempotencyOperation.scope == f"session:{session_id}:prompt",
                IdempotencyOperation.idempotency_key
                == "operation-official-no-request-id",
            )
        )
        assert session.status == "ready"
        assert session.last_sequence == 41
        assert operation.status == "completed"
        assert operation.response_json["status"] == "completed"


def test_duplicate_uncorrelated_terminal_cannot_close_a_newer_prompt(
    authenticated, app
):
    with app.state.session_factory() as db:
        gateway, owner = _gateway_and_admin(db)
        session = SessionLink(
            owner_id=owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-duplicate-terminal",
            runtime_session_id="runtime-duplicate-terminal",
            runtime_generation="generation-duplicate",
            replay_epoch="epoch-duplicate",
            last_sequence=80,
            status="streaming",
        )
        db.add(session)
        db.flush()
        operation = IdempotencyOperation(
            user_id=owner.id,
            scope=f"session:{session.id}:prompt",
            idempotency_key="newer-operation",
            status="streaming",
            response_json={"operationId": "newer-operation", "status": "streaming"},
        )
        db.add(operation)
        db.commit()
        session_id = session.id

    persist_normalized_event(
        app.state.session_factory,
        NormalizedEvent.create(
            type="message.complete",
            gateway_id=gateway.id,
            profile_name="control-dev",
            runtime_session_id="runtime-duplicate-terminal",
            runtime_generation="generation-duplicate",
            sequence=80,
            replay_epoch="epoch-duplicate",
            data={"status": "completed"},
        ),
    )

    with app.state.session_factory() as db:
        session = db.get(SessionLink, session_id)
        operation = db.scalar(
            select(IdempotencyOperation).where(
                IdempotencyOperation.scope == f"session:{session_id}:prompt",
                IdempotencyOperation.idempotency_key == "newer-operation",
            )
        )
        assert session.status == "streaming"
        assert session.last_sequence == 80
        assert operation.status == "streaming"


def test_new_runtime_resets_old_watermark_and_closes_first_prompt_turn(
    authenticated, app
):
    with app.state.session_factory() as db:
        gateway, owner = _gateway_and_admin(db)
        session = SessionLink(
            owner_id=owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-runtime-resume",
            runtime_session_id="runtime-before-resume",
            runtime_generation="generation-before-resume",
            replay_epoch="gateway-epoch-stable",
            last_sequence=500,
            status="streaming",
        )
        db.add(session)
        db.flush()
        operation = IdempotencyOperation(
            user_id=owner.id,
            scope=f"session:{session.id}:prompt",
            idempotency_key="operation-after-resume",
            status="streaming",
            response_json={"operationId": "operation-after-resume", "status": "streaming"},
        )
        db.add(operation)
        db.commit()
        session_id = session.id

    persist_normalized_event(
        app.state.session_factory,
        NormalizedEvent.create(
            type="message.complete",
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-runtime-resume",
            runtime_session_id="runtime-after-resume",
            runtime_generation="generation-after-resume",
            sequence=1,
            replay_epoch="gateway-epoch-stable",
            data={"status": "completed"},
        ),
    )

    with app.state.session_factory() as db:
        session = db.get(SessionLink, session_id)
        operation = db.scalar(
            select(IdempotencyOperation).where(
                IdempotencyOperation.scope == f"session:{session_id}:prompt",
                IdempotencyOperation.idempotency_key == "operation-after-resume",
            )
        )
        assert session.runtime_session_id == "runtime-after-resume"
        assert session.runtime_generation == "generation-after-resume"
        assert session.last_sequence == 1
        assert session.status == "ready"
        assert operation.status == "completed"


def test_assigning_a_resumed_runtime_resets_the_session_cursor(authenticated, app):
    from hermes_control_api.services import SessionService

    with app.state.session_factory() as db:
        gateway, owner = _gateway_and_admin(db)
        session = SessionLink(
            owner_id=owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-assign-runtime",
            runtime_session_id="runtime-old",
            runtime_generation="generation-old",
            replay_epoch="epoch-old",
            last_sequence=99,
        )
        db.add(session)
        db.flush()

        SessionService._assign_runtime(
            db, session, "runtime-new", "generation-new"
        )

        assert session.runtime_session_id == "runtime-new"
        assert session.runtime_generation == "generation-new"
        assert session.last_sequence == 0
        assert session.replay_epoch is None


def test_confirmed_interrupt_closes_every_active_prompt_operation(authenticated, app):
    client, csrf = authenticated
    gateway_id = client.get("/api/v1/gateways").json()[0]["id"]
    created = client.post(
        "/api/v1/sessions",
        headers=mutation_headers(csrf, "interrupt-session-create"),
        json={
            "gatewayId": gateway_id,
            "profileName": "control-dev",
            "title": "Interrupt active operations",
        },
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    with app.state.session_factory() as db:
        owner_id = db.get(SessionLink, session_id).owner_id
        for status in ("pending", "accepted", "streaming", "completed"):
            db.add(
                IdempotencyOperation(
                    user_id=owner_id,
                    scope=f"session:{session_id}:prompt",
                    idempotency_key=f"interrupt-{status}",
                    status=status,
                    response_json={
                        "operationId": f"interrupt-{status}",
                        "status": status,
                    },
                )
            )
        db.commit()

    interrupted = client.post(
        f"/api/v1/sessions/{session_id}/interrupt",
        headers=mutation_headers(csrf, "interrupt-confirmed"),
    )
    assert interrupted.status_code == 204, interrupted.text

    with app.state.session_factory() as db:
        operations = {
            row.idempotency_key: row
            for row in db.scalars(
                select(IdempotencyOperation).where(
                    IdempotencyOperation.scope == f"session:{session_id}:prompt"
                )
            ).all()
        }
        for original in ("pending", "accepted", "streaming"):
            row = operations[f"interrupt-{original}"]
            assert row.status == "interrupted"
            assert row.response_json["status"] == "interrupted"
        assert operations["interrupt-completed"].status == "completed"
        assert db.get(SessionLink, session_id).status == "interrupted"


def test_automation_run_correlation_requires_run_event_exact_ids_and_owner(
    authenticated, app
):
    with app.state.session_factory() as db:
        gateway, automation_owner = _gateway_and_admin(db)
        other_owner = User(
            username="automation-other-owner",
            password_hash="unused",
            is_admin=False,
        )
        db.add(other_owner)
        db.flush()
        automation = Automation(
            owner_id=automation_owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            hermes_automation_id="job-exact",
            name="Exact correlation",
            schedule="0 9 * * *",
            timezone="UTC",
            prompt="Run",
            enabled=True,
            next_runs=[],
        )
        owner_session = SessionLink(
            owner_id=automation_owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-run-owner",
            runtime_session_id="runtime-run-owner",
            runtime_generation="generation-run-owner",
        )
        other_session = SessionLink(
            owner_id=other_owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-run-other",
            runtime_session_id="runtime-run-other",
            runtime_generation="generation-run-other",
        )
        db.add_all([automation, owner_session, other_session])
        db.flush()
        run_ids = {
            "message": "run-from-message-correlation",
            "alias": "run-automation-id-alias",
            "wrong_job": "run-wrong-job",
            "other_owner": "run-other-owner",
            "valid": "run-valid",
        }
        for run_id in run_ids.values():
            db.add(
                AutomationRun(
                    automation_id=automation.id,
                    hermes_run_id=run_id,
                    status="queued",
                )
            )
        db.commit()
        gateway_id = gateway.id
        owner_session_id = owner_session.id

    def persist(
        *,
        event_type: str,
        run_id: str,
        data: dict,
        stored_id: str,
        runtime_id: str,
        generation: str,
        correlation_id: str | None = None,
    ) -> None:
        persist_normalized_event(
            app.state.session_factory,
            NormalizedEvent.create(
                type=event_type,
                gateway_id=gateway_id,
                profile_name="control-dev",
                stored_session_id=stored_id,
                runtime_session_id=runtime_id,
                runtime_generation=generation,
                correlation_id=correlation_id,
                data={"status": "completed", "run_id": run_id, **data},
            ),
        )

    persist(
        event_type="message.complete",
        run_id=run_ids["message"],
        data={"job_id": "job-exact"},
        stored_id="stored-run-owner",
        runtime_id="runtime-run-owner",
        generation="generation-run-owner",
        correlation_id=run_ids["message"],
    )
    persist(
        event_type="run.completed",
        run_id=run_ids["alias"],
        data={"automation_id": "job-exact"},
        stored_id="stored-run-owner",
        runtime_id="runtime-run-owner",
        generation="generation-run-owner",
    )
    persist(
        event_type="run.completed",
        run_id=run_ids["wrong_job"],
        data={"job_id": "job-wrong"},
        stored_id="stored-run-owner",
        runtime_id="runtime-run-owner",
        generation="generation-run-owner",
    )
    persist(
        event_type="run.completed",
        run_id=run_ids["other_owner"],
        data={"job_id": "job-exact"},
        stored_id="stored-run-other",
        runtime_id="runtime-run-other",
        generation="generation-run-other",
    )
    persist(
        event_type="run.completed",
        run_id=run_ids["valid"],
        data={"job_id": "job-exact"},
        stored_id="stored-run-owner",
        runtime_id="runtime-run-owner",
        generation="generation-run-owner",
    )

    with app.state.session_factory() as db:
        runs = {
            row.hermes_run_id: row
            for row in db.scalars(select(AutomationRun)).all()
        }
        for rejected in ("message", "alias", "wrong_job", "other_owner"):
            assert runs[run_ids[rejected]].status == "queued"
            assert runs[run_ids[rejected]].session_link_id is None
        assert runs[run_ids["valid"]].status == "completed"
        assert runs[run_ids["valid"]].session_link_id == owner_session_id
        assert runs[run_ids["valid"]].finished_at is not None


@pytest.mark.parametrize(
    "unsafe_metadata",
    [
        {"event_id": "e" * 513},
        {"runtime_generation": "g" * 97},
        {"replay_epoch": "r" * 101},
        {"sequence": 9_223_372_036_854_775_808},
    ],
)
def test_unsafe_event_metadata_never_mutates_durable_state(
    authenticated, app, unsafe_metadata
):
    with app.state.session_factory() as db:
        gateway, owner = _gateway_and_admin(db)
        session = SessionLink(
            owner_id=owner.id,
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-safe-metadata",
            runtime_session_id="runtime-safe-metadata",
            runtime_generation="generation-safe",
            status="streaming",
            replay_epoch="epoch-safe",
            last_sequence=4,
        )
        db.add(session)
        db.flush()
        operation = IdempotencyOperation(
            user_id=owner.id,
            scope=f"session:{session.id}:prompt",
            idempotency_key="operation-safe-metadata",
            status="streaming",
            response_json={"status": "streaming"},
        )
        db.add(operation)
        db.commit()
        session_id = session.id

    event_values = {
        "event_id": "event-safe",
        "runtime_generation": "generation-safe",
        "replay_epoch": "epoch-new",
        "sequence": 9,
        **unsafe_metadata,
    }
    persist_normalized_event(
        app.state.session_factory,
        NormalizedEvent.create(
            type="message.complete",
            gateway_id=gateway.id,
            profile_name="control-dev",
            stored_session_id="stored-safe-metadata",
            runtime_session_id="runtime-safe-metadata",
            correlation_id="operation-safe-metadata",
            data={"status": "completed"},
            **event_values,
        ),
    )

    with app.state.session_factory() as db:
        session = db.get(SessionLink, session_id)
        operation = db.scalar(
            select(IdempotencyOperation).where(
                IdempotencyOperation.scope == f"session:{session_id}:prompt",
                IdempotencyOperation.idempotency_key == "operation-safe-metadata",
            )
        )
        assert session.status == "streaming"
        assert session.last_sequence == 4
        assert session.replay_epoch == "epoch-safe"
        assert operation.status == "streaming"
