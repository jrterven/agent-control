"""Versioned connector protocol: fixed Hermes operations, bounded binary messages.

Wire values are explicit, allowlisted data types. No pickle, eval, URLs, filesystem
commands, or dynamically selected modules are accepted by this transport.
"""
from __future__ import annotations

import json
import struct
import time
import types as python_types
from functools import lru_cache
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints
from dataclasses import fields, is_dataclass
from datetime import datetime
from uuid import uuid4

from . import types
from .admin import AdminResourceSnapshot
from .limits import validate_json_shape

VERSION = 1
MAX_FRAME_BYTES = 256 * 1024
MAX_JSON_BYTES = 2 * 1024 * 1024
MAX_BLOB_BYTES = 50 * 1024 * 1024
MAX_MESSAGE_BYTES = MAX_JSON_BYTES + MAX_BLOB_BYTES + 4
_HEADER = struct.Struct("!4s16sII")
_MAGIC = b"ACC1"
TYPES = {cls.__name__: cls for cls in (
    types.SessionRoute, types.CapabilitySet, types.HermesProfile, types.HermesSession,
    types.HermesSearchResult, types.PromptReceipt, types.PromptAttachment,
    types.PromptAttachmentReceipt, types.HermesAutomation, types.HermesRunReceipt,
    types.NormalizedEvent, AdminResourceSnapshot,
)}
READ_OPERATIONS = frozenset({
    "capabilities", "list_profiles", "list_sessions", "search_sessions", "history",
    "history_readonly", "history_page", "list_automations", "list_automation_runs", "list_models",
    "get_config", "get_transfer_config", "get_soul", "get_memory", "list_skills", "list_toolsets",
    "list_mcp_servers", "list_channels", "get_usage", "list_secrets", "media", "list_background_tasks",
    "profile_archive_read",
})
WRITE_OPERATIONS = frozenset({
    "create_profile", "delete_profile", "create_session", "resume_session",
    "renew_temporary_session", "close_temporary_session",
    "submit_prompt", "attach_prompt_attachment", "detach_prompt_images", "interrupt",
    "respond_approval", "respond_clarification", "delete_session", "create_automation",
    "update_automation", "delete_automation", "trigger_automation", "set_model",
    "update_config", "replace_config", "update_soul", "set_memory_provider", "reset_memory",
    "toggle_skill", "toggle_toolset", "create_mcp_server", "delete_mcp_server",
    "toggle_mcp_server", "test_mcp_server", "update_channel", "test_channel", "set_secret",
    "delete_secret",
    "profile_export", "profile_import_begin", "profile_archive_write",
    "profile_import_finish", "profile_archive_cleanup",
})
OPERATIONS = READ_OPERATIONS | WRITE_OPERATIONS


class ProtocolError(ValueError):
    pass



def value_matches_type(value: Any, annotation: Any) -> bool:
    if annotation is Any:
        return True
    if annotation is None or annotation is type(None):
        return value is None
    origin, arguments = get_origin(annotation), get_args(annotation)
    if origin in {Union, python_types.UnionType}:
        return any(value_matches_type(value, item) for item in arguments)
    if origin is Literal:
        return any(type(value) is type(item) and value == item for item in arguments)
    if origin in {list, set, frozenset, tuple}:
        if not isinstance(value, origin):
            return False
        if origin is tuple and arguments and arguments[-1] is not Ellipsis:
            return len(value) == len(arguments) and all(value_matches_type(v, a) for v, a in zip(value, arguments))
        return not arguments or all(value_matches_type(v, arguments[0]) for v in value)
    if origin is dict:
        return isinstance(value, dict) and all(value_matches_type(k, arguments[0]) and value_matches_type(v, arguments[1]) for k, v in value.items())
    if isinstance(annotation, type):
        return type(value) is annotation if annotation in {str, int, float, bool, bytes} else isinstance(value, annotation)
    return False


@lru_cache(maxsize=100)
def type_hints(target):
    return get_type_hints(target)


def validate_arguments(operation: str, args: tuple, kwargs: dict):
    import inspect
    from .provider import HermesProvider
    method = getattr(HermesProvider, operation)
    bound = inspect.signature(method).bind(None, *args, **kwargs)
    hints = type_hints(method)
    if any(name != "self" and not value_matches_type(value, hints.get(name, Any)) for name, value in bound.arguments.items()):
        raise ProtocolError("Invalid typed connector arguments")

def encode_message(value: dict[str, Any]) -> bytes:
    blobs = bytearray()

    def encode(item: Any) -> Any:
        if isinstance(item, bytes):
            start = len(blobs)
            if start + len(item) > MAX_BLOB_BYTES:
                raise ProtocolError("Binary payload exceeds connector limit")
            blobs.extend(item)
            return {"$bytes": [start, len(item)]}
        if isinstance(item, datetime):
            return {"$date": item.isoformat()}
        if isinstance(item, (tuple, set, frozenset)):
            return {"$sequence": type(item).__name__, "items": [encode(v) for v in item]}
        if is_dataclass(item) and type(item).__name__ in TYPES:
            return {"$type": type(item).__name__, "fields": {f.name: encode(getattr(item, f.name)) for f in fields(item)}}
        if isinstance(item, dict):
            # Wrap dictionaries to prevent an upstream object masquerading as a wire tag.
            return {"$dict": {str(k): encode(v) for k, v in item.items()}}
        if isinstance(item, list):
            return [encode(v) for v in item]
        if item is None or isinstance(item, (str, int, float, bool)):
            return item
        raise ProtocolError("Unsupported connector value")

    encoded = encode(value)
    validate_json_shape(encoded, max_depth=80, max_nodes=100_000)
    metadata = json.dumps(encoded, separators=(",", ":"), allow_nan=False).encode()
    if len(metadata) > MAX_JSON_BYTES:
        raise ProtocolError("Connector metadata is too large")
    return struct.pack("!I", len(metadata)) + metadata + blobs


