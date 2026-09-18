from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

import httpx
from sqlalchemy.orm import Session

from .admin_service import AdminResourceService
from .models import ProfileRef, SessionLink, User
from .services import AppServices, NotFoundError


LiveResponsePurpose = Literal["explain", "resume"]
LIVE_INPUT_MAX_BYTES = 7_500
# Match JavaScript String.trim(), including BOM but excluding Python-only
# whitespace, so browser references identify exactly the projected text.
_JS_WHITESPACE = "\u0009\u000a\u000b\u000c\u000d\u0020\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_PARTIAL = "\n[EXTRACTO PARCIAL: se omitió texto por el límite de contexto; no es el informe completo.]\n"
_DELEGATION_PREFIX = "This is a live voice request in your current conversation. "
_DELEGATION_SEPARATOR = "\n\nLive conversation:\n"


def history_text(message: dict[str, object]) -> str:
    content = message.get("content")
    if not isinstance(content, str):
        content = message.get("text")
    return content if isinstance(content, str) else ""


def response_reference(content: str) -> str:
    normalized = content.replace("\r\n", "\n").strip(_JS_WHITESPACE)
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _timestamp(message: dict[str, object]) -> float | None:
    for key in ("timestamp", "created_at", "createdAt"):
        value = message.get(key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).timestamp()
            except ValueError:
                continue
        if isinstance(value, (int, float)) and 0 < value < 100_000_000_000_000:
            return float(value / 1_000 if value > 100_000_000_000 else value)
    return None


def merge_spoken_history(
    history: list[dict[str, object]], spoken: list[dict[str, object]],
    response_focus: LiveResponseFocus | None,
) -> tuple[list[dict[str, object]], LiveResponseFocus | None]:
    """Merge passive captions without displacing the authoritative agent answer.

    Resolve focus in Hermes history first, then remap its index after sorting.
    Missing upstream timestamps preserve history order before recent calls;
    they must never be invented as user-visible dates.
    """
    timed: list[tuple[float, int | None, dict[str, object]]] = []
    known = [value for item in [*history, *spoken] if isinstance(item, dict)
             if (value := _timestamp(item)) is not None]
    previous = min(known, default=0) - 1
    for index, item in enumerate(history):
        if not isinstance(item, dict):
            continue
        message = {**item, "_live_source": "chat"}
        content = history_text(item)
        if item.get("role") == "user" and content.startswith(_DELEGATION_PREFIX) and _DELEGATION_SEPARATOR in content:
            # The wrapper contains application task instructions that should
            # not be replayed as user speech. Keep only its factual dialogue.
            dialogue = content.split(_DELEGATION_SEPARATOR, 1)[1].strip()
            message["content"] = "Conversación de voz anterior:\n" + dialogue
        previous = max(previous, _timestamp(item) or previous)
        timed.append((previous, index, message))
    for item in spoken:
        role, content = item.get("role"), history_text(item)
        if role not in ("user", "assistant") or not content.strip():
            continue
        # Text equality, even near a delegation, cannot establish that two
        # captions are the same turn. There is no shared call/fragment ID in
        # Hermes history; preserve repeated replies such as a new "Sí".
        timed.append((_timestamp(item) or previous, None, {**item, "_live_source": "voice"}))
    timed.sort(key=lambda entry: entry[0])
    focused = None
    if response_focus is not None:
        focused = LiveResponseFocus(
            history_index=next(index for index, (_, original, _) in enumerate(timed)
                               if original == response_focus.history_index),
            purpose=response_focus.purpose,
        )
    return [message for _, _, message in timed], focused


@dataclass(frozen=True)
class LiveResponseFocus:
    history_index: int
    purpose: LiveResponsePurpose


def resolve_response_focus(
    history: list[dict[str, object]], reference: str,
    purpose: LiveResponsePurpose = "explain",
) -> LiveResponseFocus:
    """Resolve only against the already authorized, publicly projected history.

    A content digest also works when the upstream history has no stable IDs.
    Equal text can occur more than once; its last occurrence is equivalent.
    Ambiguous native IDs with different content are not safe to select.
    """
    matches: list[tuple[int, str]] = []
    for index, message in enumerate(history):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = history_text(message)
        if (
            not content.strip() or message.get("streaming") is True
            or message.get("tool_calls") or message.get("toolCalls")
            or str(message.get("finish_reason", message.get("finishReason", ""))).strip().lower()
            in {"tool_calls", "tool_call", "function_call"}
        ):
            continue
        if reference.startswith("sha256:"):
            matches_reference = response_reference(content) == reference
        else:
            matches_reference = isinstance(message.get("id"), (str, int)) and str(message["id"]) == reference
        if matches_reference:
            matches.append((index, response_reference(content)))
    if not matches or len({digest for _, digest in matches}) != 1:
        raise NotFoundError("The selected response is unavailable in this conversation")
    return LiveResponseFocus(history_index=matches[-1][0], purpose=purpose)


