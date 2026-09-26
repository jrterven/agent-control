from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path
import shutil
import subprocess
import time

import pytest

from agent_control_connector import updates, managed_manifest, setup_service, manage
from agent_control_connector.storage import atomic_json, read_json

OLD, NEW = "a" * 40, "b" * 40


@pytest.fixture
def computer(tmp_path, monkeypatch):
    atomic_json(tmp_path / "config.json", {"server": "https://control.test", "gatewayId": "computer-one"})
    atomic_json(tmp_path / "update-control.json", {"automatic": True, "requestId": None, "pausedUntil": 0})
    atomic_json(tmp_path / "status.json", {"fresh": True, "connected": True, "activeWork": False,
                "observedAt": datetime.now(timezone.utc).isoformat(), "temporaryChats": 0, "release": OLD})
    offer = {"schemaVersion": 1, "version": NEW, "updates": {"protocol": 1, "sequence": 100, "rolloutPercent": 100, "paused": False}}
    monkeypatch.setattr(updates, "publication", lambda *args: offer)
    calls = []
    def apply(directory, installed, revision, server, control, progress):
        calls.append(revision)
        progress()
        value = read_json(directory / "status.json")
        atomic_json(directory / "status.json", {**value, "release": revision})
    monkeypatch.setattr(updates, "apply_update", apply)
    return tmp_path, {"kind": "connector", "release": OLD}, offer, calls


def run(computer):
    directory, installed, _, _ = computer
    updates.run_once(directory, installed)
    return read_json(directory / "update-status.json")


@pytest.mark.parametrize("changes,reason", [({"activeWork": True}, "busy"), ({"activeWork": None}, "busy"),
    ({"temporaryChats": 1}, "temporary"), ({"fresh": False}, "offline"), ({"connected": False}, "offline")])
def test_idle_and_private_chats_gate_even_explicit_update(computer, changes, reason):
    home, _, _, calls = computer
    atomic_json(home / "update-control.json", {"automatic": False, "requestId": "c" * 32, "pausedUntil": 0})
    atomic_json(home / "status.json", {**read_json(home / "status.json"), **changes})
    assert run(computer)["reason"] == reason
    assert calls == []


def test_new_release_is_observed_and_completed_request_is_not_replayed(computer):
    home, installed, offer, calls = computer
    atomic_json(home / "update-control.json", {"automatic": False, "requestId": "c" * 32, "pausedUntil": 0})
    assert run(computer)["state"] == "current"
    installed["release"] = NEW
    state = read_json(home / "update-status.json")
    atomic_json(home / "update-status.json", {**state, "nextCheckAt": 0})
    offer["version"] = "d" * 40
    assert run(computer)["state"] == "available"
    assert calls == [NEW]


@pytest.mark.parametrize("control", [{"automatic": False, "pausedUntil": 0, "requestId": None},
    {"automatic": True, "pausedUntil": 99_999_999_999, "requestId": None}])
def test_owner_can_disable_or_postpone_automatic_updates(computer, control):
    home, _, _, calls = computer
    atomic_json(home / "update-control.json", control)
    assert run(computer)["state"] in {"available", "paused"}
    assert not calls


def test_rollout_is_stable_manual_request_can_opt_in_but_global_pause_wins(computer):
    home, _, offer, calls = computer
    offer["updates"]["rolloutPercent"] = 0
    assert run(computer)["reason"] == "rollout"
    atomic_json(home / "update-control.json", {"automatic": True, "pausedUntil": 0, "requestId": "c" * 32})
    offer["updates"]["paused"] = True
    assert run(computer)["state"] == "paused"
    assert not calls
    offer["updates"]["paused"] = False
    atomic_json(home / "update-control.json", {"automatic": True, "pausedUntil": 0, "requestId": "d" * 32})
    assert run(computer)["state"] == "current"
    assert calls == [NEW]


def test_changed_policy_cancels_staged_update(computer):
    home, _, _, _ = computer
    previous = read_json(home / "update-control.json")
    atomic_json(home / "update-control.json", {**previous, "automatic": False})
    with pytest.raises(ValueError, match="preference changed"):
        updates.check_intent(home, previous)


