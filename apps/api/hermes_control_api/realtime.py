from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hermes_client import NormalizedEvent
from sqlalchemy import select

from .gateway_health import aggregate_profile_health
from .models import (
    Automation,
    AutomationRun,
    Gateway,
    IdempotencyOperation,
    ProfileRef,
    SessionLink,
    utc_now,
)


_SUCCESS = {"message.complete", "message.completed", "message.done", "run.completed"}
_FAILED = {"message.error", "message.failed", "run.error", "run.failed", "error"}
_INTERRUPTED = {
    "message.interrupted", "message.cancelled", "run.interrupted", "run.cancelled",
    "interrupted",
}
_ACTIVE_PROMPT_STATUSES = {
    "pending",
    "accepted",
    "streaming",
    "delivery_unknown",
}


def terminal_status(event_type: str, data: dict[str, Any] | None = None) -> str | None:
    explicit = str((data or {}).get("status") or "").strip().lower()
    if event_type.startswith(("message.", "run.", "session.")):
        if explicit in {"interrupted", "cancelled", "canceled", "stopped"}:
            return "interrupted"
        if explicit in {"failed", "error"}:
            return "failed"
        if explicit in {"completed", "complete", "done", "succeeded", "success"}:
            return "completed"
    if event_type in _SUCCESS:
        return "completed"
    if event_type in _FAILED:
        return "failed"
    if event_type in _INTERRUPTED:
        return "interrupted"
    return None


def persist_normalized_event(
    session_factory,
    event: NormalizedEvent,
    *,
    gateway_health_ttl_seconds: int = 60,
) -> None:
    """Persist terminal/cursor state before browser fanout.

    This path runs for every provider event, even with zero WebSocket clients,
    so prompt and automation state never depends on a browser being connected.
    """

    if not _metadata_is_safe(event):
        return
    if event.type == "control.connection":
        _persist_gateway_connection_state(
            session_factory,
            event,
            gateway_health_ttl_seconds=gateway_health_ttl_seconds,
        )
        if not event.stored_session_id and not event.runtime_session_id:
            return
    if not event.stored_session_id and not event.runtime_session_id:
        _persist_run_without_session(session_factory, event)
        return
    with session_factory() as db:
        identity = []
        if event.stored_session_id:
            identity.append(SessionLink.stored_session_id == event.stored_session_id)
        elif event.runtime_session_id:
            identity.append(SessionLink.runtime_session_id == event.runtime_session_id)
            identity.append(SessionLink.runtime_generation == event.runtime_generation)
        matches = list(
            db.scalars(
                select(SessionLink)
                .where(
                    SessionLink.gateway_id == event.gateway_id,
                    SessionLink.profile_name == event.profile_name,
                    *identity,
                )
                .limit(2)
            ).all()
        )
        session = matches[0] if len(matches) == 1 else None
        run = _find_or_create_run(db, event)
        if run is not None and session is not None:
            automation = db.get(Automation, run.automation_id)
            if automation is None or automation.owner_id != session.owner_id:
                run = None
            else:
                session.workspace_id = automation.workspace_id
        if session is None and run is not None and event.stored_session_id:
            automation = db.get(Automation, run.automation_id)
            collision = db.scalar(
                select(SessionLink).where(
                    SessionLink.gateway_id == event.gateway_id,
                    SessionLink.profile_name == event.profile_name,
                    SessionLink.stored_session_id == event.stored_session_id,
                )
            )
            if automation is not None and collision is None:
                if not _release_stale_runtime_claim(db, event, excluding_id=None):
                    return
                session = SessionLink(
                    owner_id=automation.owner_id,
                    gateway_id=event.gateway_id,
                    workspace_id=automation.workspace_id,
                    profile_name=event.profile_name,
                    stored_session_id=event.stored_session_id,
                    runtime_session_id=event.runtime_session_id,
                    runtime_generation=(
                        event.runtime_generation if event.runtime_session_id else None
                    ),
                    title=f"Ejecución · {automation.name}",
                    status="streaming",
                )
                db.add(session)
                db.flush()
        if session is None:
            if run is not None:
                _update_run(run, event)
                db.commit()
            return
        if event.runtime_session_id:
            if not _release_stale_runtime_claim(db, event, excluding_id=session.id):
                return
            if (
                session.runtime_session_id != event.runtime_session_id
                or session.runtime_generation != event.runtime_generation
            ):
                # `seq` belongs to the ephemeral Hermes runtime sid, not to the
                # durable stored session or gateway epoch.  Reset before
                # evaluating the first event from a resumed runtime.
                session.last_sequence = 0
                session.replay_epoch = None
            session.runtime_session_id = event.runtime_session_id
            session.runtime_generation = event.runtime_generation
        # Hermes' official 0.20.5/0.20.6 event envelope has a per-session
        # sequence but no prompt request id.  Reject a duplicate replay before
        # it can complete a newer prompt on the same session.  A changed epoch
        # deliberately starts a new sequence space.
        fresh_sequence = True
        if event.sequence is not None:
            same_epoch = not (
                event.replay_epoch
                and session.replay_epoch
                and event.replay_epoch != session.replay_epoch
            )
            if same_epoch and event.sequence <= session.last_sequence:
                fresh_sequence = False
        if not fresh_sequence:
            return
        if event.sequence is not None:
            if event.replay_epoch and session.replay_epoch and event.replay_epoch != session.replay_epoch:
                session.last_sequence = event.sequence
            else:
                session.last_sequence = max(session.last_sequence, event.sequence)
        if event.replay_epoch:
            session.replay_epoch = event.replay_epoch
        terminal = terminal_status(event.type, event.data)
        if terminal:
            session.status = "ready" if terminal in {"completed", "interrupted"} else "error"
            operation = _terminal_prompt_operation(db, session, event)
            if operation is not None:
                operation.status = terminal
                operation.response_json = {
                    **dict(operation.response_json or {}),
                    "operationId": operation.idempotency_key,
                    "status": terminal,
                }
        if run is not None:
            run.session_link_id = session.id
            _update_run(run, event)
        db.commit()


