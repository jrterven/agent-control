"""Project public reply text from Hermes' Responses history extensions."""

import json
from collections.abc import Mapping
from typing import Any


def project_history_message(message: Any) -> Any:
    if not isinstance(message, dict):
        return message
    result = dict(message)
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