def decode_message(payload: bytes) -> dict[str, Any]:
    if len(payload) < 4 or len(payload) > MAX_MESSAGE_BYTES:
        raise ProtocolError("Invalid message size")
    length = struct.unpack("!I", payload[:4])[0]
    if length > MAX_JSON_BYTES or length > len(payload) - 4:
        raise ProtocolError("Invalid metadata size")
    try:
        encoded = json.loads(payload[4:4 + length])
        validate_json_shape(encoded, max_depth=80, max_nodes=100_000)
    except (ValueError, RecursionError) as exc:
        raise ProtocolError("Invalid connector JSON") from exc
    blob = memoryview(payload)[4 + length:]
    if len(blob) > MAX_BLOB_BYTES:
        raise ProtocolError("Connector binary payload too large")

    decoded_binary_bytes = 0

    def decode(item: Any) -> Any:
        nonlocal decoded_binary_bytes
        if isinstance(item, list):
            return [decode(v) for v in item]
        if not isinstance(item, dict):
            return item
        if set(item) == {"$bytes"}:
            start, size = item["$bytes"]
            if type(start) is not int or type(size) is not int or min(start, size) < 0 or start + size > len(blob):
                raise ProtocolError("Invalid binary reference")
            decoded_binary_bytes += size
            if decoded_binary_bytes > MAX_BLOB_BYTES:
                raise ProtocolError("Decoded binary payload exceeds connector limit")
            return bytes(blob[start:start + size])
        if set(item) == {"$date"}:
            return datetime.fromisoformat(item["$date"])
        if set(item) == {"$dict"} and isinstance(item["$dict"], dict):
            return {k: decode(v) for k, v in item["$dict"].items()}
        if set(item) == {"$sequence", "items"} and isinstance(item["items"], list):
            constructor = {"tuple": tuple, "set": set, "frozenset": frozenset}.get(item["$sequence"])
            if constructor:
                return constructor(decode(v) for v in item["items"])
        if set(item) == {"$type", "fields"}:
            cls = TYPES.get(item["$type"])
            if cls is not None and isinstance(item["fields"], dict):
                result = cls(**{k: decode(v) for k, v in item["fields"].items()})
                if any(not value_matches_type(getattr(result, key), annotation) for key, annotation in type_hints(cls).items()):
                    raise ProtocolError("Invalid typed connector value")
                return result
        raise ProtocolError("Unknown connector wire type")

    try:
        result = decode(encoded)
    except ProtocolError:
        raise
    except (TypeError, KeyError, ValueError) as exc:
        raise ProtocolError("Invalid connector value") from exc
    if not isinstance(result, dict) or result.get("v") != VERSION:
        raise ProtocolError("Unsupported connector protocol")
    return result


def frames(message: dict[str, Any]):
    payload = encode_message(message)
    identifier = uuid4().bytes
    chunk_size = MAX_FRAME_BYTES - _HEADER.size
    for offset in range(0, len(payload), chunk_size):
        yield _HEADER.pack(_MAGIC, identifier, len(payload), offset) + payload[offset:offset + chunk_size]


class FrameReader:
    """At most two bounded messages, sequential offsets, and a 30s assembly TTL."""
    def __init__(self):
        self.pending: dict[bytes, tuple[float, int, bytearray]] = {}

    def feed(self, frame: bytes) -> dict[str, Any] | None:
        if not isinstance(frame, bytes) or not _HEADER.size < len(frame) <= MAX_FRAME_BYTES:
            raise ProtocolError("Invalid connector frame")
        now = time.monotonic()
        self.pending = {k: v for k, v in self.pending.items() if now - v[0] <= 30}
        magic, identifier, size, offset = _HEADER.unpack(frame[:_HEADER.size])
        if magic != _MAGIC or not 4 <= size <= MAX_MESSAGE_BYTES:
            raise ProtocolError("Invalid connector frame header")
        if identifier not in self.pending:
            if offset or len(self.pending) >= 2:
                raise ProtocolError("Invalid connector frame sequence")
            self.pending[identifier] = (now, size, bytearray())
        _, expected_size, collected = self.pending[identifier]
        if size != expected_size or offset != len(collected) or offset + len(frame) - _HEADER.size > size:
            raise ProtocolError("Invalid connector frame offset")
        collected.extend(frame[_HEADER.size:])
        if len(collected) == size:
            del self.pending[identifier]
            return decode_message(bytes(collected))
        return None


async def send_message(send_bytes, lock, message: dict[str, Any]) -> None:
    async with lock:
        for frame in frames(message):
            await send_bytes(frame)
