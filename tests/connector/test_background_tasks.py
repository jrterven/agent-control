import hashlib
import json
import sqlite3
from dataclasses import replace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
import yaml
from hermes_client import InMemoryHermesProvider
from hermes_client.compatibility import HERMES_0212_SHA
from hermes_client.types import SessionRoute
from agent_control_connector import background_install as install
from agent_control_connector.background_tasks import retired_profile, snapshot
from agent_control_connector.hermes_background_plugin import PLUGIN_NAME
from agent_control_connector.runtime import ConnectorRuntime


def configuration(home, value):
    (home / "config.yaml").write_text(yaml.safe_dump(value))


def ledger(home):
    db = sqlite3.connect(home / "state.db")
    db.executescript("""CREATE TABLE sessions(id TEXT PRIMARY KEY);
        CREATE TABLE async_delegations(delegation_id TEXT PRIMARY KEY,parent_session_id TEXT,state TEXT,
          delivery_state TEXT,dispatched_at REAL,updated_at REAL,completed_at REAL,task_json TEXT,result_json TEXT);
        INSERT INTO sessions VALUES('first'),('second');""")
    return db


def task(db, identifier="dg-one", session="first", state="running", delivery="pending", started=1000):
    with db:
        db.execute("INSERT INTO async_delegations VALUES(?,?,?,?,?,?,?,?,?)", (identifier, session, state, delivery,
            started, 2000, None if state == "running" else 1900, '{"goal":"PRIVATE INTERNAL PROMPT"}', 'PRIVATE RESULT'))


def test_install_preserves_identity_prompts_schedules_and_explicit_denials(tmp_path):
    original = {"platform_toolsets": {"cli": ["web"], "cron": ["web"]}, "model": "personal",
        "agent": {"system_prompt": "keep this"}, "plugins": {"enabled": ["custom-plugin"]}}
    configuration(tmp_path, original)
    soul = tmp_path / "SOUL.md"
    soul.write_text("personal identity")
    (tmp_path / "cron").mkdir()
    cron = tmp_path / "cron/jobs.json"
    cron.write_text('{"jobs":[{"prompt":"private","schedule":"personal","enabled_toolsets":["web"]}]}')
    before = cron.read_bytes()
    assert install.install_profile(tmp_path)["state"] == "pendingActivation"
    changed = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert changed["platform_toolsets"] == {"cli": ["web", "delegation"], "cron": ["web"]}
    assert changed["model"] == original["model"] and changed["agent"] == original["agent"]
    assert changed["plugins"]["enabled"] == ["custom-plugin", PLUGIN_NAME]
    assert soul.read_text() == "personal identity" and cron.read_bytes() == before
    receipt = (tmp_path / "plugins" / PLUGIN_NAME / "installation.json").read_bytes()
    install.install_profile(tmp_path)
    assert (tmp_path / "plugins" / PLUGIN_NAME / "installation.json").read_bytes() == receipt
    # A subsequent UI deselection must not be re-enabled by periodic install.
    changed["platform_toolsets"]["cli"].remove("delegation")
    configuration(tmp_path, changed)
    assert install.install_profile(tmp_path) == {"state": "disabled"}


@pytest.mark.parametrize("config", [
    {"plugins": {"disabled": [PLUGIN_NAME]}},
    {"plugins": {"entries": {PLUGIN_NAME: {"enabled": False}}}},
    {"agent": {"disabled_toolsets": ["delegation"]}},
    {"agent": {"disabled_toolsets": '["delegation"]'}},
    {"tools": {"disabled": ["delegate_task"]}},
    {"platform_toolsets": {"cli": []}},
    {"platform_toolsets": {"cli": ["web"]}, "known_builtin_toolsets": {"cli": ["delegation"]}},
])
def test_install_respects_explicit_opt_out_without_writes(tmp_path, config):
    configuration(tmp_path, config)
    before = (tmp_path / "config.yaml").read_bytes()
    assert install.install_profile(tmp_path) == {"state": "disabled"}
    assert not (tmp_path / "plugins").exists()
    assert (tmp_path / "config.yaml").read_bytes() == before


