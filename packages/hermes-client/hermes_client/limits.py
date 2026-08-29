from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

import httpx


MAX_UPSTREAM_JSON_BYTES = 2 * 1024 * 1024
MAX_UPSTREAM_JSON_DEPTH = 24
MAX_UPSTREAM_JSON_NODES = 20_000
MAX_UPSTREAM_STRING_BYTES = 256 * 1024


class UpstreamPayloadError(ValueError):
    """Hermes returned a payload outside Control's defensive contract."""


class UpstreamPayloadTooLarge(UpstreamPayloadError):
    pass


class UpstreamPayloadTooDeep(UpstreamPayloadError):
    pass


def validate_json_shape(
    value: Any,
    *,
    max_depth: int = MAX_UPSTREAM_JSON_DEPTH,
    max_nodes: int = MAX_UPSTREAM_JSON_NODES,
) -> None:
    """Validate decoded JSON iteratively so hostile nesting cannot recurse."""

    stack: list[tuple[Any, int]] = [(value, 0)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise UpstreamPayloadTooLarge("Hermes JSON contains too many values")
        if depth > max_depth:
            raise UpstreamPayloadTooDeep("Hermes JSON exceeds the nesting limit")
        if isinstance(current, Mapping):
            for key, nested in current.items():
                if len(str(key).encode("utf-8", errors="replace")) > 8_192:
                    raise UpstreamPayloadTooLarge("Hermes JSON key is too large")
                stack.append((nested, depth + 1))
        elif isinstance(current, Sequence) and not isinstance(
            current, (str, bytes, bytearray)
        ):
            stack.extend((nested, depth + 1) for nested in current)
        elif isinstance(current, str) and len(
            current.encode("utf-8", errors="replace")
        ) > MAX_UPSTREAM_STRING_BYTES:
            raise UpstreamPayloadTooLarge("Hermes JSON string is too large")


async def bounded_json_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    max_bytes: int = MAX_UPSTREAM_JSON_BYTES,
    **kwargs: Any,
) -> Any:
    """Read one JSON response without buffering an unbounded upstream body."""

    async with client.stream(method, path, **kwargs) as response:
        response.raise_for_status()
        content_length = response.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    raise UpstreamPayloadTooLarge("Hermes response is too large")
            except ValueError as exc:
                raise UpstreamPayloadError("Hermes returned an invalid Content-Length") from exc
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > max_bytes:
                raise UpstreamPayloadTooLarge("Hermes response is too large")
            body.extend(chunk)
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamPayloadError("Hermes returned invalid JSON") from exc
    validate_json_shape(value)
    return value


async def bounded_empty_request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    **kwargs: Any,
) -> None:
    """Complete a response while bounding even bodies that should be empty."""

    async with client.stream(method, path, **kwargs) as response:
        response.raise_for_status()
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > 64 * 1024:
                raise UpstreamPayloadTooLarge("Hermes response is too large")