def test_failure_is_sanitized_and_quarantined_until_new_request_or_release(computer, monkeypatch):
    home, _, offer, calls = computer
    def fail(*args):
        calls.append(NEW)
        raise ValueError("secret-token /private/customer/data")
    monkeypatch.setattr(updates, "apply_update", fail)
    state = run(computer)
    assert state["state"] == "failed"
    assert "secret" not in json.dumps(state)
    atomic_json(home / "update-status.json", {**state, "nextCheckAt": 0})
    run(computer)
    assert calls == [NEW]
    atomic_json(home / "update-control.json", {"automatic": True, "pausedUntil": 0, "requestId": "c" * 32})
    run(computer)
    assert calls == [NEW, NEW]


def test_stale_signed_publication_never_downgrades(computer):
    home, _, _, calls = computer
    atomic_json(home / "update-status.json", {"highestSequence": 101})
    assert run(computer)["state"] == "failed"
    assert not calls


def test_temporary_publication_failure_does_not_quarantine_the_offered_release(computer, monkeypatch):
    home, _, _, calls = computer
    atomic_json(home / "update-status.json", {"availableRelease": NEW})
    def unavailable(*args):
        raise ValueError("Signature and metadata were read across a publication swap")
    monkeypatch.setattr(updates, "publication", unavailable)
    state = run(computer)
    assert state["state"] == "failed" and state["failedRelease"] is None
    assert state["nextCheckAt"] <= int(time.time()) + 901
    assert not calls


def test_interrupted_install_requires_recovery_without_rerunning(computer):
    home, _, _, calls = computer
    atomic_json(home / "update-status.json", {"state": "installing", "availableRelease": NEW})
    assert run(computer)["reason"] == "recovery"
    assert not calls


def test_release_is_not_complete_without_new_connected_heartbeat(computer, monkeypatch):
    monkeypatch.setattr(updates, "apply_update", lambda *args: None)
    assert run(computer)["state"] == "failed"


@pytest.mark.parametrize("message", [{"automatic": "true", "pausedUntil": 0, "requestId": None},
    {"automatic": True, "pausedUntil": -1, "requestId": None},
    {"automatic": True, "pausedUntil": 0, "requestId": "$(touch /tmp/unsafe)"}])
def test_invalid_remote_intents_are_rejected(tmp_path, message):
    with pytest.raises(ValueError):
        updates.accept_control(tmp_path, message)
    assert not (tmp_path / "update-control.json").exists()


def test_signature_is_checked_before_using_release_or_policy(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = tmp_path / "key.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    monkeypatch.setattr(managed_manifest, "PUBLIC_KEY", key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    document = tmp_path / "latest.json"
    document.write_text(json.dumps({"schemaVersion": 1, "version": NEW, "updates": {"protocol": 1, "sequence": 1, "paused": False, "rolloutPercent": 100}}))
    signature = tmp_path / "latest.json.sig"
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private), "-out", str(signature), str(document)], check=True)
    monkeypatch.setattr(setup_service, "fetch", lambda url, dest, maximum: shutil.copyfile(tmp_path / url.rsplit("/", 1)[1], dest))
    assert updates.publication(tmp_path, {"kind": "managed"}, "https://control.test")["version"] == NEW
    document.write_text(document.read_text().replace(NEW, OLD))
    with pytest.raises(ValueError, match="signature"):
        updates.publication(tmp_path, {"kind": "managed"}, "https://control.test")


def test_linux_updater_uses_separate_user_unit_without_shell(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(updates.sys, "platform", "linux")
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    updates.launch(tmp_path, {"kind": "connector", "command": ["/safe path/agent-control-connector", "update-worker"]})
    args, kwargs = calls[0]
    assert args[:5] == ["systemd-run", "--user", "--quiet", "--collect", "--unit=agent-control-update-" + updates.hashlib.sha256(str(tmp_path).encode()).hexdigest()[:16]]
    assert args[-4:] == ["/safe path/agent-control-connector", "update-worker", "--data-dir", str(tmp_path)]
    assert not kwargs.get("shell")
