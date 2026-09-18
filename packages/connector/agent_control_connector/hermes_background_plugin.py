"""Standalone instructions for Hermes' native delegation (Python 3.10+, stdlib only).

This plugin does not register an executor, replace tools, or modify transcripts.
The native runtime owns delegation, permissions, lifecycle and completion delivery.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

PLUGIN_NAME = "agent-control-background"
PLUGIN_VERSION = "1.0.0"
INTERACTIVE_PLATFORMS = frozenset({"tui", "desktop", "cli"})
INSTRUCTIONS = """Agent Control conversational multitasking: keep this conversation available while independent, time-consuming tasks run. When native delegate_task is available and the task can be completed independently with the information and permissions already given, delegate it with a clear goal, relevant context and output language. Give only the context the worker needs. Use the native tool; do not build a background executor or change schedules. After a confirmed background dispatch, briefly say what is running and END YOUR TURN promptly so the user can keep chatting. Do not wait, poll transcripts, or repeatedly call list to await completion. Hermes delivers the result to this same conversation between turns; report the result once when it arrives, distinguishing success, failure and uncertain outcomes. If dispatch falls back to synchronous execution, do not claim the chat has been freed. Keep immediate answers and tasks needing clarification in the main conversation. Workers inherit existing permissions: delegation does not authorize sending messages, deleting data or other actions beyond the user's request. Avoid concurrent changes to the same resource; pass relevant constraints to the worker. Never retry an uncertain external action automatically, or treat a worker's self-report as independently verified. A user asking about progress is not cancelling the work. Use native list/steer/stop only when needed to answer, redirect or cancel. For visual work, ask the worker to return local file paths or HTTPS image URLs with alt text and provenance, without publishing images from its child session. Publish those images yourself with publish_images in this parent conversation before inserting the returned Markdown. Do not reuse ac-media references published in a child conversation; media access is conversation-scoped. Running subagents do not survive stopping/resetting their session or exiting Hermes; do not promise restart durability. Cron and other finite runs retain their native behavior."""


def string_list(value):
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            parsed = [part.strip() for part in value.split(",")]
        value = parsed
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("Invalid background tool configuration")
    return value


def disabled(config: dict, previously_installed=False) -> bool:
    if not isinstance(config, dict):
        raise ValueError("Invalid background configuration")
    plugins = config.get("plugins") or {}
    if not isinstance(plugins, dict):
        raise ValueError("Invalid background plugins configuration")
    entries = plugins.get("entries") or {}
    entry = entries.get(PLUGIN_NAME, {}) if isinstance(entries, dict) else {}
    if isinstance(entry, dict) and entry.get("enabled") is False:
        return True
    denied = string_list(plugins.get("disabled"))
    for group in (config.get("agent") or {}, config.get("tools") or {}):
        if not isinstance(group, dict):
            raise ValueError("Invalid background tools configuration")
        denied += string_list(group.get("disabled_toolsets")) + string_list(group.get("disabled"))
    if {PLUGIN_NAME, "delegation", "delegate_task"}.intersection(denied):
        return True
    if previously_installed and PLUGIN_NAME not in string_list(plugins.get("enabled")):
        return True
    # Serve uses the CLI selection even for its tui/desktop chat surfaces.
    platforms = config.get("platform_toolsets") or {}
    known = config.get("known_builtin_toolsets") or {}
    if not isinstance(platforms, dict) or not isinstance(known, dict):
        raise ValueError("Invalid background platform configuration")
    selected = platforms.get("cli")
    if selected == []:
        return True  # explicitly no tools
    return ("delegation" in string_list(known.get("cli"))
        and isinstance(selected, list) and "delegation" not in selected
        and "hermes-cli" not in selected)


def process_identity(pid: int) -> str:
    """PID plus OS start time avoids accepting a marker after PID reuse."""
    value = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
        check=True, capture_output=True, text=True, timeout=3).stdout.strip()
    if not value or len(value) > 100:
        raise ValueError("Background runtime identity unavailable")
    return value


def runtime_available(config) -> bool:
    if disabled(config, previously_installed=True):
        return False
    from hermes_cli.tools_config import _get_platform_tools
    from toolsets import resolve_toolset
    explicit = os.environ.get("HERMES_TUI_TOOLSETS", "").strip()
    selected = string_list(explicit) if explicit else _get_platform_tools(config, "cli")
    return any("delegate_task" in resolve_toolset(name) for name in selected)


def register(ctx):
    from hermes_constants import get_hermes_home
    from hermes_cli.config import load_config
    home = get_hermes_home().resolve()

    def context(platform="", parent_session_id="", **_):
        if platform not in INTERACTIVE_PLATFORMS or parent_session_id:
            return None
        try:
            enabled = runtime_available(load_config())
        except Exception:
            enabled = False
        return {"context": INSTRUCTIONS} if enabled else None

    ctx.register_system_prompt_section(PLUGIN_NAME,
        lambda info: (context(**dict(info)) or {}).get("context", ""), max_chars=4000)
    # Existing conversations get guidance in their NEXT turn, without replacing
    # the saved system prompt, SOUL, or any previous user/assistant message.
    ctx.register_hook("pre_llm_call", context)
    # Probe native symbols without dispatching a prompt or loading personal data.
    from tools.async_delegation import dispatch_async_delegation_batch
    from tools.delegate_tool import delegate_task
    if not callable(dispatch_async_delegation_batch) or not callable(delegate_task):
        raise ValueError("Native delegation unavailable")
    target = home / ".agent-control/background"
    for directory in (target.parent, target):
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid():
            raise ValueError("Unsafe background runtime directory")
        directory.chmod(0o700)
    receipt_path = home / "plugins" / PLUGIN_NAME / "installation.json"
    receipt = json.loads(receipt_path.read_text())
    marker = target / "runtime.json"
    if marker.is_symlink():
        raise ValueError("Unsafe background runtime marker")
    temporary = marker.with_name("runtime-" + uuid4().hex + ".tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump({"pid": os.getpid(), "processIdentity": process_identity(os.getpid()),
                "version": PLUGIN_VERSION, "sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "installationId": receipt["installationId"], "loadedAt": time.time(),
                "delegationAvailable": runtime_available(load_config())}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
