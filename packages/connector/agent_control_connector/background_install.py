"""Install shared conversational guidance without changing Hermes or cron jobs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from uuid import uuid4

import yaml
from hermes_client.compatibility import HERMES_0212_SHA

from .hermes_background_plugin import PLUGIN_NAME, PLUGIN_VERSION, disabled, process_identity, string_list
from .background_tasks import retired_profile
from .media_install import _read, _write
from .visual_media import profile_home


def plugin_source() -> bytes:
    return Path(__file__).with_name("hermes_background_plugin.py").read_bytes()


def background_self_test() -> None:
    """Check the shipped source and stdlib contract without opening a profile."""
    source = plugin_source()
    namespace = {"__name__": "background_packaging_smoke"}
    exec(compile(source, "hermes_background_plugin.py", "exec"), namespace)
    if namespace.get("PLUGIN_NAME") != PLUGIN_NAME or not callable(namespace.get("register")):
        raise ValueError("Background plugin source unavailable")
    if not namespace["disabled"]({"agent": {"disabled_toolsets": ["delegation"]}}):
        raise ValueError("Background opt-out validation failed")


def install_profile(home: Path) -> dict:
    path = home / "config.yaml"
    original = _read(path) if path.exists() else b"{}"
    config = yaml.safe_load(original) or {}
    target = home / "plugins" / PLUGIN_NAME
    manifest = target / "installation.json"
    if disabled(config, manifest.exists()):
        return {"state": "disabled"}
    for directory in (home / "plugins", target):
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid():
            raise ValueError("Unsafe background plugin directory")
    source = plugin_source()
    digest = hashlib.sha256(source).hexdigest()
    entry = target / "__init__.py"
    if entry.exists() and not manifest.exists() and _read(entry) != source:
        raise ValueError("A different background plugin already exists")
    if config.get("plugins") is None:
        config["plugins"] = {}
    plugins = config["plugins"]
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        raise ValueError("Invalid plugin allowlist")
    if PLUGIN_NAME not in enabled:
        enabled.append(PLUGIN_NAME)
    # CLI is the audited selection used by Serve chat. Leave absent/default
    # selections untouched; do not change global toolsets or finite cron runs.
    selected = (config.get("platform_toolsets") or {}).get("cli")
    if selected is not None:
        string_list(selected)  # validate before any write
        if not isinstance(selected, list):
            raise ValueError("Invalid CLI toolset selection")
        if "delegation" not in selected:
            selected.append("delegation")
        if config.get("known_builtin_toolsets") is None:
            config["known_builtin_toolsets"] = {}
        known = config["known_builtin_toolsets"].setdefault("cli", [])
        if not isinstance(known, list):
            raise ValueError("Invalid known CLI toolsets")
        if "delegation" not in known:
            known.append("delegation")
    changed = (yaml.safe_load(original) or {}) != config
    receipt = json.loads(_read(manifest)) if manifest.exists() else {}
    if receipt.get("sha256") != digest or changed:
        receipt = {"version": PLUGIN_VERSION, "sha256": digest, "installationId": uuid4().hex}
    if not entry.exists() or _read(entry) != source:
        _write(entry, source)
    if not manifest.exists() or json.loads(_read(manifest)) != receipt:
        _write(manifest, json.dumps(receipt).encode())
    descriptor = (f'name: {PLUGIN_NAME}\nversion: "{PLUGIN_VERSION}"\n'
        'description: "Keep Agent Control conversations available with native delegation"\n'
        'hooks:\n  - pre_llm_call\n').encode()
    if not (target / "plugin.yaml").exists() or _read(target / "plugin.yaml") != descriptor:
        _write(target / "plugin.yaml", descriptor)
    if changed:
        backup = target / ("config-before-" + hashlib.sha256(original).hexdigest()[:16] + ".yaml")
        if not backup.exists():
            _write(backup, original)
        if (path.exists() and _read(path) != original) or (not path.exists() and original != b"{}"):
            raise ValueError("Hermes configuration changed during background installation")
        _write(path, yaml.safe_dump(config, sort_keys=False, allow_unicode=True).encode())
    return probe_profile(home)


def probe_profile(home: Path) -> dict:
    installation = home / "plugins" / PLUGIN_NAME / "installation.json"
    path = home / "config.yaml"
    config = yaml.safe_load(_read(path)) if path.exists() else {}
    if disabled(config or {}, installation.exists()):
        return {"state": "disabled"}
    expected = hashlib.sha256(plugin_source()).hexdigest()
    try:
        receipt = json.loads(_read(installation))
        if receipt.get("sha256") != expected or hashlib.sha256(_read(installation.with_name("__init__.py"))).hexdigest() != expected:
            return {"state": "updateRequired"}
    except (OSError, ValueError):
        return {"state": "notInstalled"}
    try:
        runtime = json.loads(_read(home / ".agent-control/background/runtime.json", 4096))
        pid = runtime.get("pid")
        if (runtime.get("sha256") == expected and runtime.get("installationId") == receipt.get("installationId")
                and type(pid) is int and pid > 1):
            os.kill(pid, 0)
            if runtime.get("processIdentity") == process_identity(pid):
                return {"state": "ready" if runtime.get("delegationAvailable") is True else "disabled",
                    "version": PLUGIN_VERSION}
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return {"state": "pendingActivation", "version": PLUGIN_VERSION}


def background_profiles(config: dict, *, install=False) -> dict:
    if config.get("sourceSha") != HERMES_0212_SHA:
        return {profile: {"state": "unsupportedRuntime"} for profile in config["profiles"]}
    result = {}
    for profile in config["profiles"]:
        try:
            if retired_profile(Path(config["hermesHome"]), profile) is not None:
                result[profile] = {"state": "retired"}
                continue
            home = profile_home(Path(config["hermesHome"]), profile)
            result[profile] = install_profile(home) if install else probe_profile(home)
        except (OSError, ValueError, yaml.YAMLError):
            result[profile] = {"state": "installationFailed" if install else "unavailable"}
    return result