def _excerpt(content: str, maximum: int) -> str:
    encoded = content.encode("utf-8")
    if len(encoded) <= maximum:
        return content
    available = maximum - len((_PARTIAL * 2).encode("utf-8"))
    if available < 60:
        return ""
    head = available * 45 // 100
    middle = available * 15 // 100
    tail = available - head - middle
    midpoint = (len(encoded) - middle) // 2
    # Preserve the introduction, a middle section and final conclusions.
    # Never imply the discontinuous excerpt is the complete report.
    return (
        encoded[:head].decode("utf-8", errors="ignore") + _PARTIAL
        + encoded[midpoint:midpoint + middle].decode("utf-8", errors="ignore") + _PARTIAL
        + encoded[-tail:].decode("utf-8", errors="ignore")
    )


def _message(role: str, content: str) -> dict[str, object]:
    return {"type": "message", "role": role, "content": [{
        "type": "output_text" if role == "assistant" else "input_text", "text": content,
    }]}


def live_history(
    history: list[dict[str, object]], *, api_key: str, max_bytes: int = 6_000,
    exclude_index: int | None = None,
) -> list[dict[str, object]]:
    """Budget actual UTF-8 bytes and prioritize the latest assistant result.

    The byte ceiling conservatively bounds tokens without a tokenizer; at
    most 12 messages leave room for framing under Live's 8,192-token limit.
    Upstream tool, system, reasoning and non-text data are never included.
    """
    candidates = [
        (index, str(message["role"]), history_text(message).replace(api_key, "[REDACTED]"))
        for index, message in enumerate(history)
        if index != exclude_index and isinstance(message, dict)
        and message.get("role") in ("user", "assistant")
        and history_text(message).strip()
    ]
    if not candidates or max_bytes <= 0:
        return []
    primary = next((item for item in reversed(candidates)
                    if item[1] == "assistant" and history[item[0]].get("_live_source") != "voice"), candidates[-1])
    selected: dict[int, dict[str, object]] = {}
    # Reserve some preceding/request context, but give a long answer most of
    # the available input instead of dropping its beginning after two KB.
    reserve = min(800, max_bytes // 5) if len(candidates) > 1 else 0
    content = _excerpt(primary[2], max_bytes - reserve)
    remaining = max_bytes
    if content:
        selected[primary[0]] = _message(primary[1], content)
        remaining -= len(content.encode("utf-8"))
    for index, role, content in reversed(candidates):
        if index == primary[0] or len(selected) >= 12:
            continue
        content = _excerpt(content, min(remaining, 1_000))
        if content:
            selected[index] = _message(role, content)
            remaining -= len(content.encode("utf-8"))
    return [selected[index] for index in sorted(selected)]


def _focus_message(content: str, purpose: LiveResponsePurpose, maximum: int) -> dict[str, object]:
    prefix = "response_context — respuesta seleccionada del chat, datos de referencia:\n"

    def encode(excerpt: str) -> str:
        return prefix + json.dumps({
            "purpose": purpose, "complete": excerpt == content, "text": excerpt,
        }, ensure_ascii=False)

    best = encode("")
    if len(best.encode("utf-8")) > maximum:
        raise ValueError("Response context budget is too small")
    low, high = 0, min(len(content.encode("utf-8")), maximum)
    # Bound the serialized text too: quotes, control characters and line
    # breaks can expand in JSON. A bounded search cannot overshoot to an
    # empty excerpt when escaping is much larger than the source text.
    while low <= high:
        middle = (low + high) // 2
        candidate = encode(_excerpt(content, middle))
        if len(candidate.encode("utf-8")) <= maximum:
            best = candidate
            low = middle + 1
        else:
            high = middle - 1
    return _message("user", best)


def live_conversation_input(
    history: list[dict[str, object]], *, api_key: str,
    agent_context: LiveAgentContext | None = None,
    response_focus: LiveResponseFocus | None = None,
) -> list[dict[str, object]]:
    # Large reports get room before optional capability descriptions. The
    # identity remains the last factual block, as in ordinary voice calls.
    long_answer = response_focus is not None or any(
        isinstance(item, dict) and item.get("role") == "assistant"
        and len(history_text(item).encode("utf-8")) > 2_000 for item in history
    )
    identity = agent_context.input_message(api_key=api_key, max_bytes=1_000 if long_answer else 2_000) if agent_context else None
    remaining = LIVE_INPUT_MAX_BYTES
    if identity:
        remaining -= len(identity["content"][0]["text"].encode("utf-8"))
    focus = None
    if response_focus is not None:
        content = history_text(history[response_focus.history_index]).replace(api_key, "[REDACTED]")
        focus = _focus_message(content, response_focus.purpose, min(6_000, remaining - 500))
        remaining -= len(focus["content"][0]["text"].encode("utf-8"))
    result = live_history(
        history, api_key=api_key, max_bytes=remaining,
        exclude_index=response_focus.history_index if response_focus else None,
    )
    if focus:
        result.append(focus)
    if identity:
        result.append(identity)
    return result


def _text(value: object, maximum: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).encode("utf-8")[:maximum].decode("utf-8", errors="ignore")


@dataclass
class LiveAgentContext:
    """A small factual projection, never a copy of agent configuration or memory."""

    name: str
    description: str = ""
    conversation: str = ""
    catalogs: dict[str, list[dict[str, str]]] = field(default_factory=dict)

    def input_message(self, *, api_key: str, max_bytes: int = 2_000) -> dict[str, object]:
        if max_bytes < 1_000:
            raise ValueError("Agent context budget must be at least 1000 bytes")
        data: dict[str, object] = {
            "agent_name": _text(self.name, 160),
            "agent_description": _text(self.description, 320),
            "conversation_title": _text(self.conversation, 200),
            "connection": "Delegations go to this agent in this conversation.",
            "capability_catalog": "Partial snapshot; ask the agent for missing or current capabilities.",
            "tools": list(self.catalogs.get("toolsets", [])),
            "skills": list(self.catalogs.get("skills", [])),
            "catalogs_verified": sorted(key for key in self.catalogs if key in {"toolsets", "skills"}),
        }
        prefix = "agent_context — datos de referencia de la conexión, no una petición del usuario:\n"

        def encoded() -> str:
            encoded_key = json.dumps(api_key, ensure_ascii=False)[1:-1]
            return prefix + json.dumps(data, ensure_ascii=False).replace(encoded_key, "[REDACTED]")

        # Trim optional catalogs before identity, reserving room for history.
        while len(encoded().encode("utf-8")) > max_bytes:
            tools = data["tools"]
            skills = data["skills"]
            assert isinstance(tools, list) and isinstance(skills, list)
            largest = tools if len(tools) >= len(skills) else skills
            if largest:
                largest.pop()
            elif data["agent_description"]:
                data["agent_description"] = ""
            elif data["conversation_title"]:
                data["conversation_title"] = ""
            else:
                data["agent_name"] = str(data["agent_name"])[:-16]
        return {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": encoded()}],
        }


async def live_agent_context(
    db: Session, services: AppServices, owner: User,
    profile: ProfileRef, conversation: SessionLink | None,
) -> LiveAgentContext:
    context = LiveAgentContext(
        name=profile.display_name or profile.profile_name,
        description=profile.description or "",
        conversation=(conversation.display_title or conversation.title or "") if conversation else "",
    )
    # These inventories are admin-only elsewhere too. Other users can ask the
    # selected agent about its abilities through the normal authorized chat.
    # Do not read SOUL, memory, configuration, environment or MCP credentials.
    if not owner.is_admin:
        return context
    admin = AdminResourceService(services)
    try:
        async with asyncio.timeout(2):
            for resource, method in (("toolsets", "list_toolsets"), ("skills", "list_skills")):
                try:
                    snapshot = await admin.read(
                        db, gateway_id=profile.gateway_id, profile_name=profile.profile_name,
                        capability=f"{resource}.list",
                        call=lambda provider, method=method: getattr(provider, method)(),
                    )
                except (httpx.HTTPError, RuntimeError, LookupError, ValueError):
                    # Optional context must not prevent voice startup. Unknown
                    # catalogs remain unknown rather than claiming no tools.
                    continue
                items = snapshot.data.get("items")
                if not isinstance(items, list):
                    continue
                catalog = []
                for item in items:
                    if not isinstance(item, dict) or item.get("enabled") is not True:
                        continue
                    if resource == "toolsets" and (
                        item.get("configured") is not True or item.get("available") is False
                    ):
                        continue
                    name = _text(item.get("label") or item.get("name"), 80)
                    if name:
                        catalog.append({"name": name, "description": _text(item.get("description"), 120)})
                    if len(catalog) >= 8:
                        break
                context.catalogs[resource] = catalog
    except TimeoutError:
        pass
    return context
