from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest

from agent_control_connector import setup_mac_lifecycle as mac
from agent_control_connector import setup_service
from agent_control_connector import setup_extras
from agent_control_connector.storage import atomic_json, read_json
from deploy.managed.macos.build_app import APP_ID, SERVICE_ID, app_plist, service_plist
from deploy.managed.macos import notarize_dmg
from deploy.connector.macos_signing import NotarizationPending

OLD, NEW = "a" * 40, "b" * 40


@pytest.fixture
def installation(tmp_path, monkeypatch):
    directory = tmp_path / "managed"
    directory.mkdir(mode=0o700)
    home = directory / "hermes-home"
    home.mkdir(mode=0o700)
    (home / "history.txt").write_text("existing history")
    connector = tmp_path / "connector"
    connector.mkdir(mode=0o700)
    canonical = tmp_path / "Applications/Agent Control.app"
    root = canonical / "Contents/Resources/runtime"
    target = tmp_path / "staged/Agent Control.app/Contents/Resources/runtime"
    root.mkdir(parents=True); target.mkdir(parents=True)
    config = {"hermesSource": str(root / "hermes"), "sourceSha": "c" * 40, "access": "no-secret-in-test"}
    atomic_json(connector / "config.json", config)
    state = {"mode": "managed", "hermesHome": str(home), "hermesSource": str(root / "hermes"),
             "releaseRoot": str(root), "sourceSha": "c" * 40, "hermesVersion": "0.21.2"}
    engine = SimpleNamespace(directory=directory, connector_dir=connector, state=state, root=root, server="https://control.test")
    engine.save = lambda: atomic_json(directory / "setup.json", engine.state)
    engine.status = lambda: {"localReady": True, "ready": True, "paired": True}
    engine.save()
    actual = {"revision": OLD, "schema": 1}
    def verify(path, expected_release=None):
        revision = NEW if path == target else actual["revision"]
        if expected_release and revision != expected_release:
            raise ValueError("Wrong runtime revision")
        return {"release": revision, "hermesSourceSha": "c" * 40, "hermesVersion": "0.21.2", "dataSchemaVersion": actual["schema"]}
    monkeypatch.setattr(mac, "verify_runtime", verify)
    monkeypatch.setattr(mac, "app_path", lambda: canonical)
    def drain(_):
        (connector / "maintenance.request").write_text("owned-maintenance")
    monkeypatch.setattr(mac, "drain", drain)
    async def idle(_): pass
    monkeypatch.setattr(setup_service, "all_profiles_idle", idle)
    monkeypatch.setattr(setup_service, "ensure_service", lambda _: None)
    monkeypatch.setattr(setup_extras, "validate_extra_transition", lambda *_: {})
    return engine, target, actual


def prepare(installation):
    engine, target, _ = installation
    return mac.lifecycle(engine, "update", {"phase": "prepare", "targetRoot": str(target), "revision": NEW})["transactionId"]


def phase(engine, nonce, name):
    return mac.lifecycle(engine, "update", {"phase": name, "transactionId": nonce})


def test_durable_update_keeps_drain_until_fresh_readiness_and_preserves_history(installation):
    engine, _, actual = installation
    nonce = prepare(installation)
    assert (engine.connector_dir / "maintenance.request").read_text() == "owned-maintenance"
    assert read_json(engine.directory / mac.TRANSACTION)["phase"] == "prepared"
    phase(engine, nonce, "stopped")
    tx = read_json(engine.directory / mac.TRANSACTION)
    assert (Path(tx["backup"]) / "history.txt").read_text() == "existing history"
    actual["revision"] = NEW
    phase(engine, nonce, "activate")
    assert (engine.connector_dir / "maintenance.request").exists()
    assert phase(engine, nonce, "complete")["ready"]
    assert not (engine.connector_dir / "maintenance.request").exists()
    assert not (engine.directory / mac.TRANSACTION).exists()
    assert read_json(engine.directory / "previous-app.json")["revision"] == OLD
    assert (engine.directory / "hermes-home/history.txt").read_text() == "existing history"
    with pytest.raises(ValueError, match="No hay"):
        phase(engine, nonce, "complete")


def test_active_work_prevents_transaction_and_restores_own_drain(installation, monkeypatch):
    engine, _, _ = installation
    async def active(_): raise ValueError("Hay trabajo activo")
    monkeypatch.setattr(setup_service, "all_profiles_idle", active)
    with pytest.raises(ValueError, match="activo"):
        prepare(installation)
    assert not (engine.directory / mac.TRANSACTION).exists()
    assert not (engine.connector_dir / "maintenance.request").exists()


