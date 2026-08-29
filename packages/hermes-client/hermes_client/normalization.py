from __future__ import annotations

import re
from collections.abc import Mapping
from copy import deepcopy
from math import isfinite
from typing import Any

from .types import NormalizedEvent


_SECRET_KEY = re.compile(
    r"(?:token|secret|password|authorization|api[_-]?key|cookie|credential)", re.I
)
_REASONING_KEY = re.compile(
    r"(?:chain[_-]?of[_-]?thought|reasoning(?:_content)?|internal[_-]?monologue|scratchpad|thinking|thoughts?|analysis|deliberation)",
    re.I,
)
_UNIX_HOME = re.compile(r"/(?:home|Users)/[^/\s]+")
_SECRET_VALUE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|authorization)\b\s*([:=])\s*([^\s,;]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JSON_SECRET_VALUE = re.compile(
    r'''(?i)(["'](?:api[_-]?key|token|secret|password|authorization)["']\s*:\s*)["'][^"']+["']'''
)
_TOKEN_SHAPE = re.compile(
    r"(?i)\b(?:sk-(?:proj-)?[a-z0-9_-]{12,}|gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,}|xox[baprs]-[a-z0-9-]{12,}|AKIA[A-Z0-9]{16}|eyJ[a-z0-9_-]{10,}\.[a-z0-9_-]{10,}\.[a-z0-9_-]{10,})\b"
)
_UNTRUSTED_OUTPUT_KEY = re.compile(
    r"^(?:output|stdout|stderr|raw|arguments?|input|environment|env|headers?)$", re.I
)
_PUBLIC_EVENT_PREFIXES = (
    "gateway.", "message.", "tool.", "approval.", "clarify.", "session.",
    "error", "cron.", "delegation.", "subagent.",
)
_USAGE_NUMERIC_FIELDS = frozenset(
    {
        "input",
        "output",
        "prompt",
        "completion",
        "total",
        "calls",
        "context_used",
        "context_max",
        "context_percent",
        "compressions",
        "active_subagents",
    }
)


class EventNormalizer:
    """Transforms permissive upstream events into a safe public envelope."""

    def __init__(self, *, gateway_id: str, profile_name: str) -> None:
        self.gateway_id = gateway_id
        self.profile_name = profile_name

    def normalize(self, raw: Mapping[str, Any]) -> NormalizedEvent:
        params = raw.get("params") if isinstance(raw.get("params"), Mapping) else {}
        is_event_envelope = raw.get("method") == "event" and bool(params)
        if is_event_envelope:
            payload = params.get("payload") or {}
            metadata = params
            event_type = str(params.get("type") or "hermes.unknown")
        else:
            payload = raw.get("payload") or params or raw.get("data") or {}
            metadata = payload if isinstance(payload, Mapping) else {}
            event_type = str(
                raw.get("event") or raw.get("method") or raw.get("type") or "hermes.unknown"
            )
        if not isinstance(payload, Mapping):
            payload = {"value": payload}
        if event_type.startswith("control."):
            # control.* is reserved for events created inside this process.
            # An upstream frame must never impersonate a trusted global state.
            event_type = "hermes.unknown"
            safe = {"opaque": True, "keys": []}
        elif event_type in {"gateway.health", "gateway.status", "gateway.pong"}:
            safe = {
                key: self._sanitize(payload[key], key)
                for key in ("status", "state", "version", "source_sha", "at")
                if key in payload and isinstance(payload[key], (str, int, float, bool))
            }
        elif _REASONING_KEY.search(event_type):
            event_type = "reasoning.omitted"
            safe = {"omitted": True}
        elif event_type.startswith("message."):
            if self._has_reasoning_discriminator(payload):
                event_type = "reasoning.omitted"
                safe = {"omitted": True}
            else:
                safe = self._project(
                    payload,
                    {
                        "delta", "text", "content", "role", "status",
                        "finish_reason", "fallback", "message_id", "id",
                    },
                )
                if isinstance(payload.get("usage"), Mapping):
                    safe["usage"] = self._project_usage(payload["usage"])
        elif event_type.startswith("tool."):
            safe = self._project(
                payload,
                {
                    "tool_id", "name", "label", "status", "summary", "preview",
                    "duration_s", "duration_ms",
                },
            )
        elif event_type.startswith(("approval.", "clarify.")):
            safe = self._project_interaction(event_type, payload)
        elif event_type == "session.usage":
            usage = payload.get("usage")
            safe = {
                "usage": self._project_usage(
                    usage if isinstance(usage, Mapping) else payload
                )
            }
        elif event_type.startswith(("session.", "run.", "cron.")):
            safe = self._project(
                payload,
                {
                    "status", "running", "title", "reason", "run_id", "job_id",
                    "automation_id", "started_at", "finished_at", "error",
                },
            )
            if event_type.startswith("session.") and isinstance(payload.get("usage"), Mapping):
                safe["usage"] = self._project_usage(payload["usage"])
        elif event_type.startswith(("secret.", "sudo.")):
            safe = {"redacted": True}
        elif not event_type.startswith(_PUBLIC_EVENT_PREFIXES):
            safe = {
                "opaque": True,
                "keys": [
                    str(key)
                    for key in list(payload.keys())[:50]
                    if not _SECRET_KEY.search(str(key))
                ],
            }
        else:
            safe = self._sanitize(dict(payload))
        sequence = self._int_or_none(raw.get("seq", metadata.get("seq", payload.get("seq"))))
        return NormalizedEvent.create(
            event_id=str(raw.get("event_id") or raw.get("id") or "") or None,
            type=event_type,
            gateway_id=self.gateway_id,
            profile_name=self.profile_name,
            stored_session_id=self._str_or_none(
                metadata.get("stored_session_id")
                or metadata.get("session_key")
                or payload.get("stored_session_id")
                or payload.get("session_key"),
                max_length=255,
            ),
            runtime_session_id=self._str_or_none(
                metadata.get("session_id") or payload.get("session_id"),
                max_length=255,
            ),
            sequence=sequence,
            replay_epoch=self._str_or_none(
                raw.get("replay_epoch")
                or metadata.get("replay_epoch")
                or payload.get("replay_epoch"),
                max_length=100,
            ),
            correlation_id=self._str_or_none(
                raw.get("correlation_id")
                or metadata.get("request_id")
                or payload.get("request_id"),
                max_length=200,
            ),
            data=safe,
        )

    def sanitize_data(self, value: Any) -> Any:
        """Sanitize non-event payloads such as recovered message history."""
        return self._sanitize(value)

    def _project(self, payload: Mapping[str, Any], allowed: set[str]) -> dict[str, Any]:
        return {
            str(key): self._sanitize(value, str(key), 1)
            for key, value in payload.items()
            if str(key) in allowed
        }

    def _project_interaction(
        self, event_type: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Project the audited 0.20.5/0.20.6 human-gate contract.

        These payloads are rendered as controls, so accepting arbitrary nested
        extensions would turn an upstream event into browser UI. Keep the
        official fields only and bound every collection independently.
        """

        safe: dict[str, Any] = {}
        request_id = payload.get("request_id")
        if isinstance(request_id, str) and 0 < len(request_id) <= 200:
            safe["request_id"] = request_id
        status = payload.get("status")
        if isinstance(status, str) and len(status) <= 80:
            safe["status"] = status
        if event_type.startswith("approval."):
            for field, maximum in (("description", 10_000), ("command", 20_000)):
                value = payload.get(field)
                if isinstance(value, str):
                    safe[field] = self._sanitize(value, field, 1)[:maximum]
            for field in ("allow_permanent", "allow_session", "smart_denied"):
                if isinstance(payload.get(field), bool):
                    safe[field] = payload[field]
            for field in ("pattern_key",):
                value = payload.get(field)
                if isinstance(value, str) and len(value) <= 1_000:
                    safe[field] = self._sanitize(value, field, 1)
            pattern_keys = payload.get("pattern_keys")
            if isinstance(pattern_keys, list):
                safe["pattern_keys"] = [
                    self._sanitize(value, "pattern_keys", 1)[:1_000]
                    for value in pattern_keys[:50]
                    if isinstance(value, str)
                ]
            choices = payload.get("choices")
            if isinstance(choices, list):
                allowed = {"once", "session", "always", "deny"}
                projected_choices = [
                    value for value in choices[:4]
                    if isinstance(value, str) and value in allowed
                ]
                safe["choices"] = projected_choices or ["deny"]
            return safe

        question = payload.get("question")
        if isinstance(question, str):
            safe["question"] = self._sanitize(question, "question", 1)[:10_000]
        choices = payload.get("choices")
        if isinstance(choices, list):
            safe["choices"] = [
                self._sanitize(value, "choices", 1)[:1_000]
                for value in choices[:100]
                if isinstance(value, str)
            ]
        if isinstance(payload.get("multi_select"), bool):
            safe["multi_select"] = payload["multi_select"]
        questions = payload.get("questions")
        if isinstance(questions, list):
            projected_questions: list[dict[str, Any]] = []
            for item in questions[:50]:
                if not isinstance(item, Mapping):
                    continue
                qid = item.get("qid")
                text = item.get("question")
                if not (
                    isinstance(qid, str)
                    and 0 < len(qid) <= 100
                    and isinstance(text, str)
                ):
                    continue
                projected: dict[str, Any] = {
                    "qid": qid,
                    "question": self._sanitize(text, "question", 2)[:10_000],
                }
                item_choices = item.get("choices")
                if isinstance(item_choices, list):
                    projected["choices"] = [
                        self._sanitize(value, "choices", 2)[:1_000]
                        for value in item_choices[:100]
                        if isinstance(value, str)
                    ]
                if isinstance(item.get("multi_select"), bool):
                    projected["multi_select"] = item["multi_select"]
                projected_questions.append(projected)
            safe["questions"] = projected_questions
        answers = payload.get("answers")
        if isinstance(answers, Mapping):
            projected_answers: dict[str, str | list[str]] = {}
            for key, value in list(answers.items())[:50]:
                if not isinstance(key, str) or not 0 < len(key) <= 100:
                    continue
                if isinstance(value, str):
                    projected_answers[key] = self._sanitize(
                        value, "answer", 2
                    )[:10_000]
                elif isinstance(value, (list, tuple)):
                    projected_answers[key] = [
                        self._sanitize(item, "answer", 3)[:1_000]
                        for item in value[:100]
                        if isinstance(item, str)
                    ]
            safe["answers"] = projected_answers
        return safe

    @staticmethod
    def _project_usage(payload: Mapping[str, Any]) -> dict[str, int | float]:
        """Keep only bounded public counters from Hermes usage snapshots.

        Model names, credit data, arbitrary extensions and reasoning-related
        fields never cross this boundary. Booleans are excluded even though
        Python treats them as integers.
        """

        projected: dict[str, int | float] = {}
        for key in _USAGE_NUMERIC_FIELDS:
            value = payload.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            try:
                numeric = float(value)
            except (OverflowError, ValueError):
                continue
            if not isfinite(numeric) or numeric < 0:
                continue
            maximum = (
                100.0
                if key == "context_percent"
                else 9_007_199_254_740_991.0
            )
            numeric = min(numeric, maximum)
            projected[key] = int(numeric) if numeric.is_integer() else round(numeric, 3)
        return projected

    def _sanitize(self, value: Any, key: str = "", depth: int = 0) -> Any:
        if depth > 24:
            return "[OMITTED: nesting limit]"
        if _SECRET_KEY.search(key):
            return "[REDACTED]"
        if _REASONING_KEY.search(key):
            return "[OMITTED]"
        if _UNTRUSTED_OUTPUT_KEY.search(key):
            return "[OMITTED: untrusted tool data]"
        if isinstance(value, Mapping):
            if self._has_reasoning_discriminator(value):
                return {"type": "reasoning.omitted", "omitted": True}
            return {
                str(k): self._sanitize(v, str(k), depth + 1)
                for k, v in list(value.items())[:500]
            }
        if isinstance(value, list):
            return [self._sanitize(v, key, depth + 1) for v in value[:500]]
        if isinstance(value, tuple):
            return [self._sanitize(v, key, depth + 1) for v in value[:500]]
        if isinstance(value, str):
            redacted = _UNIX_HOME.sub("/[private]", value)
            redacted = _BEARER_VALUE.sub("Bearer [REDACTED]", redacted)
            redacted = _SECRET_VALUE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted)
            redacted = _JSON_SECRET_VALUE.sub(lambda match: f"{match.group(1)}\"[REDACTED]\"", redacted)
            redacted = _TOKEN_SHAPE.sub("[REDACTED TOKEN]", redacted)
            return redacted[:100_000]
        return deepcopy(value)

    @staticmethod
    def _has_reasoning_discriminator(value: Mapping[str, Any]) -> bool:
        return any(
            isinstance(value.get(key), str)
            and bool(_REASONING_KEY.search(str(value[key])))
            for key in ("role", "type", "kind")
        )

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        try:
            parsed = int(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return parsed if parsed is not None and 0 <= parsed <= 9_223_372_036_854_775_807 else None

    @staticmethod
    def _str_or_none(value: Any, *, max_length: int = 512) -> str | None:
        if value in (None, ""):
            return None
        rendered = str(value)
        return rendered if len(rendered) <= max_length else None
