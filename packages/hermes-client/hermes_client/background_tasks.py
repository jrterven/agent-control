"""Public metadata for Hermes' session-scoped delegated work.

Native goals, progress text and completion summaries are model context, not
task titles. Never derive UI labels from those fields.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_STATES = {
    "dispatched": "queued", "queued": "queued", "pending": "queued",
    "running": "running", "stalling": "running", "finalizing": "running",
    "completed": "completed", "success": "completed",
    "failed": "failed", "error": "failed", "stalled": "failed", "timeout": "failed",
    "cancelled": "cancelled", "canceled": "cancelled", "interrupted": "cancelled",
}


def background_task_id(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def background_task_projection(raw: Mapping[str, Any], *, event_type: str | None = None) -> dict[str, Any]:
    """Allowlist a child lifecycle event without exposing its private context.

    Child IDs and delegation IDs are distinct: one delegation can own several
    children. A child completing therefore never completes the whole group.
    """
    identifier = background_task_id(raw.get("subagent_id"))
    if identifier is None:
        return {"opaque": True}
    state = _STATES.get(raw.get("status"), "unknown") if isinstance(raw.get("status"), str) else "unknown"
    if event_type == "subagent.spawn_requested":
        state = "queued"
    elif event_type in {"subagent.start", "subagent.progress", "subagent.tool"}:
        state = "running"
    result: dict[str, Any] = {"id": identifier, "state": state, "title": "Tarea delegada"}
    for source, target in (("delegation_id", "delegationId"), ("parent_id", "parentTaskId")):
        if value := background_task_id(raw.get(source)):
            result[target] = value
    return result