def _terminal_prompt_operation(
    db,
    session: SessionLink,
    event: NormalizedEvent,
) -> IdempotencyOperation | None:
    """Resolve a terminal Hermes message to one Control prompt operation.

    Audited Hermes releases do not echo ``request_id`` on events.  Control
    therefore permits at most one active prompt per session (enforced by
    ``SessionService.submit``) and may use that unambiguous session-local
    operation when a fresh sequenced ``message.*`` terminal event arrives.
    Exact correlation ids remain authoritative for mocks/future protocols.
    """

    scope = f"session:{session.id}:prompt"
    if event.correlation_id:
        return db.scalar(
            select(IdempotencyOperation).where(
                IdempotencyOperation.user_id == session.owner_id,
                IdempotencyOperation.scope == scope,
                IdempotencyOperation.idempotency_key == event.correlation_id,
            )
        )
    if not event.type.startswith("message.") or event.sequence is None:
        return None
    active = list(
        db.scalars(
            select(IdempotencyOperation)
            .where(
                IdempotencyOperation.user_id == session.owner_id,
                IdempotencyOperation.scope == scope,
                IdempotencyOperation.status.in_(_ACTIVE_PROMPT_STATUSES),
            )
            .order_by(IdempotencyOperation.created_at)
            .limit(2)
        ).all()
    )
    return active[0] if len(active) == 1 else None


def _run_identity(event: NormalizedEvent) -> tuple[str, str] | None:
    data: dict[str, Any] = event.data or {}
    if not event.type.startswith(("run.", "cron.")):
        return None
    run_id = data.get("run_id")
    automation_identity = data.get("job_id")
    if not isinstance(run_id, str) or not isinstance(automation_identity, str):
        return None
    if not (0 < len(run_id) <= 255 and 0 < len(automation_identity) <= 255):
        return None
    return run_id, automation_identity


def _find_or_create_run(db, event: NormalizedEvent) -> AutomationRun | None:
    identity = _run_identity(event)
    if identity is None:
        return None
    run_id, automation_identity = identity
    automations = list(
        db.scalars(
            select(Automation)
            .where(
                Automation.gateway_id == event.gateway_id,
                Automation.profile_name == event.profile_name,
                Automation.hermes_automation_id == automation_identity,
            )
            .limit(2)
        ).all()
    )
    if len(automations) != 1:
        return None
    automation = automations[0]
    run = db.scalar(
        select(AutomationRun).where(
            AutomationRun.automation_id == automation.id,
            AutomationRun.hermes_run_id == run_id,
        )
    )
    if run is None:
        run = AutomationRun(
            automation_id=automation.id,
            hermes_run_id=run_id,
            status="queued",
        )
        db.add(run)
        db.flush()
    return run


