"""Session policies shared by Control, connectors and the native adapter."""
from typing import Literal

ChatMode = Literal["memory_read_write", "memory_read_only", "temporary"]
CHAT_MODES: tuple[ChatMode, ...] = ("memory_read_write", "memory_read_only", "temporary")
DEFAULT_CHAT_MODE: ChatMode = "memory_read_write"


def validate_chat_mode(value: str) -> ChatMode:
    if value not in CHAT_MODES:
        raise ValueError("Unsupported chat mode")
    return value


def mode_capability(mode: ChatMode) -> str:
    return "session.mode." + validate_chat_mode(mode)
