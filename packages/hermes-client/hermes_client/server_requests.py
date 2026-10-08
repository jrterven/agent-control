"""Bounded contract-8 questions translated to Control's existing human gates.

The upstream request ID never grants authority by itself: the provider also
binds it to a route and a transport generation before exposing or answering it.
Unsupported desktop/secret requests are rejected by the transport.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .types import SessionRoute

_REQUEST_ID = re.compile(r"srq-[0-9a-f]{12}\Z")
_CHOICES = frozenset({"once", "session", "always", "deny"})


def valid_request_id(value: Any) -> bool:
    return isinstance(value, str) and _REQUEST_ID.fullmatch(value) is not None


@dataclass
class ServerRequestBinding:
    identifier: str
    method: str
    route: SessionRoute
    generation: str
    payload: dict[str, Any]
    question_ids: frozenset[str]
    locked_ids: frozenset[str]


@dataclass(frozen=True)
class ServerRequestSnapshot:
    generation: str
    revision: int
    bindings: frozenset[tuple[str, str, SessionRoute]]


def request_payload(raw: Mapping[str, Any]) -> tuple[str, str, str, dict[str, Any]] | None:
    identifier, method, params = raw.get("id"), raw.get("method"), raw.get("params")
    if (not valid_request_id(identifier)
            or not isinstance(method, str) or method not in {"approval", "clarify"}
            or not isinstance(params, Mapping)):
        return None
    sid = params.get("session_id")
    if not isinstance(sid, str) or not 0 < len(sid) <= 255:
        return None
    # Retain only the fields the human gate actually uses, never arbitrary
    # extension data (which can contain credentials or unbounded sidecars).
    payload = {"session_id": sid, "request_id": identifier}
    if method == "approval":
        choices = params.get("choices")
        if (not isinstance(choices, list) or not 0 < len(choices) <= 4
                or any(not isinstance(choice, str) or choice not in _CHOICES for choice in choices)):
            return None
        if any(field in params and (not isinstance(params[field], str) or len(params[field]) > limit)
               for field, limit in (("command", 20_000), ("description", 10_000))):
            return None
        if any(field in params and type(params[field]) is not bool
               for field in ("allow_session", "allow_permanent", "smart_denied")):
            return None
        pattern_keys = params.get("pattern_keys")
        if pattern_keys is not None and (not isinstance(pattern_keys, list) or len(pattern_keys) > 50
                or any(not isinstance(key, str) or len(key) > 1_000 for key in pattern_keys)):
            return None
        if "pattern_key" in params and (not isinstance(params["pattern_key"], str) or len(params["pattern_key"]) > 1_000):
            return None
        payload.update({field: params[field] for field in ("command", "description", "choices", "allow_session",
            "allow_permanent", "smart_denied", "pattern_key", "pattern_keys") if field in params})
    else:
        questions = params.get("questions")
        if not isinstance(questions, list) or not 0 < len(questions) <= 5:
            return None
        qids = set()
        for question in questions:
            if not isinstance(question, Mapping):
                return None
            qid, text = question.get("qid"), question.get("question")
            if (not isinstance(qid, str) or not 0 < len(qid) <= 100 or qid in qids
                    or not isinstance(text, str) or not 0 < len(text) <= 10_000
                    or type(question.get("multi_select", False)) is not bool):
                return None
            choices = question.get("choices")
            if choices is not None and (not isinstance(choices, list) or len(choices) > 100
                    or any(not isinstance(choice, str) or len(choice) > 1_000 for choice in choices)):
                return None
            qids.add(qid)
        answers = params.get("answers", {})
        if (not isinstance(answers, Mapping) or not set(answers).issubset(qids)
                or any(value is not None and (not isinstance(value, str) or len(value) > 10_000)
                       for value in answers.values())):
            return None
        payload["questions"] = [{field: question[field] for field in ("qid", "question", "choices", "multi_select")
                                  if field in question} for question in questions]
        payload["answers"] = dict(answers)
    if len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) > 16_384:
        return None  # Do not accept a gate larger than Control can retain/render.
    return identifier, method, sid, payload
