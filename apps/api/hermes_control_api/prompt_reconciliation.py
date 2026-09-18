"""Read-only turn reconciliation independent of a connected browser."""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select

from .models import IdempotencyOperation, SessionLink
from .realtime import terminal_status


class PromptHistoryReconciler:
    def __init__(self, session_factory, services):
        self.session_factory, self.services = session_factory, services
        self.tasks = {}
        self.dirty = set()
        self.slots = asyncio.Semaphore(4)

    def schedule(self, event):
        turn = event.data.get("controlTurn")
        if (not terminal_status(event.type, event.data) or not isinstance(turn, dict)
                or turn.get("correlation") != "history"):
            return
        key = (event.gateway_id, event.profile_name, event.stored_session_id,
            event.runtime_session_id, event.runtime_generation)
        if key not in self.tasks and len(self.tasks) >= 256:
            return  # History/resume remains the durable recovery path.
        self.dirty.add(key)
        if key not in self.tasks:
            self.tasks[key] = asyncio.create_task(self._run(key), name="prompt-history-reconciliation")

    async def _run(self, key):
        from .services import SessionService
        try:
            while key in self.dirty:
                self.dirty.discard(key)
                async with self.slots, asyncio.timeout(65):
                    with self.session_factory() as db:
                        gateway, profile, stored, runtime, generation = key
                        identity = [SessionLink.stored_session_id == stored] if stored else [
                            SessionLink.runtime_session_id == runtime, SessionLink.runtime_generation == generation]
                        if not stored and not (runtime and generation):
                            continue
                        rows = list(db.scalars(select(SessionLink).where(SessionLink.gateway_id == gateway,
                            SessionLink.profile_name == profile, *identity).limit(2)))
                        if len(rows) != 1:
                            continue
                        row = rows[0]
                        active = db.scalar(select(IdempotencyOperation.id).where(IdempotencyOperation.user_id == row.owner_id,
                            IdempotencyOperation.scope == f"session:{row.id}:prompt",
                            IdempotencyOperation.status.in_(("pending", "queued", "redirected", "steered", "accepted", "streaming", "delivery_unknown"))).limit(1))
                        if active is None:
                            continue
                        service = SessionService(self.services)
                        history = await service._raw_history(db, row)
                        row = db.scalar(select(SessionLink).where(SessionLink.id == row.id)
                            .with_for_update().execution_options(populate_existing=True))
                        if row is None:
                            continue
                        # The helper only accepts the exact persisted human
                        # prompt boundary and never dispatches/retries work.
                        service._reconcile_active_prompt_from_history(db, row, history)
                        db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # No provider bodies, prompts, results or private routing in logs.
            logging.getLogger("hermes_control").info("Prompt history reconciliation deferred (%s)", type(exc).__name__)
        finally:
            self.tasks.pop(key, None)
            self.dirty.discard(key)

    async def close(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
