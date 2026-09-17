"""A revoked standalone connector can receive a fresh, explicit web approval."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path
import platform
from types import SimpleNamespace

import pytest

from agent_control_connector import cli, manage
from agent_control_connector.storage import SecretStore, atomic_json, read_json


@pytest.fixture
def paired(tmp_path, monkeypatch):
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: user))
    home = user / "connector"
    release = home / "releases/r1"
    release.mkdir(parents=True)
    binary = release / "agent-control-connector"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    atomic_json(release / "release.json", {"revision": "r1", "protocol": 1,
        "system": platform.system(), "architecture": platform.machine()})
    (home / "current").symlink_to(release)
    config = {"server": "https://control.test", "connectorId": "revoked", "gatewayId": "old-gateway",
        "profiles": ["default"], "hermesHome": str(user / "a custom home"),
        "hermesSource": str(user / "a custom source"), "sourceSha": "audit",
        "restUrl": "http://127.0.0.1:9123/custom", "wsUrl": "ws://127.0.0.1:9123/custom/ws"}
    atomic_json(home / "config.json", config)
    (home / "operations.sqlite3").write_bytes(b"uncertain-operation-must-survive")
    secrets = {"accessToken": "old-cloud-token", "hermesToken": "private-hermes-" + "x" * 32}
    saved = [deepcopy(secrets)]
    monkeypatch.setattr(SecretStore, "load", lambda _: deepcopy(saved[-1]))
    monkeypatch.setattr(SecretStore, "save", lambda _, value: saved.append(deepcopy(value)))
    manage.write_service(home)
    events = []
    running = [True]
    monkeypatch.setattr(manage, "runtime_running", lambda _: running[0])
    def drain(directory):
        events.append("drain")
        (directory / "maintenance.request").write_text("requested")
    monkeypatch.setattr(manage, "drain", drain)
    def service(action):
        events.append(action)
        running[0] = action == "start"
    monkeypatch.setattr(manage, "service", service)
    def restored(directory):
        assert not (directory / "maintenance.request").exists()
        events.append("restore")
    monkeypatch.setattr(manage, "restore_service", restored)
    return SimpleNamespace(home=home, config=config, secrets=secrets, saved=saved, events=events, release=release, running=running)


def test_reconnect_reuses_custom_hermes_settings_and_token_until_fresh_approval(paired, monkeypatch):
    async def authorize(args, *, existing_config, saved_token):
        assert read_json(paired.home / "config.json") == paired.config
        assert paired.saved == [paired.secrets]
        assert existing_config == paired.config
        assert saved_token == paired.secrets["hermesToken"]
        assert args.hermes_home == paired.config["hermesHome"]
        assert args.hermes_source == paired.config["hermesSource"]
        assert args.rest_url == paired.config["restUrl"]
        assert args.ws_url == paired.config["wsUrl"]
        assert args.profiles is None  # Rediscover; the web must approve profiles afresh.
        with pytest.raises(ValueError, match="still running"):
            with manage.stopped_runtime(paired.home):
                pytest.fail("Runtime identity lock must be held during approval")
        paired.events.append("approve")
        return {**existing_config, "connectorId": "new", "gatewayId": "new-gateway", "profiles": ["turing"]}, {
            "accessToken": "new-cloud-token", "hermesToken": saved_token}
    monkeypatch.setattr(cli, "authorize_pairing", authorize)
    assert manage.management_main(["install-service", "--reconnect", "--data-dir", str(paired.home),
                                   "--server", "https://control.test"]) == 0
    assert paired.events == ["drain", "stop", "approve", "restore"]
    assert read_json(paired.home / "config.json")["connectorId"] == "new"
    assert paired.saved[-1]["accessToken"] == "new-cloud-token"
    assert paired.saved[-1]["hermesToken"] == paired.secrets["hermesToken"]
    assert (paired.home / "operations.sqlite3").read_bytes() == b"uncertain-operation-must-survive"
    assert (paired.home / "current").resolve() == paired.release


@pytest.mark.parametrize("error", [RuntimeError("Pairing expired"), KeyboardInterrupt()])
def test_canceled_pairing_restores_previous_service_and_identity(paired, monkeypatch, error):
    async def canceled(*args, **kwargs):
        raise error
    monkeypatch.setattr(cli, "authorize_pairing", canceled)
    with pytest.raises(type(error)):
        manage.reconnect(paired.home, "https://control.test")
    assert paired.events == ["drain", "stop", "start"]
    assert paired.saved == [paired.secrets]
    assert read_json(paired.home / "config.json") == paired.config
    assert not (paired.home / "maintenance.request").exists()


@pytest.mark.parametrize("failure", ["server", "managed", "setup-existing", "credentials", "malformed-credentials", "token", "endpoint", "release", "service"])
def test_reconnect_preflight_leaves_running_identity_unchanged(paired, monkeypatch, failure):
    server = "https://control.test"
    if failure == "server":
        server = "https://another.test"
    elif failure in {"managed", "setup-existing"}:
        atomic_json(paired.home / "config.json", {**paired.config, "installationKind": "managed" if failure == "managed" else "existing"})
    elif failure == "credentials":
        monkeypatch.setattr(SecretStore, "load", lambda _: (_ for _ in ()).throw(RuntimeError("Keychain denied")))
    elif failure == "malformed-credentials":
        monkeypatch.setattr(SecretStore, "load", lambda _: [])
    elif failure == "token":
        paired.saved[-1]["hermesToken"] = "invalid"
    elif failure == "endpoint":
        atomic_json(paired.home / "config.json", {**paired.config, "restUrl": "https://remote.example"})
    elif failure == "release":
        (paired.release / "release.json").unlink()
    else:
        manage.service_path().write_text("unrelated service")
    original = (paired.home / "config.json").read_bytes()
    with pytest.raises((ValueError, RuntimeError)):
        manage.reconnect(paired.home, server)
    assert paired.events == []
    assert (paired.home / "config.json").read_bytes() == original


def test_reconnect_refuses_active_work_before_service_stop(paired, monkeypatch):
    def active(_):
        raise ValueError("Hermes has active or uncertain work")
    monkeypatch.setattr(manage, "drain", active)
    with pytest.raises(ValueError, match="active or uncertain"):
        manage.reconnect(paired.home, "https://control.test")
    assert paired.events == []
    assert paired.saved == [paired.secrets]


def test_interrupted_idle_check_cleans_marker_without_stopping_service(paired, monkeypatch):
    def interrupted(directory):
        (directory / "maintenance.request").write_text("requested")
        raise KeyboardInterrupt()
    monkeypatch.setattr(manage, "drain", interrupted)
    assert manage.management_main(["install-service", "--reconnect", "--server", "https://control.test",
                                   "--data-dir", str(paired.home)]) == 130
    assert paired.events == []
    assert not (paired.home / "maintenance.request").exists()
    assert read_json(paired.home / "config.json") == paired.config


@pytest.mark.parametrize("stopped", [True, False])
def test_interrupted_service_stop_recovers_only_if_service_actually_stopped(paired, monkeypatch, stopped):
    def service(action):
        paired.events.append(action)
        if action == "stop":
            paired.running[0] = not stopped
            raise KeyboardInterrupt()
        paired.running[0] = True
    monkeypatch.setattr(manage, "service", service)
    with pytest.raises(KeyboardInterrupt):
        manage.reconnect(paired.home, "https://control.test")
    assert paired.events == ["drain", "stop", *(["start"] if stopped else [])]
    assert not (paired.home / "maintenance.request").exists()
    assert read_json(paired.home / "config.json") == paired.config


def test_reconnect_refuses_manual_runtime_without_owned_service(paired):
    manage.service_path().unlink()
    with pytest.raises(ValueError, match="outside its installed service"):
        manage.reconnect(paired.home, "https://control.test")
    assert paired.events == []


def test_reconnect_stopped_install_does_not_start_it_on_cancel(paired, monkeypatch):
    monkeypatch.setattr(manage, "runtime_running", lambda _: False)
    async def canceled(*args, **kwargs):
        raise RuntimeError("Pairing expired")
    monkeypatch.setattr(cli, "authorize_pairing", canceled)
    with pytest.raises(RuntimeError, match="expired"):
        manage.reconnect(paired.home, "https://control.test")
    assert paired.events == ["stop"]


def test_approved_identity_survives_startup_failure_for_service_resume(paired, monkeypatch):
    async def authorize(*args, **kwargs):
        return {**paired.config, "connectorId": "approved"}, {**paired.secrets, "accessToken": "approved-token"}
    monkeypatch.setattr(cli, "authorize_pairing", authorize)
    monkeypatch.setattr(manage, "restore_service", lambda _: (_ for _ in ()).throw(ValueError("startup failed")))
    with pytest.raises(ValueError, match="startup failed"):
        manage.reconnect(paired.home, "https://control.test")
    assert read_json(paired.home / "config.json")["connectorId"] == "approved"
    assert paired.saved[-1]["accessToken"] == "approved-token"
    assert paired.events == ["drain", "stop"]


def test_identity_write_failure_restores_both_previous_credentials_and_service(paired, monkeypatch):
    from agent_control_connector import storage
    async def authorize(*args, **kwargs):
        return {**paired.config, "connectorId": "approved"}, {**paired.secrets, "accessToken": "approved-token"}
    monkeypatch.setattr(cli, "authorize_pairing", authorize)
    real_write = storage.atomic_json
    def failing_write(path, value):
        if value.get("connectorId") == "approved":
            raise OSError("disk full")
        real_write(path, value)
    monkeypatch.setattr(storage, "atomic_json", failing_write)
    with pytest.raises(OSError, match="disk full"):
        manage.reconnect(paired.home, "https://control.test")
    assert paired.saved[-1] == paired.secrets
    assert read_json(paired.home / "config.json") == paired.config
    assert paired.events == ["drain", "stop", "start"]


def test_reconnect_does_not_place_private_token_in_process_arguments(paired, monkeypatch, tmp_path):
    replacement_token = "rotated-private-token-" + "z" * 32
    token_file = tmp_path / "token"
    token_file.write_text(replacement_token)
    token_file.chmod(0o600)
    async def authorize(args, *, saved_token, **kwargs):
        assert saved_token == replacement_token
        assert replacement_token not in repr(args)
        return paired.config, {**paired.secrets, "hermesToken": saved_token}
    monkeypatch.setattr(cli, "authorize_pairing", authorize)
    monkeypatch.setattr(manage.subprocess, "run", lambda *a, **k: pytest.fail("Credentials cannot be forwarded through argv"))
    manage.reconnect(paired.home, "https://control.test", token_file=str(token_file))
    assert paired.saved[-1]["hermesToken"] == replacement_token


def test_existing_management_lock_prevents_reconnection(paired):
    with manage.management_lock(paired.home):
        result = manage.management_main(["install-service", "--reconnect", "--server", "https://control.test",
                                         "--data-dir", str(paired.home)])
    assert result == 1
    assert paired.events == []


def test_source_manifest_is_checked_before_reconnecting(paired):
    result = manage.management_main(["install-service", "--reconnect", "--data-dir", str(paired.home),
        "--server", "https://control.test", "--source", str(paired.release), "--release", "forged"])
    assert result == 1
    assert paired.events == []


def test_authorization_discovers_profiles_without_overwriting_existing_identity(paired, monkeypatch):
    revision = next(iter(cli.AUDITED_REVISIONS))
    monkeypatch.setattr(cli, "detect_revision", lambda home, source: (revision, Path(source)))
    advertised = []
    class Provider:
        def __init__(self, connection):
            assert connection.rest_url == paired.config["restUrl"]
            assert connection.dashboard_token == paired.secrets["hermesToken"]
        async def capabilities(self):
            return SimpleNamespace(version=cli.AUDITED_REVISIONS[revision][0])
        async def list_profiles(self):
            return [SimpleNamespace(name="default"), SimpleNamespace(name="turing")]
        async def close(self):
            pass
    monkeypatch.setattr(cli, "HermesGatewayProvider", Provider)
    class Client:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, path, *, json):
            assert read_json(paired.home / "config.json") == paired.config
            assert paired.saved == [paired.secrets]
            if path.endswith("authorize"):
                advertised.extend(json["profiles"])
                payload = {"userCode": "ABCD-1234", "deviceCode": "private-device-code"}
            else:
                payload = {"profiles": ["turing"], "connectorId": "new", "gatewayId": "new-gateway", "accessToken": "new-token"}
            return SimpleNamespace(status_code=200, json=lambda: payload)
    monkeypatch.setattr(cli.httpx, "AsyncClient", Client)
    async def no_wait(_):
        pass
    monkeypatch.setattr(cli.asyncio, "sleep", no_wait)
    args = SimpleNamespace(data_dir=str(paired.home), server="https://control.test", name=None,
        hermes_home=paired.config["hermesHome"], hermes_source=paired.config["hermesSource"],
        rest_url=paired.config["restUrl"], ws_url=paired.config["wsUrl"], profiles=None)
    config, secrets = asyncio.run(cli.authorize_pairing(args, existing_config=paired.config,
                                                       saved_token=paired.secrets["hermesToken"]))
    assert advertised == ["default", "turing"]
    assert config["profiles"] == ["turing"] and config["connectorId"] == "new"
    assert secrets["hermesToken"] == paired.secrets["hermesToken"]
    assert read_json(paired.home / "config.json") == paired.config
    assert paired.saved == [paired.secrets]
