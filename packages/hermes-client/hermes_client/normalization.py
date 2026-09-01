from __future__ import annotations

import re
from collections import OrderedDict
from collections.abc import Mapping
from copy import deepcopy
from math import isfinite
from typing import Any

from .email_references import (
    EMAIL_REFERENCE_MARKER_NAME,
    email_reference_candidates,
    parse_email_reference_marker,
    project_email_reference_prompt,
)
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
_EMAIL_STREAM_MAX_FRAGMENTS = 64
_EMAIL_STREAM_MAX_TOMBSTONES = 512
_EMAIL_STREAM_MAX_BUFFER_BYTES = 20 * 1024
_HTML_COMMENT_PREFIX = "<!--"
_EMAIL_STREAM_TERMINAL_TYPES = frozenset(
    {
        "message.cancelled",
        "message.complete",
        "message.completed",
        "message.done",
        "message.error",
        "message.failed",
        "message.interrupted",
    }
)


class EventNormalizer:
    """Transforms permissive upstream events into a safe public envelope."""

    def __init__(self, *, gateway_id: str, profile_name: str) -> None:
        self.gateway_id = gateway_id
        self.profile_name = profile_name
        # The client retains this normalizer across reconnect/replay. Buffers
        # are independently keyed by route/message so a replayed marker tail
        # remains quarantined even when the transport reconnects mid-marker.
        self._email_stream_fragments: OrderedDict[
            tuple[str, str, str], str
        ] = OrderedDict()
        self._email_stream_tombstones: OrderedDict[
            tuple[str, str, str], None
        ] = OrderedDict()
        self._email_stream_global_quarantine = False

    def normalize(self, raw: Mapping[str, Any]) -> NormalizedEvent:
        private_email_references: list[Any] = []
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
        if event_type in {"message.start", "message.started"}:
            self._discard_email_streams(payload, metadata)
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
                references = []
                for field in ("emailReferences", "email_references"):
                    references.extend(email_reference_candidates(payload.get(field)))
                content_fields = [
                    field
                    for field in ("delta", "text", "content")
                    if isinstance(payload.get(field), str)
                ]
                primary_content_field = content_fields[0] if content_fields else None
                for field in content_fields:
                    if field != primary_content_field:
                        safe.pop(field, None)
                        continue
                    value = str(payload[field])
                    if event_type == "message.delta" or self._has_email_stream(
                        payload, metadata
                    ):
                        cleaned, embedded = self._project_email_stream_chunk(
                            payload,
                            metadata,
                            value,
                        )
                    else:
                        cleaned, embedded = project_email_reference_prompt(value)
                    safe[field] = self._sanitize(cleaned, field, 1)
                    references.extend(embedded)
                if references:
                    projected: list[dict[str, Any]] = []
                    seen: set[str] = set()
                    for reference in references:
                        if reference.fingerprint in seen:
                            continue
                        seen.add(reference.fingerprint)
                        projected.append(reference.transport_view())
                        private_email_references.append(reference.private_payload())
                        if len(projected) == 8:
                            break
                    safe["controlEmailReferences"] = projected
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
            references = []
            for field in ("emailReferences", "email_references"):
                references.extend(email_reference_candidates(payload.get(field)))
            if references:
                safe["controlEmailReferences"] = [
                    reference.transport_view() for reference in references[:8]
                ]
                private_email_references.extend(
                    reference.private_payload() for reference in references[:8]
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
        if event_type in _EMAIL_STREAM_TERMINAL_TYPES:
            self._discard_email_streams(payload, metadata)
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
            private_data=(
                {"emailReferences": private_email_references}
                if private_email_references
                else None
            ),
        )

    def _project_email_stream_chunk(
        self,
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
        chunk: str,
    ) -> tuple[str, list[Any]]:
        key = self._active_email_stream_key(payload, metadata)
        if self._email_stream_global_quarantine:
            return "", []
        if key in self._email_stream_tombstones:
            self._email_stream_tombstones.move_to_end(key)
            return "", []
        if key in self._email_stream_fragments:
            fragment = self._email_stream_fragments.pop(key)
        else:
            fragment = ""
        combined = fragment + chunk
        visible: list[str] = []
        references: list[Any] = []
        while combined:
            marker_index = combined.find(_HTML_COMMENT_PREFIX)
            if marker_index >= 0:
                visible.append(combined[:marker_index])
                combined = combined[marker_index:]
                marker_state = self._email_comment_state(combined)
                if marker_state == "email":
                    parsed = parse_email_reference_marker(combined)
                    if parsed is None:
                        self._remember_email_stream(key, combined)
                        return "".join(visible), references
                    if parsed.candidate is not None:
                        references.append(parsed.candidate)
                    combined = combined[parsed.end:]
                    continue
                if marker_state == "possible":
                    self._remember_email_stream(key, combined)
                    return "".join(visible), references
                comment_end = combined.find("-->", len(_HTML_COMMENT_PREFIX))
                if comment_end < 0:
                    self._remember_email_stream(key, combined)
                    return "".join(visible), references
                combined = combined[comment_end + 3 :]
                continue

            suffix_length = self._email_marker_prefix_suffix_length(combined)
            if suffix_length:
                visible.append(combined[:-suffix_length])
                self._remember_email_stream(key, combined[-suffix_length:])
            else:
                visible.append(combined)
            break
        return "".join(visible), references

    def _remember_email_stream(
        self,
        key: tuple[str, str, str],
        value: str,
    ) -> None:
        if len(value.encode("utf-8")) > _EMAIL_STREAM_MAX_BUFFER_BYTES:
            self._quarantine_email_stream(key)
            return
        if key not in self._email_stream_fragments and len(
            self._email_stream_fragments
        ) >= _EMAIL_STREAM_MAX_FRAGMENTS:
            evicted, _ = self._email_stream_fragments.popitem(last=False)
            self._quarantine_email_stream(evicted)
            if self._email_stream_global_quarantine:
                return
        self._email_stream_fragments[key] = value
        self._email_stream_fragments.move_to_end(key)

    def _quarantine_email_stream(self, key: tuple[str, str, str]) -> None:
        self._email_stream_fragments.pop(key, None)
        if key in self._email_stream_tombstones:
            self._email_stream_tombstones.move_to_end(key)
            return
        if len(self._email_stream_tombstones) >= _EMAIL_STREAM_MAX_TOMBSTONES:
            # Never evict a quarantine marker and accidentally release its
            # private tail. A hostile cardinality overflow fails closed.
            self._email_stream_global_quarantine = True
            return
        self._email_stream_tombstones[key] = None

    @staticmethod
    def _email_marker_prefix_suffix_length(value: str) -> int:
        maximum = min(len(value), len(_HTML_COMMENT_PREFIX) - 1)
        for length in range(maximum, 0, -1):
            if value.endswith(_HTML_COMMENT_PREFIX[:length]):
                return length
        return 0

    @staticmethod
    def _email_comment_state(value: str) -> str:
        """Classify a buffered HTML comment without exposing near-markers."""

        if not value.startswith(_HTML_COMMENT_PREFIX):
            return "other"
        remainder = value[len(_HTML_COMMENT_PREFIX) :].lstrip()
        lowered = remainder.casefold()
        if not lowered or EMAIL_REFERENCE_MARKER_NAME.startswith(lowered):
            return "possible"
        if lowered.startswith(EMAIL_REFERENCE_MARKER_NAME):
            boundary = remainder[len(EMAIL_REFERENCE_MARKER_NAME) : len(EMAIL_REFERENCE_MARKER_NAME) + 1]
            return "email" if not boundary or boundary.isspace() or boundary == "{" else "other"
        return "other"

    @staticmethod
    def _email_stream_route(
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> tuple[str, str, str]:
        stored = str(
            metadata.get("stored_session_id")
            or metadata.get("session_key")
            or payload.get("stored_session_id")
            or payload.get("session_key")
            or ""
        )[:255]
        runtime = str(
            metadata.get("session_id") or payload.get("session_id") or ""
        )[:255]
        message = str(
            payload.get("message_id") or payload.get("messageId") or ""
        )[:255]
        return stored, runtime, message

    def _discard_email_streams(
        self,
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> None:
        stored, runtime, message = self._email_stream_route(payload, metadata)
        for collection in (
            self._email_stream_fragments,
            self._email_stream_tombstones,
        ):
            for key in tuple(collection):
                key_stored, key_runtime, key_message = key
                if stored and key_stored == stored:
                    collection.pop(key, None)
                elif runtime and key_runtime == runtime:
                    collection.pop(key, None)
                elif message and key_message == message:
                    collection.pop(key, None)

    def _has_email_stream(
        self,
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> bool:
        key = self._active_email_stream_key(payload, metadata)
        return (
            self._email_stream_global_quarantine
            or key in self._email_stream_fragments
            or key in self._email_stream_tombstones
        )

    def _active_email_stream_key(
        self,
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any],
    ) -> tuple[str, str, str]:
        exact = self._email_stream_route(payload, metadata)
        if exact in self._email_stream_fragments or exact in self._email_stream_tombstones:
            return exact
        stored, runtime, _ = exact
        matches = [
            key
            for key in (*self._email_stream_fragments, *self._email_stream_tombstones)
            if (stored and key[0] == stored) or (runtime and key[1] == runtime)
        ]
        return matches[0] if len(matches) == 1 else exact

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
