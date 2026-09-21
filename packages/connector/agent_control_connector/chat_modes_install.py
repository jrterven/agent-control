"""Install the audited, per-conversation policy adapter while Hermes is idle."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import yaml
from hermes_client.compatibility import HERMES_0212_SHA
from .hermes_chat_policy import PLUGIN_NAME, PLUGIN_VERSION
from .media_install import _read, _write
from .visual_media import profile_home


def plugin_source():
    return Path(__file__).with_name("hermes_chat_policy.py").read_bytes()


def install_profile(home: Path):
    path = home / "config.yaml"
    original = _read(path) if path.exists() else b"{}"
    config = yaml.safe_load(original) or {}
    if not isinstance(config, dict):
        raise ValueError("Invalid Hermes configuration")
    plugins = config.setdefault("plugins", {})
    if not isinstance(plugins, dict) or not isinstance(plugins.get("entries", {}), dict):
        raise ValueError("Invalid plugin configuration")
    if PLUGIN_NAME in (plugins.get("disabled") or []) or (plugins.get("entries", {}).get(PLUGIN_NAME) or {}).get("enabled") is False:
        return {"state": "disabled"}
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        raise ValueError("Invalid plugin allowlist")
    target = home / "plugins" / PLUGIN_NAME
    if (target / "installation.json").exists() and PLUGIN_NAME not in enabled:
        return {"state": "disabled"}  # Removing a managed plugin is an opt-out too.
    for directory in (target.parent, target):
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid():
            raise ValueError("Unsafe chat policy plugin directory")
    source = plugin_source()
    entry, manifest = target / "__init__.py", target / "installation.json"
    if entry.exists() and not manifest.exists() and _read(entry) != source:
        raise ValueError("A different chat policy plugin already exists")
    if PLUGIN_NAME not in enabled:
        enabled.insert(0, PLUGIN_NAME)
    for file, value in (
        (entry, source),
        (manifest, json.dumps({"version": PLUGIN_VERSION, "sha256": hashlib.sha256(source).hexdigest(), "sourceSha": HERMES_0212_SHA}).encode()),
        (target / "plugin.yaml", f'name: {PLUGIN_NAME}\nversion: "{PLUGIN_VERSION}"\ndescription: "Per-conversation memory and temporary history policies"\nhooks:\n  - pre_llm_call\n'.encode()),
    ):
        if not file.exists() or _read(file) != value:
            _write(file, value)
    if config != (yaml.safe_load(original) or {}):
        backup = target / ("config-before-" + hashlib.sha256(original).hexdigest()[:16] + ".yaml")
        if not backup.exists():
            _write(backup, original)
        if path.exists() and _read(path) != original:
            raise ValueError("Hermes configuration changed during installation")
        _write(path, yaml.safe_dump(config, sort_keys=False, allow_unicode=True).encode())
    return {"state": "pendingActivation", "version": PLUGIN_VERSION}


def chat_mode_profiles(config: dict, *, install=False):
    if config.get("sourceSha") != HERMES_0212_SHA:
        return {}
    result = {}
    for profile in config["profiles"]:
        try:
            if install:
                result[profile] = install_profile(profile_home(Path(config["hermesHome"]), profile))
        except (OSError, ValueError, yaml.YAMLError):
            result[profile] = {"state": "installationFailed"}
    return result
