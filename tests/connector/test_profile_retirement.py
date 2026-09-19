import copy
import shutil
import sqlite3
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from agent_control_connector import runtime as runtime_module
from agent_control_connector.runtime import ConnectorRuntime
from agent_control_connector.storage import atomic_json, read_json
from hermes_client import InMemoryHermesProvider
from hermes_client.compatibility import HERMES_0212_SHA
from hermes_client.provider import ProfileManagementServerRequired
from hermes_client.connector_protocol import frames
from hermes_client.types import HermesProfile, NormalizedEvent


def request(name="control-dev", manager="default", identity=None):
    return {"v": 1, "id": uuid4().hex, "type": "request", "profile": manager,
        "operation": "delete_profile", "args": (name,), "kwargs": {}, "operationId": identity or uuid4().hex}


@pytest.fixture
def runtime(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    directory = tmp_path / "connector"
    config = {"gatewayId": "gateway", "profiles": ["default", "control-dev", "other"],
        "restUrl": "http://127.0.0.1:9119", "wsUrl": "ws://127.0.0.1:9119/api/ws",
        "sourceSha": HERMES_0212_SHA, "hermesHome": str(home), "server": "https://control.test"}
    value = ConnectorRuntime(directory, config, {"hermesToken": "local", "accessToken": "cloud"}, InMemoryHermesProvider)
    atomic_json(directory / "config.json", config)
    native = set(config["profiles"])
    value.native_profiles = native
    for name, provider in value.providers.items():
        profile_home = home if name == "default" else home / "profiles" / name
        profile_home.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(profile_home / "state.db") as db:
            db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY)")
        provider.list_profiles = AsyncMock(side_effect=lambda: [HermesProfile(name=p, display_name=p) for p in sorted(native)])
        provider.list_sessions = AsyncMock(return_value=[])
        provider._session_inventory_complete = True
        provider.close = AsyncMock()
    async def delete(name):
        shutil.rmtree(home / "profiles" / name)
        marker = home / "profiles" / ".deleted" / name
        marker.parent.mkdir(exist_ok=True)
        marker.write_bytes(b"deleted\n")
        native.remove(name)
    for provider in value.providers.values():
        provider.delete_profile = AsyncMock(side_effect=delete)
    yield value
    value.ledger.close()


@pytest.mark.asyncio
async def test_delete_refused_before_ledger_or_native_mutation_when_server_is_profile_bound(runtime):
    provider = runtime.providers["default"]
    provider.assert_default_management_server = AsyncMock(side_effect=ProfileManagementServerRequired())
    original = runtime.config["profiles"].copy()
    command = request()
    response = await runtime.execute(command)
    assert response["error"] == "HERMES_DEFAULT_SERVER_REQUIRED"
    provider.delete_profile.assert_not_called()
    assert runtime.config["profiles"] == original
    assert runtime.native_profiles == set(original)
    # No uncertain write reservation is created by a failed read-only proof.
    provider.assert_default_management_server = AsyncMock()
    assert (await runtime.execute(command)).get("error") is None
    provider.delete_profile.assert_awaited_once_with("control-dev")


@pytest.mark.asyncio
async def test_confirmed_delete_removes_only_its_binding_and_replays_after_restart_without_adoption(runtime):
    provider = runtime.providers["default"]
    retired = runtime.providers["control-dev"]
    runtime.media_states = {"control-dev": {"state": "ready"}, "other": {"state": "ready"}}
    runtime.background_states = copy.deepcopy(runtime.media_states)
    runtime.media_pending = {"retired-file": "control-dev", "other-file": "other"}
    runtime.background_fingerprints = {("control-dev", "session"): "a", ("other", "session"): "b"}
    for name in ("control-dev", "other"):
        await runtime.on_event(NormalizedEvent.create(type="control.reconcile", gateway_id="gateway",
            profile_name=name, runtime_generation="generation", data={}))
    command = request()
    first = await runtime.execute(command)
    assert first.get("error") is None and first["result"] is None
    provider.delete_profile.assert_awaited_once_with("control-dev")
    retired.close.assert_awaited_once()
    assert set(runtime.providers) == {"default", "other"}
    assert read_json(runtime.directory / "config.json")["profiles"] == ["default", "other"]
    assert runtime.media_states == runtime.background_states == {"other": {"state": "ready"}}
    assert runtime.media_pending == {"other-file": "other"}
    assert runtime.background_fingerprints == {("other", "session"): "b"}
    assert [event[1]["event"].profile_name for event in runtime.events] == ["other"]
    assert runtime.event_bytes == sum(event[2] for event in runtime.events)
    listed = await runtime.execute({**command, "id": uuid4().hex, "operation": "list_profiles", "args": ()})
    assert {profile.name for profile in listed["result"]} == {"default", "other"}
    # An unrelated new local agent reusing the technical name stays private.
    runtime.native_profiles.add("control-dev")
    assert (await runtime.execute(command)).get("error") is None
    assert (await runtime.execute(request()))["error"] == "INVALID_OPERATION"
    provider.delete_profile.assert_awaited_once()
    restarted = ConnectorRuntime(runtime.directory, read_json(runtime.directory / "config.json"), runtime.secrets, InMemoryHermesProvider)
    try:
        manager = restarted.providers["default"]
        manager.delete_profile = AsyncMock()
        assert (await restarted.execute(command)).get("error") is None
        assert "control-dev" not in restarted.providers
        manager.delete_profile.assert_not_awaited()
    finally:
        restarted.ledger.close()