def test_default_selections_and_plugin_removal_are_preserved(tmp_path):
    configuration(tmp_path, {"platform_toolsets": {"cron": []}})
    install.install_profile(tmp_path)
    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert config["platform_toolsets"] == {"cron": []}
    config["plugins"]["enabled"] = []
    configuration(tmp_path, config)
    assert install.install_profile(tmp_path) == {"state": "disabled"}


def test_probe_requires_exact_loaded_source_installation_and_live_process(tmp_path, monkeypatch):
    install.install_profile(tmp_path)
    receipt = json.loads((tmp_path / "plugins" / PLUGIN_NAME / "installation.json").read_text())
    marker = tmp_path / ".agent-control/background/runtime.json"
    marker.parent.mkdir(parents=True)
    value = {**receipt, "pid": 100, "processIdentity": "started", "delegationAvailable": True}
    marker.write_text(json.dumps(value))
    monkeypatch.setattr(install.os, "kill", lambda *_: None)
    monkeypatch.setattr(install, "process_identity", lambda _: "started")
    assert install.probe_profile(tmp_path)["state"] == "ready"
    value["installationId"] = "old"
    marker.write_text(json.dumps(value))
    assert install.probe_profile(tmp_path)["state"] == "pendingActivation"
    value["installationId"] = receipt["installationId"]
    value["processIdentity"] = "reused pid"
    marker.write_text(json.dumps(value))
    assert install.probe_profile(tmp_path)["state"] == "pendingActivation"


def test_scoped_ledger_projection_never_reads_prompts_and_has_scoped_counts(tmp_path):
    db = ledger(tmp_path)
    task(db)
    task(db, "dg-other", "second")
    task(db, "dg-complete", "first", "completed")
    db.close()
    before = hashlib.sha256((tmp_path / "state.db").read_bytes()).hexdigest()
    result = snapshot(tmp_path, "first")
    assert result["available"] and result["complete"]
    assert result["activeCount"] == 1 and result["pendingDeliveryCount"] == 1 and result["totalCount"] == 2
    assert {row["id"] for row in result["tasks"]} == {"dg-one", "dg-complete"}
    assert "PRIVATE" not in json.dumps(result) and "task_json" not in json.dumps(result)
    assert snapshot(tmp_path)["activeCount"] == 2
    assert snapshot(tmp_path, "absent")["tasks"] == []
    assert hashlib.sha256((tmp_path / "state.db").read_bytes()).hexdigest() == before


def test_truncated_orphaned_and_invalid_rows_do_not_certify_complete(tmp_path, monkeypatch):
    db = ledger(tmp_path)
    task(db, session="missing")
    assert snapshot(tmp_path)["complete"] is False
    assert snapshot(tmp_path)["activeCount"] == 1
    assert snapshot(tmp_path)["tasks"] == []
    task(db, "valid")
    monkeypatch.setattr("agent_control_connector.background_tasks.MAX_TASKS", 1)
    assert not snapshot(tmp_path)["complete"]
    db.close()


@pytest.mark.parametrize("state,delivery,expected", [
    ("completed", "delivered", True), ("failed", "dropped", True), ("cancelled", "delivered", True),
    ("running", "pending", False), ("completed", "pending", False), ("unknown", "delivered", False),
])
def test_deleted_parent_retained_terminal_metadata_does_not_block_idle(tmp_path, state, delivery, expected):
    db = ledger(tmp_path)
    task(db, state=state, delivery=delivery)
    with db:
        db.execute("DELETE FROM sessions WHERE id='first'")
    db.close()
    result = snapshot(tmp_path)
    assert result["complete"] is expected and result["tasks"] == []
    if expected:
        assert result["activeCount"] == result["pendingDeliveryCount"] == 0
    with sqlite3.connect(tmp_path / "state.db") as db:
        assert db.execute("SELECT count(*) FROM async_delegations").fetchone()[0] == 1


