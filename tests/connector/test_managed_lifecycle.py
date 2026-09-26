"""Lifecycle transactions preserve user data and stop only owned processes."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from agent_control_connector import setup_engine as setup
from agent_control_connector import setup_service as service
from agent_control_connector import storage
from agent_control_connector.manage import atomic_link
from agent_control_connector.storage import OperationLedger, atomic_json, private_dir, read_json
from hermes_client.compatibility import HERMES_0206_SHA, HERMES_0212_SHA


@pytest.fixture
def installation(tmp_path, monkeypatch):
    """Real persisted identity/history/ledger, with only OS service and HTTP boundaries simulated."""
    old, new = tmp_path / "releases/old", tmp_path / "releases/new"
    for root in (old, new):
        (root / "hermes").mkdir(parents=True)
        (root / "python/bin").mkdir(parents=True)
    engine = setup.SetupEngine(old, tmp_path / "managed", "https://control.test", tmp_path / "connector")
    home = engine.directory / "hermes-home"
    private_dir(home)
    (home / "history.json").write_bytes(b'{"conversation":"preserve this history"}\n')
    atomic_json(home / "config.yaml", {"model": {"provider": "openai", "default": "user-model"}})
    engine.state = {"schemaVersion": 1, "mode": "managed", "server": engine.server,
        "hermesHome": str(home), "hermesSource": str(old / "hermes"), "sourceSha": HERMES_0206_SHA,
        "hermesVersion": "0.20.6", "restUrl": "http://127.0.0.1:19119", "releaseRoot": str(old),
        "providerReady": True, "profiles": ["default", "science"]}
    engine.save()
    atomic_json(engine.connector_dir / "config.json", {"connectorId": "owned-identity", "gatewayId": "owned-gateway",
        "profiles": ["science"], "sourceSha": HERMES_0206_SHA, "hermesSource": str(old / "hermes")})
    ledger = OperationLedger(engine.connector_dir)
    assert ledger.reserve("finished-prompt", "prompt-digest")[0] == "new"
    ledger.finish("finished-prompt", b'{"accepted":true}')
    ledger.close()
    atomic_link(engine.directory, "current", old)
    os_state = SimpleNamespace(ready=True, commands=[], sessions={"default": [], "science": []}, complete=True)

    def status(**_):
        return {"localReady": os_state.ready, "ready": os_state.ready, "connected": os_state.ready,
                "paired": (engine.connector_dir / "config.json").exists()}
    monkeypatch.setattr(engine, "status", status)
    monkeypatch.setattr(engine, "token", lambda: "private-fixture-token")
    async def profiles():
        return ["default", "science"]
    monkeypatch.setattr(engine, "profiles", profiles)
    class Provider:
        def __init__(self, connection):
            self.name = connection.profile_name
            self.session_inventory_complete = os_state.complete
        async def list_sessions(self):
            return [SimpleNamespace(status=value) for value in os_state.sessions[self.name]]
        async def close(self):
            pass
    monkeypatch.setattr(service, "HermesGatewayProvider", Provider)
    monkeypatch.setattr(service, "sys", SimpleNamespace(platform="linux"))
    original_which = service.shutil.which
    monkeypatch.setattr(service.shutil, "which", lambda command: "/fixture/systemctl" if command == "systemctl" else original_which(command))
    monkeypatch.setattr(service, "verify_runtime", lambda root, **_: {
        "release": "b" * 40 if Path(root) == new else "a" * 40, "dataSchemaVersion": 1,
        "hermesSourceSha": HERMES_0212_SHA if Path(root) == new else HERMES_0206_SHA,
        "hermesVersion": "0.21.2" if Path(root) == new else "0.20.6"})
    monkeypatch.setattr(service, "drain", lambda directory: atomic_json(directory / "maintenance.request", {"requested": True}))
    unit = tmp_path / "systemd/user" / service.UNIT
    monkeypatch.setattr(service, "service_file", lambda: unit)
    def systemctl(*args, **_):
        os_state.commands.append(args)
        if args[0] == "stop" or (args[0] == "disable" and "--now" in args):
            os_state.ready = False
        elif args[0] == "enable" and "--now" in args:
            os_state.ready = True
        return subprocess.CompletedProcess(args, 0, b"", b"")
    monkeypatch.setattr(service, "systemctl", systemctl)
    original_run = subprocess.run
    def run(args, **kwargs):
        if args[0] == "loginctl":
            return subprocess.CompletedProcess(args, 0, "yes\n", "")
        return original_run(args, **kwargs)
    monkeypatch.setattr(service.subprocess, "run", run)
    return SimpleNamespace(engine=engine, old=old, new=new, host=os_state, home=home, unit=unit)


def receipt_rows(engine):
    with sqlite3.connect(engine.connector_dir / "operations.sqlite3") as database:
        return database.execute("SELECT key,digest,state,result FROM operations ORDER BY key").fetchall()


def assert_preserved(installation, history, receipts):
    assert (installation.home / "history.json").read_bytes() == history
    assert receipt_rows(installation.engine) == receipts
    identity = read_json(installation.engine.connector_dir / "config.json")
    assert identity["connectorId"] == "owned-identity"
    assert identity["gatewayId"] == "owned-gateway"
    assert identity["profiles"] == ["science"]


def test_linux_update_switches_verified_source_and_preserves_identity_history_receipts(installation):
    item = installation
    history, receipts = (item.home / "history.json").read_bytes(), receipt_rows(item.engine)
    result = service.lifecycle(item.engine, "update", {"releaseRoot": str(item.new)})
    state = read_json(item.engine.directory / "setup.json")
    config = read_json(item.engine.connector_dir / "config.json")
    assert result["status"] == "complete"
    assert state["releaseRoot"] == str(item.new) and state["hermesVersion"] == "0.21.2"
    assert config["hermesSource"] == str(item.new / "hermes") and config["sourceSha"] == HERMES_0212_SHA
    assert (item.engine.directory / "current").resolve() == item.new
    assert (item.engine.directory / "previous").resolve() == item.old
    assert str(item.new / "python/bin/python3") in item.unit.read_text()
    assert "private-fixture-token" not in item.unit.read_text()
    assert not (item.engine.directory / "linux-update.json").exists()
    assert not (item.engine.connector_dir / "maintenance.request").exists()
    backup = list((item.engine.directory / "backups").glob("*/history.json"))
    assert len(backup) == 1 and backup[0].read_bytes() == history
    assert_preserved(item, history, receipts)


def test_existing_hermes_update_preserves_external_source_and_home(installation):
    item = installation
    item.engine.state["mode"] = "existing"
    item.engine.save()
    original = dict(item.engine.state)
    configuration = (item.engine.connector_dir / "config.json").read_bytes()
    history, receipts = (item.home / "history.json").read_bytes(), receipt_rows(item.engine)
    assert service.lifecycle(item.engine, "update", {"releaseRoot": str(item.new)})["status"] == "complete"
    for key in ("hermesHome", "hermesSource", "sourceSha", "hermesVersion"):
        assert item.engine.state[key] == original[key]
    assert (item.engine.connector_dir / "config.json").read_bytes() == configuration
    assert not list((item.engine.directory / "backups").glob("*/history.json"))
    assert_preserved(item, history, receipts)


def test_first_managed_boot_prepares_chat_modes_before_pairing(tmp_path):
    import yaml
    from agent_control_connector.hermes_chat_policy import PLUGIN_NAME
    directory, home = tmp_path / "managed", tmp_path / "managed/hermes-home"
    private_dir(home)
    tools = directory / "tool-environments/default/bin/python"
    tools.parent.mkdir(parents=True)
    tools.write_text("fixture interpreter already prepared")
    engine = SimpleNamespace(directory=directory, connector_dir=tmp_path / "not-paired",
        state={"hermesHome": str(home), "sourceSha": HERMES_0212_SHA})
    service.prepare_owned_tools(engine)
    assert PLUGIN_NAME in yaml.safe_load((home / "config.yaml").read_text())["plugins"]["enabled"]
    assert (home / "plugins" / PLUGIN_NAME / "installation.json").is_file()
    assert not engine.connector_dir.exists()


@pytest.mark.parametrize("state", ["running", "unknown"])
def test_uncertain_operation_ledger_prevents_every_service_stop(installation, state):
    item = installation
    with sqlite3.connect(item.engine.connector_dir / "operations.sqlite3") as database:
        database.execute("INSERT INTO operations VALUES (?,?,?,NULL)", ("uncertain-prompt", "digest", state))
    with pytest.raises(ValueError, match="incierto"):
        service.lifecycle(item.engine, "update", {"releaseRoot": str(item.new)})
    assert item.host.commands == []
    assert (item.engine.directory / "current").resolve() == item.old


@pytest.mark.parametrize("scenario", ["active-other-profile", "incomplete-inventory", "unresponsive"])
def test_all_profiles_must_be_provably_idle_before_restart(installation, scenario):
    item = installation
    if scenario == "active-other-profile":
        item.host.sessions["science"] = ["running"]
    elif scenario == "incomplete-inventory":
        item.host.complete = False
    else:
        item.host.ready = False
    with pytest.raises(ValueError):
        service.lifecycle(item.engine, "update", {"releaseRoot": str(item.new)})
    assert item.host.commands == []
    assert not (item.engine.directory / "linux-update.json").exists()
    assert (item.engine.directory / "current").resolve() == item.old


def test_failed_upgrade_restores_service_source_config_and_user_data(installation, monkeypatch):
    item = installation
    original = read_json(item.engine.directory / "setup.json")
    config = read_json(item.engine.connector_dir / "config.json")
    history, receipts = (item.home / "history.json").read_bytes(), receipt_rows(item.engine)
    def readiness(engine):
        if engine.state["releaseRoot"] == str(item.new):
            raise ValueError("new runtime did not become ready")
        assert item.host.ready
    monkeypatch.setattr(service, "ensure_service", readiness)
    with pytest.raises(ValueError, match="did not become ready"):
        service.lifecycle(item.engine, "update", {"releaseRoot": str(item.new)})
    assert read_json(item.engine.directory / "setup.json") == original
    assert read_json(item.engine.connector_dir / "config.json") == config
    assert (item.engine.directory / "current").resolve() == item.old
    assert str(item.old / "python/bin/python3") in item.unit.read_text()
    assert item.host.ready
    assert not (item.engine.directory / "linux-update.json").exists()
    assert not (item.engine.connector_dir / "maintenance.request").exists()
    assert_preserved(item, history, receipts)


def test_failed_automatic_restore_keeps_recovery_journal_and_blocks_another_update(installation, monkeypatch):
    item = installation
    history, receipts = (item.home / "history.json").read_bytes(), receipt_rows(item.engine)
    monkeypatch.setattr(service, "ensure_service", lambda engine: (_ for _ in ()).throw(ValueError("service unavailable")))
    with pytest.raises(ValueError, match="unavailable"):
        service.lifecycle(item.engine, "update", {"releaseRoot": str(item.new)})
    assert (item.engine.directory / "linux-update.json").exists()
    assert (item.engine.connector_dir / "maintenance.request").exists()
    item.host.commands.clear()
    with pytest.raises(ValueError, match="interrumpida"):
        service.lifecycle(item.engine, "update", {"releaseRoot": str(item.new)})
    assert item.host.commands == []
    assert_preserved(item, history, receipts)


def test_incompatible_data_schema_rejected_before_stopping_anything(installation, monkeypatch):
    item = installation
    verify = service.verify_runtime
    monkeypatch.setattr(service, "verify_runtime", lambda root, **kwargs: {
        **verify(root, **kwargs), "dataSchemaVersion": 2 if Path(root) == item.new else 1})
    with pytest.raises(ValueError, match="migración"):
        service.lifecycle(item.engine, "update", {"releaseRoot": str(item.new)})
    assert item.host.commands == []
    assert (item.engine.directory / "current").resolve() == item.old


def interrupted_transaction(item):
    original = read_json(item.engine.directory / "setup.json")
    config = read_json(item.engine.connector_dir / "config.json")
    atomic_json(item.engine.directory / "linux-update.json", {"oldState": original, "oldConfig": config,
        "targetRoot": str(item.new), "dataSchemaVersion": 1})
    atomic_json(item.engine.connector_dir / "maintenance.request", {"requested": True})
    item.engine.state.update(releaseRoot=str(item.new), hermesSource=str(item.new / "hermes"),
                             sourceSha=HERMES_0212_SHA, hermesVersion="0.21.2")
    item.engine.root = item.new
    item.engine.save()
    atomic_link(item.engine.directory, "current", item.new)
    return original, config


def test_interrupted_upgrade_requires_explicit_rollback_and_restores_previous_release(installation):
    item = installation
    original, config = interrupted_transaction(item)
    history, receipts = (item.home / "history.json").read_bytes(), receipt_rows(item.engine)
    with pytest.raises(ValueError, match="interrumpida"):
        service.lifecycle(item.engine, "update", {"releaseRoot": str(item.new)})
    assert item.host.commands == []
    assert service.lifecycle(item.engine, "rollback", {})["status"] == "restored"
    assert read_json(item.engine.directory / "setup.json") == original
    assert read_json(item.engine.connector_dir / "config.json") == config
    assert (item.engine.directory / "current").resolve() == item.old
    assert item.engine.root == item.old
    assert not (item.engine.directory / "linux-update.json").exists()
    assert not (item.engine.connector_dir / "maintenance.request").exists()
    assert_preserved(item, history, receipts)


def test_interrupted_rollback_does_not_stop_a_live_unresponsive_supervisor(installation):
    item = installation
    interrupted_transaction(item)
    item.host.ready = False
    with (item.engine.directory / "supervisor.lock").open("w") as live_supervisor:
        fcntl.flock(live_supervisor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError):
            service.lifecycle(item.engine, "rollback", {})
    assert item.host.commands == []
    assert (item.engine.directory / "linux-update.json").exists()
    assert (item.engine.connector_dir / "maintenance.request").exists()


def test_private_local_credentials_and_state_reject_foreign_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "sys", SimpleNamespace(platform="linux"))
    directory = tmp_path / "managed"
    private_dir(directory)
    credentials = storage.SecretStore(directory / "credentials")
    credentials.save({"hermesToken": "local-only-secret"})
    atomic_json(directory / "setup.json", {"mode": "managed"})
    assert directory.stat().st_mode & 0o777 == 0o700
    assert (directory / "credentials").stat().st_mode & 0o777 == 0o700
    assert (directory / "credentials/secrets.json").stat().st_mode & 0o777 == 0o600
    assert (directory / "setup.json").stat().st_mode & 0o777 == 0o600
    assert "local-only-secret" not in (directory / "setup.json").read_text()
    actual_uid = os.getuid()
    monkeypatch.setattr(storage.os, "getuid", lambda: actual_uid + 1)
    with pytest.raises(ValueError, match="owned"):
        storage.SecretStore(directory / "credentials")


def test_unrelated_systemd_unit_cannot_be_overwritten(installation):
    item = installation
    item.unit.parent.mkdir(parents=True)
    item.unit.write_text("[Unit]\nDescription=Unrelated user's process\n")
    before = item.unit.read_bytes()
    with pytest.raises(ValueError, match="Otro servicio"):
        service.install_linux_service(item.engine)
    assert item.unit.read_bytes() == before
    assert item.host.commands == []


@pytest.mark.parametrize("method", ["update", "uninstall"])
def test_failed_setup_with_colliding_unit_never_stops_that_unrelated_service(installation, method):
    item = installation
    item.unit.parent.mkdir(parents=True)
    item.unit.write_text("[Unit]\nDescription=Existing unrelated service\n")
    before = item.unit.read_bytes()
    with pytest.raises(ValueError, match="Otro servicio|servicio.*ajeno|servicio.*pertenece"):
        service.lifecycle(item.engine, method, {"releaseRoot": str(item.new)} if method == "update" else {})
    assert item.host.commands == []
    assert item.unit.read_bytes() == before
    assert (item.engine.directory / "current").resolve() == item.old


def test_uninstall_stops_only_managed_unit_and_keeps_identity_history_and_receipts(installation):
    item = installation
    history, receipts = (item.home / "history.json").read_bytes(), receipt_rows(item.engine)
    state = read_json(item.engine.directory / "setup.json")
    result = service.lifecycle(item.engine, "uninstall", {})
    assert result["status"] == "stopped" and result["dataPreserved"]
    assert item.host.commands == [("disable", "--now", service.UNIT), ("daemon-reload",)]
    assert read_json(item.engine.directory / "setup.json") == state
    assert_preserved(item, history, receipts)


@pytest.mark.parametrize("mode", ["existing", "managed"])
def test_supervisor_pins_managed_root_and_stops_only_owned_processes(tmp_path, mode):
    """Real children with a CLI fixture for explicit profile > sticky selection."""
    repo = Path(__file__).resolve().parents[2]
    directory, home, connector, runtime = [tmp_path / name for name in ("managed", "existing-hermes", "connector", "runtime")]
    for path in (directory, home, connector, runtime / "python/bin"):
        private_dir(path)
    (home / "active_profile").write_text("science\n")
    private_dir(home / "profiles/science")
    atomic_json(connector / "config.json", {"connectorId": "fixture"})
    fake_python = runtime / "python/bin/python3"
    fake_python.write_text(f"#!{sys.executable}\n" + """import argparse,json,os,sys,time