@pytest.mark.asyncio
async def test_native_acknowledgement_without_confirmed_absence_preserves_binding_and_unknown_receipt(runtime):
    manager = runtime.providers["default"]
    manager.delete_profile = AsyncMock(return_value=None)
    original = read_json(runtime.directory / "config.json")
    command = request()
    assert (await runtime.execute(command))["error"] == "CONNECTOR_DELIVERY_UNKNOWN"
    assert (await runtime.execute(command))["error"] == "CONNECTOR_DELIVERY_UNKNOWN"
    manager.delete_profile.assert_awaited_once()
    assert read_json(runtime.directory / "config.json") == original
    assert "control-dev" in runtime.providers
    runtime.providers["control-dev"].close.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_or_unshared_profile_cannot_be_deleted(runtime):
    for name in ("default", "private", "../control-dev"):
        assert (await runtime.execute(request(name)))["error"] == "INVALID_OPERATION"
    runtime.providers["default"].delete_profile.assert_not_awaited()
    assert runtime.ledger.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_last_profile_retirement_allows_empty_restart_and_receipt_only_self_replay(runtime):
    runtime.config["profiles"] = ["control-dev"]
    runtime.providers = {"control-dev": runtime.providers["control-dev"]}
    command = request(manager="control-dev")
    first = await runtime.execute(command)
    assert first.get("error") is None
    assert runtime.providers == {} and runtime.config["profiles"] == []
    # Nothing re-creates the retired provider, even after a process restart.
    restarted = ConnectorRuntime(runtime.directory, read_json(runtime.directory / "config.json"), runtime.secrets, InMemoryHermesProvider)
    try:
        replay = await restarted.execute(command)
        assert replay.get("error") is None and replay["generation"] == first["generation"]
        assert restarted.providers == {}
        assert (await restarted.execute(request(manager="control-dev")))["error"] == "INVALID_OPERATION"
    finally:
        restarted.ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("capabilities,expected", [
    ({"profileTransferV1": True}, False), ({"profileTransferV2": True}, False),
    ({"profileTransferV3": True}, True), ({"profileTransferV2": True, "profileTransferV3": False}, False),
    ({"profileTransferV1": True, "profileTransferV2": False}, False), ({}, False),
])
async def test_reconnect_requires_v3_and_never_adopts_stale_cloud_grants(runtime, monkeypatch, capabilities, expected):
    command = request()
    assert (await runtime.execute(command)).get("error") is None
    class Socket:
        send = AsyncMock()
        recv = AsyncMock(return_value=next(iter(frames({"v": 1, "type": "welcome", "gatewayId": "gateway",
            "profiles": ["default", "control-dev", "other", "foreign"], "capabilities": capabilities}))))
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise StopAsyncIteration
    @asynccontextmanager
    async def connect(*args, **kwargs):
        yield Socket()
    monkeypatch.setattr(runtime_module, "connect", connect)
    await runtime._connection()
    assert runtime.profile_transfer_supported is expected
    assert set(runtime.providers) == {"default", "other"}
    assert runtime.config["profiles"] == ["default", "other"]


@pytest.mark.asyncio
async def test_retired_absent_native_directory_does_not_reopen_provider_or_block_idle(runtime, monkeypatch):
    retired = runtime.providers["control-dev"]
    await runtime.providers["default"].delete_profile("control-dev")
    async def stop(_):
        runtime.closed = True
    monkeypatch.setattr(runtime_module.asyncio, "sleep", stop)
    await runtime._status_loop()
    assert read_json(runtime.directory / "status.json")["activeWork"] is False
    retired.list_profiles.assert_not_awaited()
    retired.list_sessions.assert_not_awaited()
    from pathlib import Path
    assert not (Path(runtime.config["hermesHome"]) / "profiles/control-dev").exists()