@pytest.mark.parametrize("state,expected", [("stalling", "running"), ("finalizing", "running"),
    ("interrupted", "cancelled"), ("error", "failed"), ("unknown", "unknown"), ("future-state", "unknown")])
def test_state_projection_preserves_uncertainty(tmp_path, state, expected):
    db = ledger(tmp_path)
    task(db, state=state)
    db.close()
    result = snapshot(tmp_path)
    assert result["tasks"][0]["state"] == expected
    if state == "future-state":
        assert not result["complete"]


def test_missing_or_aliased_db_never_means_idle(tmp_path):
    assert snapshot(tmp_path)["activeCount"] is None
    other = tmp_path / "foreign"
    other.mkdir()
    db = ledger(other)
    task(db)
    db.close()
    (tmp_path / "state.db").symlink_to(other / "state.db")
    assert not snapshot(tmp_path)["available"]


def test_empty_initialized_database_without_delegation_schema_is_verified_empty(tmp_path):
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
    result = snapshot(tmp_path)
    assert result["complete"] and result["activeCount"] == result["pendingDeliveryCount"] == 0
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("INSERT INTO sessions VALUES ('existing')")
    assert snapshot(tmp_path)["complete"] is False


def retired_fixture(root):
    home = root / "profiles/retired"
    home.mkdir(parents=True)
    deleted = root / "profiles/.deleted"
    deleted.mkdir()
    (deleted / "retired").write_bytes(b"deleted\n")
    with sqlite3.connect(home / "state.db") as db:
        db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
    return home


def test_native_retirement_requires_exact_tombstone_empty_database_and_no_configuration(tmp_path):
    home = retired_fixture(tmp_path)
    result = retired_profile(tmp_path, "retired")
    assert result["retired"] and result["complete"] and not result["available"]
    assert result["activeCount"] == result["pendingDeliveryCount"] == 0
    (home / "config.yaml").write_text("{}")
    assert retired_profile(tmp_path, "retired") is None
    (home / "config.yaml").unlink()
    with sqlite3.connect(home / "state.db") as db:
        db.execute("INSERT INTO sessions VALUES ('still-exists')")
    assert retired_profile(tmp_path, "retired") is None


@pytest.mark.parametrize("bad", ["missing", "symlink", "incorrect", "state_missing", "extra_artifact"])
def test_missing_or_aliased_retirement_evidence_remains_uncertain(tmp_path, bad):
    home = retired_fixture(tmp_path)
    marker = tmp_path / "profiles/.deleted/retired"
    if bad in {"missing", "symlink"}:
        marker.unlink()
        if bad == "symlink":
            other = tmp_path / "marker"
            other.write_bytes(b"deleted\n")
            marker.symlink_to(other)
    elif bad == "incorrect":
        marker.write_bytes(b"deleted?")
    elif bad == "state_missing":
        (home / "state.db").unlink()
    else:
        (home / ".env").write_text("private")
    assert retired_profile(tmp_path, "retired") is None


def test_retired_profiles_are_not_reinstalled_or_given_new_media_state(runtime, tmp_path):
    home = retired_fixture(tmp_path)
    runtime.config["profiles"].append("retired")
    observed = runtime._background_snapshot("retired")
    assert observed["retired"] and observed["complete"]
    result = install.background_profiles(runtime.config, install=True)
    assert result["retired"] == {"state": "retired"}
    runtime._save_media_policy(["retired"])
    assert {entry.name for entry in home.iterdir()} == {"state.db"}


@pytest.fixture
def runtime(tmp_path):
    value = ConnectorRuntime(tmp_path, {"gatewayId": "gateway", "profiles": ["default"], "restUrl": "http://127.0.0.1:9119",
        "wsUrl": "ws://127.0.0.1:9119/api/ws", "sourceSha": HERMES_0212_SHA, "hermesHome": str(tmp_path)},
        {"hermesToken": "test"}, InMemoryHermesProvider)
    yield value
    value.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["running", "pendingDelivery", "unavailable", "idle"])
