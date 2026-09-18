from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio
import pytest

from sqlalchemy import select

from hermes_client import NormalizedEvent
from hermes_control_api.background_tasks import persist_snapshot, project_snapshot, unavailable_snapshot
from hermes_control_api.models import Gateway, IdempotencyOperation, SessionLink, User
from hermes_control_api.realtime import persist_normalized_event
from hermes_control_api.services import SessionService

STAMP = "2026-09-18T12:00:00+00:00"
LATER = "2026-09-18T12:01:00+00:00"


def task(**kwargs):
    return {"id": "delegate-1", "storedSessionId": "stored", "state": "running", "deliveryState": "pending",
        "title": "private task prompt must never become a label", "goal": "private goal", "result": "private result",
        "createdAt": STAMP, "updatedAt": STAMP, **kwargs}


def snapshot(**kwargs):
    return {"tasks": [task()], "complete": True, "available": True, "activeCount": 1,
        "pendingDeliveryCount": 0, "observedAt": STAMP, **kwargs}


def seed(app):
    with app.state.session_factory() as db:
        gateway = db.scalar(select(Gateway).order_by(Gateway.created_at))
        owner = db.scalar(select(User).where(User.username == "admin"))
        row = SessionLink(owner_id=owner.id, gateway_id=gateway.id, profile_name="control-dev", stored_session_id="stored")
        db.add(row)
        db.flush()
        operation = IdempotencyOperation(user_id=owner.id, scope=f"session:{row.id}:prompt", idempotency_key="human",
            status="accepted", response_json={"_historyCount": 0, "_promptHash": SessionService._prompt_digest("human prompt")})
        db.add(operation)
        db.commit()
        return row.id, gateway.id, operation.id


def test_snapshot_is_bounded_private_and_route_scoped():
    result = project_snapshot(snapshot(tasks=[task(), task(id="other", storedSessionId="foreign"), task(id="bad", state={})]), "stored", observed_at=STAMP)
    assert result["complete"] is False
    assert [t["id"] for t in result["items"]] == ["delegate-1", "bad"]
    assert result["items"][1]["state"] == "unknown"
    assert "private" not in str(result)
    assert "storedSessionId" not in str(result)
    bounded = project_snapshot(snapshot(tasks=[task(id=f"task-{i}") for i in range(201)]), "stored", observed_at=STAMP)
    assert len(bounded["items"]) == 200 and bounded["complete"] is False


def test_stale_and_incomplete_snapshots_never_erase_known_work():
    row = SimpleNamespace(background_tasks={}, stored_session_id="stored")
    latest = project_snapshot(snapshot(observedAt=LATER), "stored", observed_at=LATER)
    persist_snapshot(row, latest)
    stale = project_snapshot(snapshot(tasks=[], activeCount=0), "stored", observed_at=STAMP)
    assert persist_snapshot(row, stale) == latest
    incomplete = project_snapshot(snapshot(tasks=[], complete=False, observedAt="2026-09-18T12:02:00+00:00"), "stored", observed_at=LATER)
    assert len(persist_snapshot(row, incomplete)["items"]) == 1
    offline = unavailable_snapshot(row, observed_at=LATER)
    assert offline["activeCount"] is None and offline["available"] is False
    assert offline["items"][0]["state"] == "unknown"


def test_background_terminal_does_not_complete_pending_human_prompt(authenticated, app):
    row_id, gateway_id, operation_id = seed(app)
    for number, kind in enumerate(("message.start", "message.complete"), 1):
        persist_normalized_event(app.state.session_factory, NormalizedEvent.create(type=kind, gateway_id=gateway_id,
            profile_name="control-dev", stored_session_id="stored", sequence=number,
            data={"controlTurn": {"correlation": "history", "id": "a" * 64}}))
    with app.state.session_factory() as db:
        row = db.get(SessionLink, row_id)
        assert row.active_turn_id is None and row.status == "ready"
        assert db.get(IdempotencyOperation, operation_id).status == "accepted"
        history = [{"role": "user", "content": "human prompt"},
            {"role": "user", "display_kind": "async_delegation_complete", "content": "internal result"},
            {"role": "assistant", "content": "result from another task"}]
        SessionService._reconcile_active_prompt_from_history(db, row, history)
        assert db.get(IdempotencyOperation, operation_id).status == "accepted"
        history.insert(1, {"role": "assistant", "content": "human response"})
        SessionService._reconcile_active_prompt_from_history(db, row, history)
        assert db.get(IdempotencyOperation, operation_id).status == "completed"


