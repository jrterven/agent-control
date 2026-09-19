"""Bounded read-only metadata projection of the audited native delegation ledger.

The connector never dispatches, retries, repairs or updates Hermes tasks here.
Internal goals, prompts, results, transcripts, local paths and PIDs stay local.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import re
import sqlite3
import stat

MAX_TASKS = 200
IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$")
ACTIVE_STATES = frozenset({"running", "stalling", "finalizing", "queued", "dispatched"})
STATE_MAP = {**dict.fromkeys(ACTIVE_STATES, "running"), "queued": "queued",
    "completed": "completed", "success": "completed", "failed": "failed", "error": "failed",
    "timed_out": "failed", "stalled": "failed", "cancelled": "cancelled", "interrupted": "cancelled",
    "unknown": "unknown"}
DELIVERY_STATES = frozenset({"pending", "delivered", "dropped"})


def _timestamp(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value <= 0:
        raise ValueError("Invalid task timestamp")
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def unavailable() -> dict:
    return {"available": False, "complete": False, "activeCount": None, "pendingDeliveryCount": None, "totalCount": None,
        "tasks": [], "source": "hermes-native-delegation", "observedAt": datetime.now(timezone.utc).isoformat()}


def retired_profile(root: Path, profile: str) -> dict | None:
    """Recognize only Hermes' explicit, empty native deletion tombstone.

    A disappeared path is never enough: it could be a mount or disk failure.
    This does not alter sharing configuration or remove the retained database.
    """
    if profile == "default" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", profile):
        return None
    profiles = root / "profiles"
    home = profiles / profile
    tombstone = profiles / ".deleted" / profile
    try:
        for directory in (root, profiles, profiles / ".deleted"):
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
                return None
        info = tombstone.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_size != 8 or tombstone.read_bytes() != b"deleted\n"):
            return None
        try:
            info = home.lstat()
        except FileNotFoundError:
            # Native deletion may remove the whole profile instead of leaving
            # its empty SQLite shell. The exact owned marker and available
            # parent directories distinguish this from a missing mount/path.
            return {"available": False, "complete": True, "activeCount": 0,
                "pendingDeliveryCount": 0, "totalCount": 0, "tasks": [],
                "source": "hermes-native-delegation", "retired": True,
                "observedAt": datetime.now(timezone.utc).isoformat()}
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            return None
        allowed = {"state.db", "state.db-shm", "state.db-wal", "state.db.fts_rebuild.lock"}
        entries = list(home.iterdir())
        if any(entry.name not in allowed or not stat.S_ISREG(entry.lstat().st_mode) for entry in entries):
            return None
        path = home / "state.db"
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            if db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone() is not None:
                return None
        evidence = snapshot(home)
        if not evidence["complete"] or evidence["activeCount"] != 0 or evidence["pendingDeliveryCount"] != 0:
            return None
        return {**evidence, "available": False, "retired": True}
    except (OSError, sqlite3.Error):
        return None


def snapshot(home: Path, stored_session_id: str | None = None) -> dict:
    observed_at = datetime.now(timezone.utc).isoformat()
    if stored_session_id is not None and (not isinstance(stored_session_id, str) or not IDENTIFIER.fullmatch(stored_session_id)):
        raise ValueError("Invalid task session")
    path = home / "state.db"
    # profile_home already validates approved profile roots. Reject database
    # aliases separately; this code only opens SQLite in mode=ro/query_only.
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or path.is_symlink():
            return unavailable()
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "async_delegations" not in tables:
                # Audited Hermes persists a dispatch before starting its worker.
                # A readable, initialized session database with no sessions is
                # the one verifiable pre-schema empty case. A missing database,
                # legacy database with sessions, or unreadable schema is unknown.
                if "sessions" in tables and db.execute("SELECT 1 FROM sessions LIMIT 1").fetchone() is None:
                    return {"available": True, "complete": True, "activeCount": 0, "pendingDeliveryCount": 0,
                        "totalCount": 0, "tasks": [], "source": "hermes-native-delegation", "observedAt": observed_at}
                return unavailable()
            # SQL aggregates inspect only lifecycle columns, including tasks
            # whose parent is absent from Control or no longer in the UI list.
            where = " WHERE a.parent_session_id=?" if stored_session_id is not None else ""
            params = (stored_session_id,) if stored_session_id is not None else ()
            scope = " AND parent_session_id=?" if stored_session_id is not None else ""
            active = db.execute("SELECT count(*) FROM async_delegations WHERE state IN ('running','stalling','finalizing','queued','dispatched')" + scope, params).fetchone()[0]
            pending = db.execute("SELECT count(*) FROM async_delegations WHERE state NOT IN ('running','stalling','finalizing','queued','dispatched') AND delivery_state='pending'" + scope, params).fetchone()[0]
            total = db.execute("SELECT count(*) FROM async_delegations a" + where, params).fetchone()[0]
            rows = db.execute("""SELECT a.delegation_id,a.parent_session_id,a.state,a.delivery_state,
                a.dispatched_at,a.updated_at,a.completed_at,s.id
                FROM async_delegations a LEFT JOIN sessions s ON s.id=a.parent_session_id""" + where +
                " ORDER BY a.updated_at DESC,a.delegation_id LIMIT ?", (*params, MAX_TASKS)).fetchall()
        result = {"available": True, "complete": total <= MAX_TASKS, "activeCount": active, "pendingDeliveryCount": pending,
            "totalCount": total, "tasks": [], "source": "hermes-native-delegation", "observedAt": observed_at}
        for identifier, session_id, state, delivery, started, updated, ended, parent in rows:
            if (not isinstance(identifier, str) or len(identifier) > 128 or not IDENTIFIER.fullmatch(identifier)
                    or not isinstance(session_id, str) or not IDENTIFIER.fullmatch(session_id)):
                result["complete"] = False
                continue
            if state not in STATE_MAP or delivery not in DELIVERY_STATES:
                result["complete"] = False
            try:
                task = {"id": identifier, "storedSessionId": session_id, "state": STATE_MAP.get(state, "unknown"),
                    "deliveryState": delivery if delivery in DELIVERY_STATES else "unknown",
                    "title": "Tarea en segundo plano", "createdAt": _timestamp(started), "updatedAt": _timestamp(updated)}
                if ended is not None:
                    task["completedAt"] = _timestamp(ended)
            except (ValueError, OSError, OverflowError):
                result["complete"] = False
                continue
            if parent != session_id:
                # Hermes session deletion does not cascade to this lifecycle
                # ledger. A known terminal record whose delivery is finished
                # is harmless retained history, never a public orphan route.
                # Active, pending or uncertain orphans still fail closed.
                if task["state"] not in {"completed", "failed", "cancelled"} or delivery not in {"delivered", "dropped"}:
                    result["complete"] = False
                continue
            result["tasks"].append(task)
        return result
    except (OSError, sqlite3.Error):
        return unavailable()
