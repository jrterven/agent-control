"""Versioned Hermes media plugin installation; never restarts an active runtime."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

import yaml
from hermes_client.compatibility import HERMES_0212_SHA

from .hermes_media_plugin import PLUGIN_NAME, PLUGIN_VERSION
from .visual_media import profile_home


def plugin_source() -> bytes:
    return Path(__file__).with_name("hermes_media_plugin.py").read_bytes()


def _read(path: Path, maximum=4 * 1024 * 1024) -> bytes:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise ValueError("Unsafe media configuration file")
    return path.read_bytes()


def _write(path: Path, content: bytes):
    if path.is_symlink():
        raise ValueError("Unsafe media configuration file")
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _disabled(config: dict, previously_installed: bool) -> bool:
    if not isinstance(config, dict):
        raise ValueError("Invalid Hermes configuration")
    plugins = config.get("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("Invalid Hermes plugins configuration")
    entries = plugins.get("entries", {})
    entry = entries.get(PLUGIN_NAME, {}) if isinstance(entries, dict) else {}
    if isinstance(entry, dict) and entry.get("enabled") is False:
        return True
    denied = plugins.get("disabled", [])
    if not isinstance(denied, list):
        raise ValueError("Invalid Hermes plugin denylist")
    for group in (config.get("agent", {}), config.get("tools", {})):
        if isinstance(group, dict):
            for key in ("disabled_toolsets", "disabled"):
                values = group.get(key, [])
                if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                    raise ValueError("Invalid Hermes toolset denylist")
                denied = [*denied, *values]
    if {PLUGIN_NAME, "publish_images"}.intersection(denied):
        return True
    return previously_installed and PLUGIN_NAME not in plugins.get("enabled", [])


def _append(container: dict, key: str):
    if key not in container or container[key] is None:
        return
    values = container[key]
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError("Invalid Hermes toolset list")
    if PLUGIN_NAME not in values:
        values.append(PLUGIN_NAME)


def _platform_disabled(config: dict, platform: str) -> bool:
    known = config.get("known_plugin_toolsets", {})
    platforms = config.get("platform_toolsets", {})
    return (isinstance(known, dict) and isinstance(platforms, dict)
        and PLUGIN_NAME in (known.get(platform) or [])
        and PLUGIN_NAME not in (platforms.get(platform) or []))


def install_profile(home: Path) -> dict:
    path = home / "config.yaml"
    original = _read(path) if path.exists() else b"{}"
    config = yaml.safe_load(original) or {}
    if not isinstance(config, dict):
        raise ValueError("Invalid Hermes configuration")
    target = home / "plugins" / PLUGIN_NAME
    manifest = target / "installation.json"
    previously_installed = manifest.exists()
    if _disabled(config, previously_installed):
        return {"state": "disabled"}
    for directory in (home / "plugins", target):
        directory.mkdir(mode=0o700, exist_ok=True)
        if directory.is_symlink() or directory.stat().st_uid != os.getuid():
            raise ValueError("Unsafe Hermes plugin directory")
    source = plugin_source()
    digest = hashlib.sha256(source).hexdigest()
    entry = target / "__init__.py"
    if entry.exists() and not previously_installed and _read(entry) != source:
        raise ValueError("A different agent-control-media plugin is already installed")
    plugins = config.setdefault("plugins", {})
    enabled = plugins.setdefault("enabled", [])
    if not isinstance(enabled, list):
        raise ValueError("Invalid Hermes plugin allowlist")
    if PLUGIN_NAME not in enabled:
        enabled.append(PLUGIN_NAME)
    for group in (config.get("tools", {}), config.get("agent", {})):
        if isinstance(group, dict):
            _append(group, "enabled_toolsets")
    platforms = config.get("platform_toolsets", {})
    if isinstance(platforms, dict):
        for platform in platforms:
            if _platform_disabled(config, platform):
                continue
            _append(platforms, platform)
            known = config.setdefault("known_plugin_toolsets", {})
            if not isinstance(known, dict):
                raise ValueError("Invalid known plugin toolsets")
            known.setdefault(platform, [])
            _append(known, platform)
    changed = (yaml.safe_load(original) or {}) != config
    if changed:
        backup = target / ("config-before-" + hashlib.sha256(original).hexdigest()[:16] + ".yaml")
        if not backup.exists():
            _write(backup, original)
        # Recheck optimistic version after staging; do not clobber concurrent UI edits.
        if (path.exists() and _read(path) != original) or (not path.exists() and original != b"{}"):
            raise ValueError("Hermes configuration changed during media installation")
        _write(path, yaml.safe_dump(config, sort_keys=False, allow_unicode=True).encode())
    if not entry.exists() or _read(entry) != source:
        _write(entry, source)
    plugin_manifest = (f'name: {PLUGIN_NAME}\nversion: "{PLUGIN_VERSION}"\ndescription: "Private images and galleries in Agent Control"\nhooks:\n  - pre_llm_call\n  - pre_tool_call\n').encode()
    if not (target / "plugin.yaml").exists() or _read(target / "plugin.yaml") != plugin_manifest:
        _write(target / "plugin.yaml", plugin_manifest)
    if not _platform_disabled(config, "cron"):
        _merge_cron(home)
    receipt = {"version": PLUGIN_VERSION, "sha256": digest}
    if not manifest.exists() or json.loads(_read(manifest)) != receipt:
        _write(manifest, json.dumps(receipt).encode())
    return probe_profile(home)


def _merge_cron(home: Path):
    path = home / "cron/jobs.json"
    if not path.exists():
        return
    if path.parent.is_symlink():
        raise ValueError("Unsafe cron directory")
    lock_fd = os.open(path.parent / ".jobs.lock", os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        original = _read(path)
        data = json.loads(original)
        rows = data.get("jobs") if isinstance(data, dict) else None
        if not isinstance(rows, list):
            raise ValueError("Invalid Hermes cron store")
        for row in rows:
            if not isinstance(row, dict) or not row.get("enabled_toolsets") or PLUGIN_NAME in row.get("disabled_toolsets", []):
                continue
            _append(row, "enabled_toolsets")
        if data != json.loads(original):
            backup = home / "plugins" / PLUGIN_NAME / ("cron-before-" + hashlib.sha256(original).hexdigest()[:16] + ".json")
            if not backup.exists():
                _write(backup, original)
            _write(path, json.dumps(data, ensure_ascii=False).encode())


def probe_profile(home: Path) -> dict:
    expected = hashlib.sha256(plugin_source()).hexdigest()
    installation = home / "plugins" / PLUGIN_NAME / "installation.json"
    config_path = home / "config.yaml"
    config = yaml.safe_load(_read(config_path)) if config_path.exists() else {}
    if _disabled(config or {}, installation.exists()):
        return {"state": "disabled"}
    try:
        receipt = json.loads(_read(installation))
        if receipt.get("sha256") != expected:
            return {"state": "updateRequired"}
        actual = hashlib.sha256(_read(installation.with_name("__init__.py"))).hexdigest()
        if actual != expected:
            return {"state": "updateRequired"}
    except (OSError, ValueError):
        return {"state": "notInstalled"}
    try:
        runtime = json.loads(_read(home / ".agent-control/media/runtime.json", 4096))
        pid = runtime.get("pid")
        if runtime.get("sha256") == expected and type(pid) is int and pid > 1:
            os.kill(pid, 0)
            return {"state": "ready", "version": PLUGIN_VERSION}
    except (OSError, ValueError):
        pass
    return {"state": "pendingActivation", "version": PLUGIN_VERSION}


def media_profiles(config: dict, *, install=False) -> dict:
    if config.get("sourceSha") != HERMES_0212_SHA:
        return {profile: {"state": "unsupportedRuntime"} for profile in config["profiles"]}
    result = {}
    for profile in config["profiles"]:
        try:
            home = profile_home(Path(config["hermesHome"]), profile)
            result[profile] = install_profile(home) if install else probe_profile(home)
        except (OSError, ValueError, yaml.YAMLError):
            result[profile] = {"state": "installationFailed" if install else "unavailable"}
    return result
