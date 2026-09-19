"""Standalone instructions for Hermes' native delegation (Python 3.10+, stdlib only).

This plugin does not register an executor, replace tools, or modify transcripts.
An audited compatibility shim keeps native completion delivery in its session's
profile. The native runtime owns claims, permissions and acknowledgement.
"""
from __future__ import annotations

import hashlib
import functools
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types
from uuid import uuid4

PLUGIN_NAME = "agent-control-background"
PLUGIN_VERSION = "1.0.1"
AUDITED_SOURCE_SHA = "939e45c91d751fadd94dcd1b873ac3cb44846213"
DELIVERY_SHIM = "profile-delivery-939e-v1"
DELIVERY_SOURCE_HASHES = {
    "tui_gateway/session_notifications.py": "f71b9dac8722e74cbf2fda89fd49263a1ef89df468f13ef4dbc79888b7f25cfa",
    "tools/async_delegation.py": "a837e0c5decadfb304d76cc8fe23c2c8bb56275ec38645125eaf1322f75db0a0",
    "hermes_constants.py": "d26e0db65ed08561a061db3bf6a51e4446938dc8d90737a9ba9c52e1983d15f2",
}
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


def _verified_delivery_dispatch(server):
    """Only wrap the reviewed native body, never another plugin's replacement."""
    dispatch = getattr(server, "_notif_dispatch_event", None)
    if getattr(dispatch, "_agent_control_delivery_shim", None) == DELIVERY_SHIM:
        return dispatch
    if not isinstance(dispatch, types.FunctionType) or dispatch.__globals__ is not vars(server):
        raise ValueError("Unsupported native background delivery handler")
    root = Path(server.__file__).resolve().parent.parent
    for relative, expected in DELIVERY_SOURCE_HASHES.items():
        source = root / relative
        if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != expected:
            raise ValueError("Unsupported native background delivery source")
    native_path = root / "tui_gateway/session_notifications.py"
    if (Path(dispatch.__code__.co_filename).resolve() != native_path
            or dispatch.__code__.co_name != "_notif_dispatch_event"
            or dispatch.__code__.co_argcount != 4
            or dispatch.__closure__):
        raise ValueError("Unsupported native background delivery handler")
    return dispatch


def install_delivery_profile_scope(source_sha: str) -> bool:
    """Bind claim/admission/ack to the profile already selected by native routing.

    The 939e poller starts on a bare thread. Its native claim and acknowledgement
    otherwise open the launch profile's database, even though the separate agent
    turn correctly persists the result in the session profile. No event is
    replayed, acknowledged early, or changed by this shim.
    """
    if source_sha != AUDITED_SOURCE_SHA:
        raise ValueError("Unsupported native background delivery revision")
    server = sys.modules.get("tui_gateway.server")
    if server is None:
        return False  # CLI needs no TUI shim; it cannot prove Serve activation.
    original = _verified_delivery_dispatch(server)
    if getattr(original, "_agent_control_delivery_shim", None) == DELIVERY_SHIM:
        return True
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override

    @functools.wraps(original)
    def scoped(sid, session, evt, text):
        home = session.get("profile_home") or getattr(server, "_hermes_home", None)
        if not isinstance(home, (str, Path)) or not str(home):
            raise ValueError("Native background delivery profile unavailable")
        token = set_hermes_home_override(home)
        try:
            return original(sid, session, evt, text)
        finally:
            reset_hermes_home_override(token)

    scoped._agent_control_delivery_shim = DELIVERY_SHIM
    # Concurrent profile builds may register this same plugin. Wrapping the
    # original once is sufficient; never stack wrappers or replace an outsider.
    current = getattr(server, "_notif_dispatch_event", None)
    if current is not original:
        if getattr(current, "_agent_control_delivery_shim", None) == DELIVERY_SHIM:
            return True
        raise ValueError("Native background delivery handler changed")
    server._notif_dispatch_event = scoped
    return True


def delivery_runtime_mode(delivery_scoped: bool) -> str | None:
    if delivery_scoped:
        return "tui-scoped"
    # A separate profile gateway uses native gateway delivery, not the TUI
    # poller. Do not confuse it with Serve importing late/partially initialized.
    if "tui_gateway.server" in sys.modules or "tui_gateway.entry" in sys.modules:
        return None
    if "gateway.run" in sys.modules and os.environ.get("_HERMES_GATEWAY") == "1":
        return "native-gateway"
    return None  # An unrelated CLI invocation cannot certify Control activation.


def register(ctx):
    from hermes_constants import get_hermes_home
    from hermes_cli.config import load_config
    home = get_hermes_home().resolve()
    receipt_path = home / "plugins" / PLUGIN_NAME / "installation.json"
    receipt = json.loads(receipt_path.read_text())
    delivery_scoped = install_delivery_profile_scope(receipt.get("sourceSha", ""))

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
                "profileDeliveryMode": delivery_runtime_mode(delivery_scoped),
                "profileDeliveryShim": DELIVERY_SHIM if delivery_scoped else None,
                "delegationAvailable": runtime_available(load_config())}, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)
