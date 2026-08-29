from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .limits import UpstreamPayloadError, validate_json_shape


AdminResourceName = Literal[
    "models",
    "config",
    "soul",
    "memory",
    "skills",
    "toolsets",
    "mcp",
    "channels",
    "usage",
    "secrets",
]


@dataclass(frozen=True, slots=True)
class AdminResourceSnapshot:
    """Bounded and sanitized projection of one Hermes administration resource."""

    resource: AdminResourceName
    data: dict[str, Any]


_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:api[_-]?key|secret|token|password|passwd|credential|authorization|cookie|private[_-]?key)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_CONTAINER_KEYS = frozenset(
    {"env", "environment", "headers", "auth", "credentials", "secrets"}
)
_PRIVATE_REASONING_KEYS = frozenset(
    {
        "analysis_content",
        "analysis_text",
        "chain_of_thought",
        "reasoning_content",
        "reasoning_text",
        "scratchpad",
        "thinking_content",
        "thinking_text",
    }
)
_REASONING_MARKERS = frozenset(
    {
        "analysis",
        "assistant.analysis",
        "assistant.reasoning",
        "assistant.thinking",
        "chain_of_thought",
        "reasoning",
        "thinking",
    }
)
_PRIVATE_STRUCTURAL_KEYS = frozenset(
    {
        "event_id",
        "id",
        "index",
        "kind",
        "message_id",
        "role",
        "seq",
        "sequence",
        "status",
        "timestamp",
        "type",
    }
)
_OBVIOUS_SECRET = re.compile(
    r"^(?:Bearer\s+\S+|Basic\s+\S+|sk-(?:proj-)?[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{20,})$",
    re.IGNORECASE,
)
_SECRET_VALUE_TYPES = frozenset(
    {
        "api_key",
        "credential",
        "password",
        "private_key",
        "secret",
        "token",
    }
)


def _is_secret_key(key: str) -> bool:
    # Normalize camelCase/PascalCase before token matching. Upstream mixes
    # snake_case (`refresh_token`) with browser-oriented fields
    # (`refreshToken` / `clientSecret`). They must have identical policy.
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key.strip())
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_")
    lowered = normalized.lower()
    return bool(_SECRET_KEY.search(normalized)) and not (
        lowered.startswith(("has_", "is_"))
        or lowered.endswith(("_configured", "_is_set"))
    )


def _configured(value: Any) -> bool:
    if value is None or value is False:
        return False
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized not in {"", "none", "null", "unset", "not set"}
    if isinstance(value, Mapping):
        if "configured" in value:
            return bool(value["configured"])
        if "is_set" in value:
            return bool(value["is_set"])
    return True


def contains_secret_fields(value: Any) -> bool:
    """Return true when a generic config document attempts to carry secrets.

    Secret writes have dedicated provider methods. Keeping them out of the
    generic config mutation avoids accidentally echoing them through a future
    upstream config response or audit record.
    """

    stack: list[tuple[Any, str | None]] = [(value, None)]
    while stack:
        current, parent = stack.pop()
        if isinstance(current, Mapping):
            semantic_name = next(
                (
                    str(current[key])
                    for key in ("key", "name")
                    if isinstance(current.get(key), str)
                ),
                "",
            )
            semantic_type = str(current.get("type") or "").strip()
            normalized_type = re.sub(
                r"[^A-Za-z0-9]+",
                "_",
                re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", semantic_type),
            ).strip("_").casefold()
            if (
                (semantic_name and _is_secret_key(semantic_name))
                or normalized_type in _SECRET_VALUE_TYPES
                or bool(current.get("secret") or current.get("is_secret"))
                or "redacted_value" in {str(key).casefold() for key in current}
            ):
                return True
            for raw_key, nested in current.items():
                key = str(raw_key)
                if _is_secret_key(key) or key.casefold() in _SECRET_CONTAINER_KEYS:
                    return True
                stack.append((nested, key))
        elif isinstance(current, (list, tuple)):
            stack.extend((nested, parent) for nested in current)
        elif isinstance(current, str) and _OBVIOUS_SECRET.fullmatch(current.strip()):
            return True
    return False


def sanitize_admin_payload(value: Any) -> Any:
    """Remove secrets and private reasoning from a bounded upstream document.

    This is deliberately applied inside the Hermes client, before an API
    response can be constructed. API serializers provide a second, typed
    boundary but are not the primary secret filter.
    """

    validate_json_shape(value)
    return _sanitize(value, parent_key=None, secret_context=False)