def test_queued_receipt_remains_pending_until_exact_human_row_is_consumed(authenticated, app):
    row_id, _, operation_id = seed(app)
    with app.state.session_factory() as db:
        row = db.get(SessionLink, row_id)
        operation = db.get(IdempotencyOperation, operation_id)
        operation.status = "queued"
        background = [{"role": "user", "display_kind": "async_delegation_complete", "content": "private worker wake"},
            {"role": "assistant", "content": "Previous task is finished"}]
        SessionService._reconcile_active_prompt_from_history(db, row, background)
        assert operation.status == "queued"
        SessionService._reconcile_active_prompt_from_history(db, row, [*background, {"role": "user", "content": "human prompt"}])
        assert operation.status == "streaming" and operation.response_json["status"] == "streaming"
        assert row.status != "ready"


def test_snapshot_persists_without_browser_and_cannot_cross_profile(authenticated, app):
    row_id, gateway_id, _ = seed(app)
    for profile in ("foreign-profile", "control-dev"):
        persist_normalized_event(app.state.session_factory, NormalizedEvent.create(type="background.tasks", gateway_id=gateway_id,
            profile_name=profile, stored_session_id="stored", data=snapshot()))
        with app.state.session_factory() as db:
            row = db.get(SessionLink, row_id)
            assert bool(row.background_tasks) == (profile == "control-dev")
            assert row.last_sequence == 0
            assert row.active_turn_id is None


def test_reconnect_terminal_without_start_finishes_foreground_but_not_human_operation(authenticated, app):
    row_id, gateway_id, operation_id = seed(app)
    with app.state.session_factory() as db:
        row = db.get(SessionLink, row_id)
        row.status, row.active_turn_id = "streaming", "a" * 64
        row.runtime_session_id, row.runtime_generation = "runtime", "old-generation"
        row.last_sequence = 3
        db.commit()
    persist_normalized_event(app.state.session_factory, NormalizedEvent.create(type="message.complete", gateway_id=gateway_id,
        profile_name="control-dev", stored_session_id="stored", runtime_session_id="runtime", runtime_generation="new-generation",
        sequence=12, data={"controlTurn": {"correlation": "history"}}))
    with app.state.session_factory() as db:
        row = db.get(SessionLink, row_id)
        assert row.status == "ready" and row.active_turn_id is None
        assert db.get(IdempotencyOperation, operation_id).status == "accepted"


def test_unidentified_terminal_does_not_release_an_identified_newer_turn(authenticated, app):
    row_id, gateway_id, operation_id = seed(app)
    with app.state.session_factory() as db:
        row = db.get(SessionLink, row_id)
        row.status, row.active_turn_id = "streaming", "a" * 64
        db.commit()
    persist_normalized_event(app.state.session_factory, NormalizedEvent.create(type="message.complete", gateway_id=gateway_id,
        profile_name="control-dev", stored_session_id="stored", sequence=12, data={"controlTurn": {"correlation": "history"}}))
    with app.state.session_factory() as db:
        row = db.get(SessionLink, row_id)
        assert row.status == "streaming" and row.active_turn_id == "a" * 64
        assert db.get(IdempotencyOperation, operation_id).status == "accepted"


def test_background_endpoint_checks_ownership_before_provider(authenticated, app, monkeypatch):
    client, _ = authenticated
    row_id, _, _ = seed(app)
    call = AsyncMock(return_value=project_snapshot(snapshot(), "stored", observed_at=STAMP))
    monkeypatch.setattr(SessionService, "background_tasks", call)
    with app.state.session_factory() as db:
        owner = User(username="someone-else", password_hash="unused")
        db.add(owner)
        db.flush()
        db.get(SessionLink, row_id).owner_id = owner.id
        db.commit()
    assert client.get(f"/api/v1/sessions/{row_id}/background-tasks").status_code == 404
    call.assert_not_awaited()


@pytest.mark.asyncio
async def test_terminal_reconciles_without_browser_or_redispatch(authenticated, app, monkeypatch):
    from hermes_control_api.prompt_reconciliation import PromptHistoryReconciler
    _, gateway_id, operation_id = seed(app)
    reader = AsyncMock(return_value=[{"role": "user", "content": "human prompt"},
        {"role": "assistant", "content": "The task is running; I can help with something else."}])
    monkeypatch.setattr(SessionService, "_raw_history", reader)
    submit = AsyncMock(side_effect=AssertionError("Must never redispatch"))
    monkeypatch.setattr(SessionService, "submit", submit)
    reconciler = PromptHistoryReconciler(app.state.session_factory, app.state.services)
    event = NormalizedEvent.create(type="message.complete", gateway_id=gateway_id, profile_name="control-dev",
        stored_session_id="stored", data={"controlTurn": {"correlation": "history", "id": "a" * 64}})
    reconciler.schedule(event)
    reconciler.schedule(event)
    await asyncio.gather(*list(reconciler.tasks.values()))
    with app.state.session_factory() as db:
        assert db.get(IdempotencyOperation, operation_id).status == "completed"
    reader.assert_awaited_once()
    submit.assert_not_awaited()
    await reconciler.close()