async def test_idle_gate_counts_workers_and_pending_notifications(runtime, tmp_path, monkeypatch, kind):
    if kind != "unavailable":
        db = ledger(tmp_path)
        if kind != "idle":
            task(db, state="running" if kind == "running" else "completed")
        db.close()
    provider = runtime.providers["default"]
    provider.list_profiles = AsyncMock(return_value=[type("Profile", (), {"name": "default"})()])
    provider.list_sessions = AsyncMock(return_value=[])
    provider._session_inventory_complete = True
    provider.observe_background_tasks = Mock()
    async def stop(_):
        runtime.closed = True
    monkeypatch.setattr("agent_control_connector.runtime.asyncio.sleep", stop)
    await runtime._status_loop()
    value = json.loads((tmp_path / "status.json").read_text())
    assert value["activeWork"] is ({"running": True, "pendingDelivery": True, "unavailable": None, "idle": False}[kind])
    provider.observe_background_tasks.assert_called_once()
    observation = provider.observe_background_tasks.call_args.args[0]
    assert observation["complete"] is (kind != "unavailable")
    assert observation["activeCount"] == {"running": 1, "pendingDelivery": 0, "idle": 0, "unavailable": None}[kind]


@pytest.mark.asyncio
async def test_background_events_are_owned_unsequenced_and_deduplicated(runtime, tmp_path):
    db = ledger(tmp_path)
    task(db)
    db.close()
    runtime.websocket = object()
    runtime.background_tasks_supported = True
    runtime.background_states = {"default": {"state": "ready"}}
    runtime.on_event = AsyncMock()
    observed = snapshot(tmp_path)
    await runtime._background_events("default", observed)
    event = runtime.on_event.call_args.args[0]
    assert event.type == "background.tasks" and event.stored_session_id == "first"
    assert event.profile_name == "default" and event.sequence is None and event.runtime_session_id is None
    assert event.data["observedAt"] == observed["observedAt"] and event.data["activeCount"] == 1
    await runtime._background_events("default", snapshot(tmp_path))
    assert runtime.on_event.await_count == 1
    with sqlite3.connect(tmp_path / "state.db") as db:
        db.execute("DELETE FROM async_delegations")
    await runtime._background_events("default", snapshot(tmp_path))
    assert runtime.on_event.call_args.args[0].data["tasks"] == []
    assert runtime.on_event.call_args.args[0].data["complete"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["disabled", "pendingActivation", "ready"])
async def test_read_operation_preserves_drain_evidence_when_feature_unavailable(runtime, tmp_path, state):
    db = ledger(tmp_path)
    task(db)
    task(db, "other", "second")
    db.close()
    runtime.background_tasks_supported = True
    runtime.background_states = {"default": {"state": state}}
    request = {"v": 1, "type": "request", "id": uuid4().hex, "profile": "default",
        "operation": "list_background_tasks", "args": ("first",), "kwargs": {}, "operationId": "read"}
    result = (await runtime.execute(request))["result"]
    assert result["available"] is (state == "ready")
    assert result["complete"] and result["activeCount"] == result["totalCount"] == 1
    assert {task["storedSessionId"] for task in result["tasks"]} == {"first"}
    request["args"] = ()
    global_result = (await runtime.execute(request))["result"]
    assert global_result["activeCount"] == 2
    runtime.background_tasks_supported = False
    unsupported_feature = (await runtime.execute(request))["result"]
    assert not unsupported_feature["available"] and unsupported_feature["activeCount"] == 2


@pytest.mark.asyncio
async def test_incomplete_snapshot_never_emits_a_false_task_completion(runtime, tmp_path):
    db = ledger(tmp_path)
    task(db)
    db.close()
    runtime.websocket = object()
    runtime.background_tasks_supported = True
    runtime.background_states = {"default": {"state": "ready"}}
    runtime.on_event = AsyncMock()
    await runtime._background_events("default", snapshot(tmp_path))
    (tmp_path / "state.db").unlink()
    await runtime._background_events("default", snapshot(tmp_path))
    result = runtime.on_event.call_args.args[0].data
    assert result["complete"] is False and result["activeCount"] is None and result["tasks"] == []


