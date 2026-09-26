"""Signed, idle-only updates run outside the service being replaced.

Only bounded owner preferences cross the connector transport. Release selection
and verification stay local; no remote commands, paths or URLs are accepted.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time

from .storage import atomic_json, read_json

RELEASE = re.compile(r"^[a-f0-9]{40}$")
REQUEST = re.compile(r"^[a-f0-9]{32}$")
STATES = {"current", "checking", "available", "waiting", "downloading", "installing", "failed", "manual", "paused"}


class UpdateDeferred(ValueError):
    pass


class RecoveryRequired(ValueError):
    pass


def read_optional(path):
    try:
        if path.is_symlink() or path.stat().st_size > 16384:
            return {}
        value = read_json(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def installation(directory: Path):
    """Derive identity from the running executable, never a cloud field."""
    if getattr(sys, "frozen", False):
        root = Path(sys.executable).resolve().parent
        revision = read_optional(root / "release.json").get("revision")
        if RELEASE.fullmatch(str(revision)) and (directory / "current").resolve() == root:
            return {"kind": "connector", "root": root, "release": revision, "command": [str(root / "agent-control-connector"), "update-worker"]}
    root = Path(__file__).resolve().parents[2]
    # Provenance is small and covered by the inventory verified at service boot.
    revision = read_optional(root / "build-provenance.json").get("release")
    managed = os.environ.get("AGENT_CONTROL_MANAGED_DIR")
    if managed and RELEASE.fullmatch(str(revision)):
        home = Path(managed)
        state = read_optional(home / "setup.json")
        if state.get("releaseRoot") and Path(state["releaseRoot"]).resolve() == root and state.get("mode") in {"managed", "existing"}:
            return {"kind": "managed", "root": root, "home": home, "release": revision,
                    "command": [str(root / "python/bin/python3"), "-s", "-B", "-m", "agent_control_connector.updates"]}
    return None


def diagnostic(directory, installed):
    value = read_optional(directory / "update-status.json")
    supported = installed is not None and sys.platform in {"linux", "darwin"}
    result = {"protocol": 1, "supported": supported, "release": installed["release"] if installed else None,
              "state": value.get("state", "checking") if supported else "manual"}
    if result["state"] not in STATES:
        result["state"] = "checking"
    for key in ("availableRelease", "reason", "checkedAt", "requestId"):
        if key in value:
            result[key] = value[key]
    if not supported:
        result["reason"] = "unsupported"
    return result


def accept_control(directory, message):
    if (type(message.get("automatic")) is not bool or type(message.get("pausedUntil")) is not int
            or not 0 <= message["pausedUntil"] <= 100_000_000_000
            or message.get("requestId") is not None and not REQUEST.fullmatch(str(message["requestId"]))):
        raise ValueError("Invalid update preference")
    atomic_json(directory / "update-control.json", {key: message[key] for key in ("automatic", "pausedUntil", "requestId")})


@contextmanager
def worker_lock(directory):
    fd = os.open(directory / "update-worker.lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def launch(directory, installed):
    if installed is None:
        return
    state = read_optional(directory / "update-status.json")
    control = read_optional(directory / "update-control.json")
    if state.get("controlHash") == control_hash(control) and state.get("nextCheckAt", 0) > time.time():
        return
    # Avoid replacing a launchd job which is still doing an update.
    try:
        with worker_lock(directory):
            pass
    except BlockingIOError:
        return
    command = [*installed["command"], "--data-dir", str(directory)]
    env = {key: os.environ[key] for key in ("HOME", "USER", "PATH", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS", "TMPDIR") if key in os.environ}
    if installed["kind"] == "managed":
        env.update(PYTHONPATH=str(installed["root"] / "connector"), AGENT_CONTROL_MANAGED_DIR=str(installed["home"]))
    identifier = hashlib.sha256(str(directory).encode()).hexdigest()[:16]
    if sys.platform == "linux":
        # A separate user unit survives stopping the owned connector unit.
        args = ["systemd-run", "--user", "--quiet", "--collect", "--unit=agent-control-update-" + identifier,
                "--property=Type=exec", "--property=UMask=0077"]
        args.extend("--setenv=" + key + "=" + value for key, value in env.items())
        args.extend(["--", *command])
    elif sys.platform == "darwin":
        label = "com.jemailabs.agent-control.update." + identifier
        subprocess.run(["launchctl", "remove", label], capture_output=True, timeout=15)
        # launchctl inherits its own environment; /usr/bin/env supplies only the
        # locally derived runtime paths, never secrets or shell interpolation.
        args = ["launchctl", "submit", "-l", label, "--", "/usr/bin/env", *[key + "=" + value for key, value in env.items()], *command]
    else:
        return
    subprocess.run(args, env=env, capture_output=True, timeout=20, check=True)


def launch_safely(directory, installed):
    try:
        launch(directory, installed)
    except (OSError, ValueError, subprocess.SubprocessError):
        state = read_optional(directory / "update-status.json")
        atomic_json(directory / "update-status.json", {**state, "state": "failed", "reason": "service",
                    "controlHash": control_hash(read_optional(directory / "update-control.json")), "nextCheckAt": int(time.time()) + 900})


def publication(directory, installed, server):
    from .managed_manifest import verify_signature
    from .setup_service import fetch
    channel = "agent-control" if installed["kind"] == "managed" else "connector"
    with tempfile.TemporaryDirectory(dir=directory, prefix="update-check-") as temporary:
        folder = Path(temporary)
        for name, maximum in (("latest.json", 65536), ("latest.json.sig", 8192)):
            fetch(server + "/downloads/" + channel + "/" + name, folder / name, maximum)
        verify_signature(folder / "latest.json", folder / "latest.json.sig")
        value = json.loads((folder / "latest.json").read_bytes())
    policy = value.get("updates", {})
    if (value.get("schemaVersion") != 1 or not RELEASE.fullmatch(str(value.get("version")))
            or policy.get("protocol") != 1 or type(policy.get("sequence")) is not int or policy["sequence"] < 1
            or type(policy.get("rolloutPercent")) is not int or not 0 <= policy["rolloutPercent"] <= 100
            or type(policy.get("paused")) is not bool):
        raise ValueError("Unsupported signed update publication")
    return value


def safe_to_start(directory):
    from .manage import status
    try:
        snapshot = status(directory)
    except (OSError, ValueError, KeyError, TypeError):
        return "offline"
    if not snapshot.get("fresh") or snapshot.get("connected") is not True:
        return "offline"
    if snapshot.get("temporaryChats", 0):
        return "temporary"
    if snapshot.get("activeWork") is not False:
        return "busy"
    return None


def selected(control, state, now):
    manual = bool(control.get("requestId") and control["requestId"] != state.get("completedRequestId"))
    return manual or control.get("automatic") is True and control.get("pausedUntil", 0) <= now


def control_hash(control):
    return hashlib.sha256(json.dumps(control, sort_keys=True).encode()).hexdigest()


def check_intent(directory, control):
    if control is not None and read_optional(directory / "update-control.json") != control:
        raise UpdateDeferred("Update preference changed while staging")


def apply_update(directory, installed, revision, server, control, progress):
    if installed["kind"] == "connector":
        from .manage import management_lock, private_dir, verified_release, validate_release, validate_service_target, switch
        with management_lock(directory):
            validate_service_target()
            destination = directory / "releases" / revision
            private_dir(destination.parent)
            if not destination.exists():
                with tempfile.TemporaryDirectory(dir=directory, prefix=".download-") as temporary:
                    source = verified_release(directory, Path(temporary), revision)
                    shutil.move(str(source), destination)
            validate_release(destination, revision)
            validate_service_target()
            check_intent(directory, control)
            progress()
            # switch repeats the fresh drain gate after downloading.
            switch(directory, destination)
        return
    from .setup_engine import SetupEngine
    engine = SetupEngine(installed["root"], installed["home"], server, directory)
    if any((engine.directory / name).exists() for name in ("linux-update.json", "mac-update.json", "restart.pending.json")):
        raise RecoveryRequired("Recovery required")
    if sys.platform == "darwin":
        from .setup_mac_lifecycle import app_path
        helper = app_path() / "Contents/MacOS/AgentControlUpdater"
        result = subprocess.run([str(helper)], input=json.dumps({"method": "update", "expectedRevision": revision, "expectedControl": control, "background": True}),
                                text=True, capture_output=True, timeout=3600)
        if result.returncode:
            raise ValueError("Application update failed")
    else:
        from .setup_service import stage_update
        target = stage_update(engine, expected_release=revision)
        check_intent(directory, control)
        progress()
        engine.dispatch("update", {"releaseRoot": str(target), "expectedControl": control})


def run_once(directory, installed):
    from .cli import cloud_url
    state_path = directory / "update-status.json"
    state = read_optional(state_path)
    control = read_optional(directory / "update-control.json")
    if not control:
        return
    now = int(time.time())
    request = control.get("requestId")
    new_request = bool(request and request != state.get("requestId"))
    changed_control = control_hash(control) != state.get("controlHash")
    if not new_request and not changed_control and state.get("nextCheckAt", 0) > now:
        return
    config = read_json(directory / "config.json")
    server = cloud_url(config["server"])

    def save(status, reason=None, **values):
        state.update(state=status, reason=reason, **values)
        atomic_json(state_path, state)

    # An interrupted attempt is never silently retried after a restart.
    if state.get("state") == "installing":
        if installed["release"] == state.get("availableRelease") and safe_to_start(directory) != "offline":
            save("current", completedRequestId=request, controlHash=control_hash(control), nextCheckAt=now + 21600)
        else:
            save("failed", "recovery", failedRelease=state.get("availableRelease"), nextCheckAt=now + 21600)
        return
    attempted_update = False
    try:
        save("checking", requestId=request, controlHash=control_hash(control), nextCheckAt=now + 900)
        offer = publication(directory, installed, server)
        policy, revision = offer["updates"], offer["version"]
        if policy["sequence"] < state.get("highestSequence", 0):
            raise ValueError("Superseded publication")
        save("available", availableRelease=revision, checkedAt=now, highestSequence=policy["sequence"])
        if revision == installed["release"]:
            save("current", completedRequestId=request, nextCheckAt=now + 21600 + random.randint(0, 1800))
            return
        if state.get("failedRelease") == revision and not new_request:
            save("failed", "verification", nextCheckAt=now + 21600)
            return
        if policy["paused"] or not selected(control, state, now):
            save("paused" if policy["paused"] or control.get("pausedUntil", 0) > now else "available", nextCheckAt=now + 300)
            return
        bucket = int(hashlib.sha256((config["gatewayId"] + revision).encode()).hexdigest()[:8], 16) % 100
        manual = bool(request and request != state.get("completedRequestId"))
        if not manual and bucket >= policy["rolloutPercent"]:
            save("available", "rollout", nextCheckAt=now + 1800)
            return
        reason = safe_to_start(directory)
        if reason:
            save("waiting", reason, nextCheckAt=now + 60)
            return
        # Recheck owner intent just before dispatch; lifecycle repeats admission
        # checks after staging and refuses active, uncertain or temporary work.
        if read_optional(directory / "update-control.json") != control:
            save("available", nextCheckAt=now)
            return
        save("downloading", nextCheckAt=now + 3600)
        attempted_update = True
        apply_update(directory, installed, revision, server, control,
                     lambda: save("installing", nextCheckAt=int(time.time()) + 3600))
        observed = read_optional(directory / "status.json")
        if observed.get("release") != revision or observed.get("connected") is not True:
            raise ValueError("New release was not observed connected")
        save("current", failedRelease=None, completedRequestId=request, nextCheckAt=int(time.time()) + 21600)
    except UpdateDeferred:
        save("available", nextCheckAt=int(time.time()) + 60)
    except RecoveryRequired:
        save("failed", "recovery", failedRelease=state.get("availableRelease"), nextCheckAt=int(time.time()) + 21600)
    except Exception as error:
        # No exception text, process output, paths or credentials enter telemetry.
        reason = safe_to_start(directory)
        quarantine = attempted_update and isinstance(error, ValueError) and reason is None
        save("waiting" if reason else "failed", reason or "verification", failedRelease=state.get("availableRelease") if quarantine else None,
             nextCheckAt=int(time.time()) + (60 if reason else 21600 if quarantine else 900))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    installed = installation(args.data_dir)
    if installed is None:
        return 1
    try:
        with worker_lock(args.data_dir):
            run_once(args.data_dir, installed)
    except BlockingIOError:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