from pathlib import Path
root = Path(os.environ['HERMES_HOME'])
args = sys.argv[1:]
component = 'hermes' if 'serve' in args else 'connector'
record = {'pid': os.getpid(), 'args': args, 'home': str(root),
          'tokenMatches': os.environ['HERMES_DASHBOARD_SESSION_TOKEN'] == 'temporary-token'}
if component == 'hermes':
    # Hermes preparses an explicit selector before consulting active_profile.
    # A configured root HERMES_HOME does not by itself suppress that fallback.
    parser = argparse.ArgumentParser()
    parser.add_argument('-p', '--profile')
    parsed, remaining = parser.parse_known_args(args[args.index('-c') + 2:])
    profile = parsed.profile or (root / 'active_profile').read_text().strip()
    record.update(profile=profile, effectiveHome=str(root if profile == 'default' else root / 'profiles' / profile),
                  remaining=remaining, cwd=os.getcwd())
target = root / (component + '-start.json')
temporary = target.with_suffix('.tmp')
temporary.write_text(json.dumps(record))
temporary.chmod(0o600)
temporary.replace(target)
while True: time.sleep(1)
""")
    fake_python.chmod(0o755)
    scenario = tmp_path / "supervise.py"
    scenario.write_text("import sys\nfrom pathlib import Path\nfrom types import SimpleNamespace\n"
        + f"sys.path[:0] = {[str(repo / 'packages/connector'), str(repo / 'packages/hermes-client')]!r}\n"
        + "from agent_control_connector import setup_service as service\n"
        + "service.verify_runtime = lambda root: {}\n"
        + "service.prepare_owned_tools = lambda engine: None\n"
        + f"engine=SimpleNamespace(directory=Path({str(directory)!r}),root=Path({str(runtime)!r}),connector_dir=Path({str(connector)!r}),token=lambda:'temporary-token',state="
        + repr({"mode": mode, "releaseRoot": str(runtime), "hermesHome": str(home), "restUrl": "http://127.0.0.1:19119"}) + ")\n"
        + "service.supervise(engine)\n")
    external = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    supervisor = subprocess.Popen([sys.executable, str(scenario)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        env={**os.environ, "HERMES_HOME": str(tmp_path / "unrelated-hermes"),
             "HERMES_DASHBOARD_SESSION_TOKEN": "unrelated-token"})
    child_pids = []
    try:
        deadline = time.monotonic() + 10
        expected = ["connector"] + (["hermes"] if mode == "managed" else [])
        while not all((home / f"{name}-start.json").exists() for name in expected):
            if supervisor.poll() is not None:
                pytest.fail("Supervisor exited before starting the fixture connector: " + supervisor.stderr.read().decode())
            if time.monotonic() >= deadline:
                pytest.fail("Fixture connector failed to start")
            time.sleep(0.05)
        started = {name: read_json(home / f"{name}-start.json") for name in expected}
        child_pids = [record["pid"] for record in started.values()]
        assert "agent_control_connector" in started["connector"]["args"]
        assert "serve" not in started["connector"]["args"]
        assert all(record["home"] == str(home) and record["tokenMatches"] for record in started.values())
        if mode == "managed":
            hermes = started["hermes"]
            assert hermes["profile"] == "default" and hermes["effectiveHome"] == str(home)
            cli_args = hermes["args"][hermes["args"].index("-c") + 2:]
            assert cli_args[:3] == ["-p", "default", "serve"]
            assert hermes["remaining"] == ["serve", "--host", "127.0.0.1", "--port", "19119", "--isolated"]
            assert hermes["cwd"] == str(home)
        else:
            assert not (home / "hermes-start.json").exists()
        atomic_json(directory / "stop.request", {"reason": "test"})
        assert supervisor.wait(timeout=10) == 0
        assert external.poll() is None
        for child_pid in child_pids:
            with pytest.raises(ProcessLookupError):
                os.kill(child_pid, 0)
        assert read_json(connector / "config.json")["connectorId"] == "fixture"
        assert (home / "active_profile").read_text() == "science\n"
    finally:
        if supervisor.poll() is None:
            supervisor.terminate()
            supervisor.wait(timeout=10)
        for child_pid in child_pids:
            try:
                os.killpg(child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        external.terminate()
        external.wait(timeout=5)
        supervisor.stderr.close()