def _update_run(run: AutomationRun, event: NormalizedEvent) -> None:
    now = datetime.now(timezone.utc)
    terminal = terminal_status(event.type, event.data)
    already_terminal = run.status in {"completed", "failed", "interrupted"}
    if event.type == "run.started":
        if not already_terminal:
            run.status = "running"
            run.started_at = run.started_at or now
    elif terminal:
        run.status = terminal
        run.started_at = run.started_at or now
        run.finished_at = now
        if terminal == "failed":
            summary = event.data.get("error") or event.data.get("reason")
            run.error_summary = str(summary)[:2_000] if summary else "Hermes run failed"


def _persist_run_without_session(session_factory, event: NormalizedEvent) -> None:
    with session_factory() as db:
        run = _find_or_create_run(db, event)
        if run is None:
            return
        _update_run(run, event)
        db.commit()


def _persist_gateway_connection_state(
    session_factory,
    event: NormalizedEvent,
    *,
    gateway_health_ttl_seconds: int,
) -> None:
    """Persist trusted provider connectivity without requiring a session link.

    Upstream frames cannot create ``control.*`` events (the normalizer rewrites
    them), and only a small local state vocabulary is accepted here.  The
    gateway row contains no credential material, so readiness can safely use
    this cache while Hermes remains the health source of truth.
    """

    state = event.data.get("state")
    health_status = {
        "connected": "online",
        "reconnecting": "degraded",
        "offline": "offline",
    }.get(state)
    if health_status is None:
        return
    with session_factory() as db:
        gateway = db.get(Gateway, event.gateway_id)
        if gateway is None or not gateway.enabled:
            return
        profile = db.scalar(
            select(ProfileRef).where(
                ProfileRef.gateway_id == event.gateway_id,
                ProfileRef.profile_name == event.profile_name,
            )
        )
        # A provider can only report health for a profile already established
        # by configured/discovered Hermes metadata. Never create a profile or
        # let an unknown route overwrite the gateway aggregate.
        if profile is None:
            return
        now = utc_now()
        profile.status = health_status
        profile.last_seen_at = now

        required_profiles = list(
            db.scalars(
                select(ProfileRef).where(ProfileRef.gateway_id == event.gateway_id)
            ).all()
        )
        gateway.health_status = aggregate_profile_health(
            required_profiles,
            at=now,
            ttl_seconds=gateway_health_ttl_seconds,
        )
        gateway.last_health_at = now
        db.commit()


def _release_stale_runtime_claim(
    db,
    event: NormalizedEvent,
    *,
    excluding_id: str | None,
) -> bool:
    if not event.runtime_session_id:
        return True
    statement = select(SessionLink).where(
        SessionLink.gateway_id == event.gateway_id,
        SessionLink.profile_name == event.profile_name,
        SessionLink.runtime_session_id == event.runtime_session_id,
    )
    if excluding_id is not None:
        statement = statement.where(SessionLink.id != excluding_id)
    collision = db.scalar(statement)
    if collision is None:
        return True
    if collision.runtime_generation == event.runtime_generation:
        # Two stored sessions claimed the same runtime inside one live
        # generation.  Do not guess which owner should receive the event.
        return False
    collision.runtime_session_id = None
    collision.runtime_generation = None
    return True


def _metadata_is_safe(event: NormalizedEvent) -> bool:
    limits = (
        (event.event_id, 512),
        (event.gateway_id, 512),
        (event.profile_name, 512),
        (event.stored_session_id, 255),
        (event.runtime_session_id, 255),
        (event.runtime_generation, 96),
        (event.replay_epoch, 100),
        (event.correlation_id, 200),
    )
    if any(value is not None and len(str(value)) > limit for value, limit in limits):
        return False
    if event.runtime_session_id and not event.runtime_generation:
        return False
    return event.sequence is None or (
        isinstance(event.sequence, int) and 0 <= event.sequence <= 9_223_372_036_854_775_807
    )