def delete_request(operation, argument, profile="default"):
    return {"v": 1, "type": "request", "id": uuid4().hex, "profile": profile,
        "operation": operation, "args": (argument,), "kwargs": {}, "operationId": "delete-fixture"}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["running", "completed", "missing"])
async def test_session_deletion_refuses_background_work_before_reserving(runtime, tmp_path, state):
    if state != "missing":
        db = ledger(tmp_path)
        task(db, state=state)
        db.close()
    provider = runtime.providers["default"]
    provider.delete_session = AsyncMock()
    route = SessionRoute("gateway", "default", "first", "runtime")
    result = await runtime.execute(delete_request("delete_session", route))
    assert result["error"] == "CONNECTOR_BACKGROUND_BUSY"
    provider.delete_session.assert_not_awaited()
    assert runtime.ledger.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_session_delete_is_scoped_and_completed_receipt_replays_without_deleted_database(runtime, tmp_path):
    db = ledger(tmp_path)
    task(db, session="second")
    db.close()
    provider = runtime.providers["default"]
    provider.delete_session = AsyncMock(return_value=None)
    route = SessionRoute("gateway", "default", "first", "runtime")
    request = delete_request("delete_session", route)
    first = await runtime.execute(request)
    assert first.get("error") is None
    (tmp_path / "state.db").unlink()
    replay = await runtime.execute(request)
    assert replay.get("error") is None and replay["result"] is None
    provider.delete_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_profile_deletion_checks_target_profile_not_manager_profile(runtime, tmp_path):
    manager = runtime.providers["default"]
    target_home = tmp_path / "profiles/target"
    target_home.mkdir(parents=True)
    db = ledger(target_home)
    task(db)
    db.close()
    manager_db = ledger(tmp_path)
    manager_db.close()
    runtime.providers["target"] = InMemoryHermesProvider(replace(manager.connection, profile_name="target"))
    manager.delete_profile = AsyncMock(return_value=None)
    request = delete_request("delete_profile", "target")
    assert (await runtime.execute(request))["error"] == "CONNECTOR_BACKGROUND_BUSY"
    manager.delete_profile.assert_not_awaited()
    assert runtime.ledger.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0
    with sqlite3.connect(target_home / "state.db") as db:
        db.execute("DELETE FROM async_delegations")
    with sqlite3.connect(tmp_path / "state.db") as db:
        task(db)
    assert (await runtime.execute(request)).get("error") is None
    manager.delete_profile.assert_awaited_once_with("target")


def test_explicit_install_does_not_replace_an_existing_maintenance_request(tmp_path, monkeypatch):
    from agent_control_connector.cli import main
    marker = tmp_path / "maintenance.request"
    marker.write_text("another operation")
    installer = Mock()
    monkeypatch.setattr(install, "background_profiles", installer)
    assert main(["install-background", "--data-dir", str(tmp_path)]) == 1
    assert marker.read_text() == "another operation"
    installer.assert_not_called()


def test_explicit_install_cleans_only_its_own_maintenance_request(tmp_path, monkeypatch):
    from agent_control_connector.cli import main
    marker = tmp_path / "maintenance.request"
    (tmp_path / "config.json").write_text("{}")
    (tmp_path / "config.json").chmod(0o600)
    monkeypatch.setattr("agent_control_connector.manage.drain", lambda _: marker.write_text("our request"))
    def install_then_changed(_config, **_kwargs):
        marker.write_text("different request")
        return {}
    monkeypatch.setattr(install, "background_profiles", install_then_changed)
    assert main(["install-background", "--data-dir", str(tmp_path)]) == 0
    assert marker.read_text() == "different request"
