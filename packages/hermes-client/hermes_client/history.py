"""Project public reply text from Hermes' Responses history extensions."""

import json
from collections.abc import Mapping
from typing import Any

from .background_tasks import background_task_id


def _background_origin(message: Mapping[str, Any]) -> dict[str, str] | None:
    if message.get("role") != "user" or message.get("display_kind") != "async_delegation_complete":
        return None
    origin = {"kind": "background_task"}
    metadata = message.get("display_metadata")
    if isinstance(metadata, str) and len(metadata) <= 16_384:
        try:
            metadata = json.loads(metadata)
        except (ValueError, RecursionError):
            metadata = None
    if isinstance(metadata, Mapping) and (identifier := background_task_id(metadata.get("delegation_id"))):
        origin["taskId"] = identifier
    return origin


def project_history_turn_origins(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark replies to an audited synthetic turn without guessing live origin.

    A real user row ends the scope. Unknown synthetic display kinds end it too,
    so another notification cannot inherit a delegation's identity.
    """
    result = []
    origin = None
    for message in messages:
        if isinstance(message, Mapping) and message.get("role") == "user":
            origin = _background_origin(message)
        row = project_history_message(message)
        if isinstance(row, dict) and row.get("role") == "assistant" and origin is not None:
            row["controlTurnOrigin"] = dict(origin)
        result.append(row)
    return result


def project_history_message(message: Any) -> Any:
    if not isinstance(message, dict):
        return message
    if origin := _background_origin(message):
        # The role=user row is an internal notification prompt containing child
        # results/instructions. Keep its position, never its model input.
        result = {key: message[key] for key in ("id", "message_id", "timestamp", "created_at")
                  if isinstance(message.get(key), (str, int, float)) and not isinstance(message.get(key), bool)}
        result.update(role="system", content="Resultado de una tarea en segundo plano.",
                      controlTurnOrigin=origin)
        return result
    result = dict(message)
    result.pop("controlTurnOrigin", None)  # Reserved for this adapter's projection.
    # REST uses JSON text and RPC a list. Neither raw representation belongs
    # in the public transcript: these extensions can also contain analysis.
    items = result.pop("codex_message_items", None)
    result.pop("codex_reasoning_items", None)
    if result.get("role") != "assistant" or any(
        isinstance(result.get(key), str) and result[key].strip()
        for key in ("content", "text")
    ):
        return result
    if isinstance(items, str):
        if len(items) > 1_048_576:
            return result
        try:
            items = json.loads(items)
        except (ValueError, RecursionError):
            return result
    if not isinstance(items, list) or len(items) > 256:
        return result
    texts: list[str] = []
    size = 0
    for item in items:
        if (
            not isinstance(item, Mapping)
            or item.get("type") != "message"
            or item.get("role") != "assistant"
        ):
            continue
        if item.get("phase") not in (None, "", "final", "final_answer"):
            continue
        if item.get("channel") not in (None, "", "final", "final_answer"):
            continue
        if item.get("status") not in (None, "", "completed"):
            continue
        content = item.get("content")
        if not isinstance(content, list) or len(content) > 64:
            continue
        for part in content:
            if not isinstance(part, Mapping) or part.get("type") not in ("text", "output_text"):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            size += len(text)
            if size > 65_536:
                return result
            texts.append(text)
    if texts:
        # Supply the existing public field so UI hydration and prompt recovery
        # share the same final-text contract without a vendor-specific UI path.
        result["content" if "content" in result else "text"] = "".join(texts)
    return result