def test_uncertain_operation_blocks_even_when_hermes_is_idle(installation):
    engine, _, _ = installation
    with sqlite3.connect(engine.connector_dir / "operations.sqlite3") as db:
        db.execute("CREATE TABLE operations (state TEXT)")
        db.execute("INSERT INTO operations VALUES ('unknown')")
    with pytest.raises(ValueError, match="incierto"):
        prepare(installation)
    assert not (engine.directory / mac.TRANSACTION).exists()
    assert not (engine.connector_dir / "maintenance.request").exists()


def test_nonce_replay_and_competing_transactions_do_not_change_state(installation):
    engine, _, _ = installation
    nonce = prepare(installation)
    with pytest.raises(ValueError, match="pertenece"):
        phase(engine, "different", "stopped")
    with pytest.raises(ValueError, match="pendiente"):
        prepare(installation)
    assert read_json(engine.directory / mac.TRANSACTION)["transactionId"] == nonce
    assert not (engine.directory / "backups").exists()


def test_snapshot_refuses_running_supervisor(installation):
    engine, _, _ = installation
    nonce = prepare(installation)
    with (engine.directory / "supervisor.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="cerrando"):
            phase(engine, nonce, "stopped")
    assert not (engine.directory / "backups").exists()


def test_schema_mismatch_refuses_before_drain(installation, monkeypatch):
    engine, target, _ = installation
    original = mac.verify_runtime
    def changed(path, expected_release=None):
        value = original(path, expected_release)
        if path == target: value["dataSchemaVersion"] = 2
        return value
    monkeypatch.setattr(mac, "verify_runtime", changed)
    with pytest.raises(ValueError, match="formato"):
        prepare(installation)
    assert not (engine.connector_dir / "maintenance.request").exists()


def test_mac_extra_trust_root_is_preserved_by_refusing_before_drain(installation, monkeypatch):
    engine, target, _ = installation
    origin = engine.state["releaseRoot"]
    engine.state["extras"] = {"browser": {"releaseRoot": origin, "release": OLD}}
    checked = []
    def validate(actual_engine, actual_target):
        assert actual_engine is engine and actual_target == target
        checked.append(True)
        return dict(engine.state["extras"])
    monkeypatch.setattr(setup_extras, "validate_extra_transition", validate)
    with pytest.raises(ValueError, match="función opcional"):
        prepare(installation)
    assert checked == [True]
    assert engine.state["releaseRoot"] == origin
    assert not (engine.connector_dir / "maintenance.request").exists()
    assert not (engine.directory / mac.TRANSACTION).exists()


def test_failed_readiness_requires_explicit_abort_with_old_app_restored(installation, monkeypatch):
    engine, _, actual = installation
    nonce = prepare(installation)
    phase(engine, nonce, "stopped")
    actual["revision"] = NEW
    phase(engine, nonce, "activate")
    def fail(_): raise ValueError("not ready")
    monkeypatch.setattr(setup_service, "ensure_service", fail)
    with pytest.raises(ValueError, match="not ready"):
        phase(engine, nonce, "complete")
    assert (engine.connector_dir / "maintenance.request").exists()
    with pytest.raises(ValueError, match="revision"):
        phase(engine, nonce, "abort")
    actual["revision"] = OLD
    phase(engine, nonce, "abort")
    monkeypatch.setattr(setup_service, "ensure_service", lambda _: None)
    assert phase(engine, nonce, "resume")["status"] == "restored"
    assert not (engine.directory / mac.TRANSACTION).exists()


def test_expired_transaction_does_not_autoresume_or_activate(installation):
    engine, _, _ = installation
    nonce = prepare(installation)
    tx = read_json(engine.directory / mac.TRANSACTION)
    tx["createdAt"] = time.time() - mac.MAX_AGE - 1
    atomic_json(engine.directory / mac.TRANSACTION, tx)
    assert phase(engine, nonce, "inspect")["expired"]
    with pytest.raises(ValueError, match="caducó"):
        phase(engine, nonce, "stopped")
    assert (engine.connector_dir / "maintenance.request").exists()
    phase(engine, nonce, "abort")
    phase(engine, nonce, "resume")


def test_foreign_maintenance_marker_is_preserved(installation):
    engine, _, _ = installation
    marker = engine.connector_dir / "maintenance.request"
    marker.write_text("another operation")
    with pytest.raises(ValueError, match="otra operación"):
        prepare(installation)
    assert marker.read_text() == "another operation"


def test_committed_journal_recovery_never_rolls_back_or_requires_stopping(installation, monkeypatch):
    engine, _, actual = installation
    nonce = prepare(installation)
    phase(engine, nonce, "stopped")
    actual["revision"] = NEW
    phase(engine, nonce, "activate")
    # Simulate process death after persisting commit but before deleting the
    # journal. Recovery only reconfirms readiness and cleans its own markers.
    tx = read_json(engine.directory / mac.TRANSACTION)
    tx.update(phase="complete", createdAt=time.time() - mac.MAX_AGE - 1)
    atomic_json(engine.directory / mac.TRANSACTION, tx)
    def must_not_stop(_): raise AssertionError("committed release must not be stopped")
    monkeypatch.setattr(mac, "_stopped", must_not_stop)
    assert phase(engine, nonce, "complete")["status"] == "complete"
    assert not (engine.directory / mac.TRANSACTION).exists()
    assert actual["revision"] == NEW


def test_recovery_refuses_new_work_that_started_after_failed_readiness(installation, monkeypatch):
    engine, _, _ = installation
    nonce = prepare(installation)
    async def active(_): raise ValueError("Hay trabajo activo")
    monkeypatch.setattr(setup_service, "all_profiles_idle", active)
    with pytest.raises(ValueError, match="activo"):
        phase(engine, nonce, "recovery-check")
    assert read_json(engine.directory / mac.TRANSACTION)["phase"] == "prepared"
    assert (engine.connector_dir / "maintenance.request").exists()


def test_app_metadata_and_owned_launch_agent_are_consistent():
    metadata = app_plist(OLD, "0.1.0", "1")
    service = service_plist()
    assert metadata["CFBundleIdentifier"] == APP_ID
    assert metadata["LSMinimumSystemVersion"] == "13.0"
    assert service["Label"] == SERVICE_ID
    assert service["AssociatedBundleIdentifiers"] == [APP_ID]
    assert service["BundleProgram"] == "Contents/MacOS/AgentControlService"
    assert "UserName" not in service


@pytest.mark.parametrize("revision,version,number", [("../", "0.1.0", "1"), (OLD, "invalid", "1"), (OLD, "0.1.0", "abc")])
def test_invalid_release_metadata_is_rejected(revision, version, number):
    with pytest.raises(ValueError):
        app_plist(revision, version, number)


def test_notarization_pending_reuses_exact_submission_and_does_not_resubmit(tmp_path, monkeypatch):
    artifact = tmp_path / "app.zip"
    artifact.write_bytes(b"immutable signed bytes")
    submissions = []
    def invoke(*args):
        operation = args[2]
        if operation == "submit":
            submissions.append(args)
            return json.dumps({"id": "apple-id"})
        assert operation == "info"
        return json.dumps({"id": "apple-id", "status": "In Progress"})
    monkeypatch.setattr(notarize_dmg, "invoke", invoke)
    for _ in range(2):
        with pytest.raises(NotarizationPending):
            notarize_dmg.notarize(artifact, tmp_path, "app", "test-profile")
    assert len(submissions) == 1
    artifact.write_bytes(b"changed bytes")
    with pytest.raises(ValueError, match="input changed"):
        notarize_dmg.notarize(artifact, tmp_path, "app", "test-profile")
    assert len(submissions) == 1


def test_notarization_acceptance_requires_exact_apple_log_hash(tmp_path, monkeypatch):
    artifact = tmp_path / "app.zip"
    artifact.write_bytes(b"immutable signed bytes")
    def invoke(*args):
        operation = args[2]
        if operation == "submit": return json.dumps({"id": "apple-id"})
        if operation == "info": return json.dumps({"id": "apple-id", "status": "Accepted"})
        assert operation == "log"
        Path(args[4]).write_text(json.dumps({"jobId": "apple-id", "status": "Accepted", "sha256": "0" * 64, "issues": None}))
        return ""
    monkeypatch.setattr(notarize_dmg, "invoke", invoke)
    with pytest.raises(ValueError, match="exact bytes"):
        notarize_dmg.notarize(artifact, tmp_path, "app", "test-profile")
