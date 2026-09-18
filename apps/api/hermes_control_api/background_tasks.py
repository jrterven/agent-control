"""Conversation-bound, bounded metadata for native background delegations."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from hermes_client.background_tasks import background_task_id

MAX_TASKS = 200
STATES = {"queued", "running", "completed", "failed", "cancelled", "unknown"}
DELIVERY_STATES = {"pending", "delivered", "dropped", "unknown"}


def timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, OverflowError):
        return None


def project_snapshot(raw: Any, stored_session_id: str, *, observed_at: str) -> dict:
    """Never derive labels from private delegation goals or result bodies."""
    raw = raw if isinstance(raw, dict) else {}
    tasks = raw.get("tasks", raw.get("items"))
    tasks = tasks if isinstance(tasks, list) else []
    available = raw.get("available") is True
    complete = available and raw.get("complete") is True and len(tasks) <= MAX_TASKS
    items, seen = [], set()
    for item in tasks[:MAX_TASKS]:
        if not isinstance(item, dict) or item.get("storedSessionId", stored_session_id) != stored_session_id:
            complete = False
            continue
        identifier = background_task_id(item.get("id"))
        created, updated = timestamp(item.get("createdAt")), timestamp(item.get("updatedAt"))
        if identifier is None or identifier in seen or created is None or updated is None:
            complete = False
            continue
        seen.add(identifier)
        state, delivery = item.get("state"), item.get("deliveryState")
        state = state if isinstance(state, str) else "unknown"
        delivery = delivery if isinstance(delivery, str) else "unknown"
        if state not in STATES or delivery not in DELIVERY_STATES:
            complete = False
        projected = {"id": identifier, "state": state if state in STATES else "unknown",
            "deliveryState": delivery if delivery in DELIVERY_STATES else "unknown",
            "title": "Tarea en segundo plano", "createdAt": created, "updatedAt": updated}
        if ended := timestamp(item.get("completedAt")):
            projected["completedAt"] = ended
        items.append(projected)
    def count(name):
        value = raw.get(name)
        return value if available and type(value) is int and 0 <= value <= 1_000_000 else None
    return {"items": items, "available": available, "complete": complete,
        "activeCount": count("activeCount"), "pendingDeliveryCount": count("pendingDeliveryCount"),
        "observedAt": timestamp(raw.get("observedAt")) or observed_at}


def persist_snapshot(row, snapshot: dict) -> dict:
    previous = dict(row.background_tasks or {})
    if previous.get("observedAt", "") > snapshot["observedAt"]:
        return previous
    # Missing/truncated observations cannot erase previously observed tasks.
    if not snapshot["complete"]:
        current = {item["id"]: item for item in previous.get("items", [])}
        current.update({item["id"]: item for item in snapshot["items"]})
        snapshot = {**snapshot, "items": sorted(current.values(), key=lambda x: x["updatedAt"], reverse=True)[:MAX_TASKS]}
    if not snapshot["available"]:
        snapshot = {**snapshot, "items": [
            {**item, "state": "unknown"} if item["state"] in {"queued", "running"} else item
            for item in snapshot["items"]]}
    row.background_tasks = snapshot
    return snapshot


def unavailable_snapshot(row, *, observed_at: str) -> dict:
    old = project_snapshot(row.background_tasks, row.stored_session_id, observed_at=observed_at)
    return {**old, "available": False, "complete": False, "activeCount": None, "pendingDeliveryCount": None,
        "items": [{**item, "state": "unknown"} if item["state"] in {"queued", "running"} else item for item in old["items"]]}
