from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone


_LOGGER = logging.getLogger("hermes_control.supervision")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class SupervisorHealth:
    """Thread-safe, non-secret health for one background control loop.

    Readiness handlers run in a worker thread while supervisors run on the
    application event loop.  Keeping this state outside SQLite lets readiness
    report a database/table failure that prevented the watcher itself from
    writing anything.
    """

    def __init__(self, *, stale_after_seconds: float) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        self.stale_after_seconds = float(stale_after_seconds)
        self._lock = threading.Lock()
        self._started_at = _utc_now()
        self._last_attempt_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._last_failure_at: datetime | None = None
        self._consecutive_failures = 0
        self._total_failures = 0

    def mark_attempt(self, *, at: datetime | None = None) -> None:
        with self._lock:
            self._last_attempt_at = _as_utc(at or _utc_now())

    def mark_success(self, *, at: datetime | None = None) -> None:
        now = _as_utc(at or _utc_now())
        with self._lock:
            self._last_attempt_at = now
            self._last_success_at = now
            self._consecutive_failures = 0

    def mark_failure(self, *, at: datetime | None = None) -> None:
        now = _as_utc(at or _utc_now())
        with self._lock:
            self._last_attempt_at = now
            self._last_failure_at = now
            self._consecutive_failures += 1
            self._total_failures += 1

    def snapshot(self, *, at: datetime | None = None) -> dict[str, object]:
        now = _as_utc(at or _utc_now())
        with self._lock:
            last_attempt = self._last_attempt_at
            last_success = self._last_success_at
            last_failure = self._last_failure_at
            consecutive_failures = self._consecutive_failures
            total_failures = self._total_failures
            started_at = self._started_at

        reference = last_success or started_at
        stale = (now - reference).total_seconds() > self.stale_after_seconds
        if consecutive_failures:
            status = "failed"
        elif stale:
            status = "stale"
        elif last_success is None:
            status = "starting"
        else:
            status = "healthy"
        return {
            "status": status,
            "lastAttemptAt": last_attempt.isoformat() if last_attempt else None,
            "lastSuccessAt": last_success.isoformat() if last_success else None,
            "lastFailureAt": last_failure.isoformat() if last_failure else None,
            "consecutiveFailures": consecutive_failures,
            "totalFailures": total_failures,
            "staleAfterSeconds": self.stale_after_seconds,
        }


class IndependentRefresh:
    """Refresh independent routes without waiting for a stalled neighbour.

    Keep at most one operation per key. A slow operation survives a pass's
    wait budget, so subsequent passes can renew healthy keys without either
    cancelling the slow read or piling up duplicate requests behind it.
    """

    def __init__(self, operation: Callable[[str], Awaitable[None]], *, wait_seconds: float):
        self.operation = operation
        self.wait_seconds = wait_seconds
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.lock = asyncio.Lock()

    def _completed(self) -> list[Exception]:
        errors = []
        for key, task in list(self.tasks.items()):
            if not task.done():
                continue
            del self.tasks[key]
            if not task.cancelled() and (error := task.exception()) is not None:
                errors.append(error)
        return errors

    async def run(self, keys: list[str]) -> None:
        async with self.lock:
            removed = [self.tasks.pop(key) for key in list(self.tasks) if key not in keys]
            for task in removed:
                task.cancel()
            if removed:
                await asyncio.gather(*removed, return_exceptions=True)
            errors = self._completed()
            for key in keys:
                if key not in self.tasks:
                    self.tasks[key] = asyncio.create_task(self.operation(key))
            if self.tasks:
                await asyncio.wait(self.tasks.values(), timeout=self.wait_seconds)
            errors.extend(self._completed())
            if errors:
                # Observe every result before surfacing a failure to the
                # supervisor; no task exception is abandoned or logged raw.
                raise errors[0]

    async def close(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        self.tasks.clear()


async def supervise_periodic(
    operation: Callable[[], Awaitable[None]],
    *,
    health: SupervisorHealth,
    interval_seconds: float,
) -> None:
    """Run a bounded watcher forever, isolating transient operation failures."""

    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")
    while True:
        health.mark_attempt()
        try:
            await operation()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Exception messages may embed SQL, paths, or upstream values.
            # The public health snapshot carries only counters and timestamps.
            health.mark_failure()
            _LOGGER.warning("Background supervisor iteration failed; retry scheduled")
        else:
            health.mark_success()
        await asyncio.sleep(interval_seconds)