def writable_config_projection(value: Any) -> Any:
    """Remove redacted secret fields from a sanitized config document.

    Hermes' GET config response is default-expanded and includes empty
    ``api_key`` fields. The generic config endpoint must never echo those
    fields back: secrets have a dedicated write-only API, and a redacted
    ``{"configured": false}`` marker is not a valid Hermes config value.
    """

    if isinstance(value, Mapping):
        projected: dict[str, Any] = {}
        for raw_key, nested in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            if (
                _is_secret_key(key)
                or lowered in _SECRET_CONTAINER_KEYS
                or lowered in _PRIVATE_REASONING_KEYS
                or lowered == "privatereasoning"
            ):
                continue
            projected[key] = writable_config_projection(nested)
        return projected
    if isinstance(value, (list, tuple)):
        return [writable_config_projection(item) for item in value]
    return value


def _sanitize(value: Any, *, parent_key: str | None, secret_context: bool) -> Any:
    if isinstance(value, Mapping):
        if secret_context:
            return {
                str(raw_key): {"configured": _configured(nested)}
                for raw_key, nested in value.items()
            }
        discriminators = [
            str(value[key]).strip().casefold()
            for key in ("role", "type", "kind")
            if key in value and isinstance(value[key], str)
        ]
        private_reasoning = any(
            discriminator in _REASONING_MARKERS
            or any(
                marker in discriminator
                for marker in ("analysis", "thinking", "reasoning")
            )
            for discriminator in discriminators
        )
        if private_reasoning:
            # A reasoning node is private regardless of which field upstream
            # chose for its body (`content`, `payload`, `value`, `data`, ...).
            # Keep only scalar routing/status metadata; do not try to chase an
            # ever-growing list of body aliases.
            structural = {
                str(raw_key): nested
                for raw_key, nested in value.items()
                if str(raw_key).casefold() in _PRIVATE_STRUCTURAL_KEYS
                and (nested is None or isinstance(nested, (str, int, float, bool)))
            }
            structural["privateReasoning"] = True
            return structural
        semantic_name = next(
            (
                str(value[key])
                for key in ("key", "name")
                if isinstance(value.get(key), str)
            ),
            "",
        )
        semantic_type = re.sub(
            r"[^A-Za-z0-9]+",
            "_",
            re.sub(
                r"(?<=[a-z0-9])(?=[A-Z])",
                "_",
                str(value.get("type") or "").strip(),
            ),
        ).strip("_").casefold()
        declared_secret = bool(value.get("secret") or value.get("is_secret")) or (
            bool(semantic_name) and _is_secret_key(semantic_name)
        ) or semantic_type in _SECRET_VALUE_TYPES
        output: dict[str, Any] = {}
        secret_configured = _configured(
            value.get(
                "value",
                value.get(
                    "redacted_value",
                    value.get("is_set", value.get("configured")),
                ),
            )
        )
        for raw_key, nested in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            if lowered in _PRIVATE_REASONING_KEYS:
                continue
            nested_secret_context = secret_context or lowered in _SECRET_CONTAINER_KEYS
            if lowered == "redacted_value":
                continue
            if declared_secret and lowered in {
                "value",
                "default",
                "content",
                "text",
                "payload",
                "data",
            }:
                continue
            if _is_secret_key(key) or (
                lowered == "value" and (secret_context or declared_secret)
            ):
                output[key] = {"configured": _configured(nested)}
                continue
            output[key] = _sanitize(
                nested,
                parent_key=key,
                secret_context=nested_secret_context,
            )
        if declared_secret:
            output["configured"] = secret_configured
        return output
    if isinstance(value, (list, tuple)):
        return [
            _sanitize(item, parent_key=parent_key, secret_context=secret_context)
            for item in value
        ]
    if secret_context:
        return {"configured": _configured(value)}
    if isinstance(value, str) and _OBVIOUS_SECRET.fullmatch(value.strip()):
        return {"configured": True}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise UpstreamPayloadError("Hermes administration payload contains an invalid value")


def admin_snapshot(resource: AdminResourceName, raw: Any) -> AdminResourceSnapshot:
    sanitized = sanitize_admin_payload(raw)
    if isinstance(sanitized, list):
        data: dict[str, Any] = {"items": sanitized}
    elif isinstance(sanitized, dict):
        data = sanitized
    else:
        raise UpstreamPayloadError(
            f"Hermes {resource} response must be an object or list"
        )
    return AdminResourceSnapshot(resource=resource, data=data)
