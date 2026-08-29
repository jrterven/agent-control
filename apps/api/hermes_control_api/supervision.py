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
