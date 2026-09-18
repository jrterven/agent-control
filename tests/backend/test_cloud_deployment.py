"""Release preflights are verified without connecting to a production host."""
from __future__ import annotations

import importlib.util
import json
import os
from email.message import Message
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit

import pytest

from hermes_control_api import cloud_migrations
from .test_cloud_accounts import cloud  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("cloud_release_verify", REPO / "deploy/cloud/verify.py")
verification = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(verification)


@pytest.mark.parametrize("watcher_name", ["automation_route_health", "capability_refresh_health"])
def test_cloud_readiness_fails_for_a_broken_supervisor_then_recovers(cloud, watcher_name):
    app, client, _ = cloud
    watcher = getattr(app.state, watcher_name)
    watcher.mark_failure()
    response = client.get("/api/v1/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["database"] == "ready"
    watcher.mark_success()
    assert client.get("/api/v1/ready").json()["status"] == "ready"


def test_migration_check_locates_source_config_when_imported_from_an_installed_wheel(tmp_path, monkeypatch):
    config = tmp_path / "apps/api/alembic.ini"
    config.parent.mkdir(parents=True)
    config.write_text("[alembic]\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cloud_migrations, "__file__", str(tmp_path / "venv/site-packages/hermes_control_api/cloud_migrations.py"))
    assert cloud_migrations.alembic_config_path() == config


def test_migration_check_normalizes_psycopg_and_only_changes_database():
    result = cloud_migrations.restored_database_url(
        "postgresql://operator:encoded%40password@postgres:5432/agent_control?sslmode=require",
        "control_release_check_test",
    )
    assert result.drivername == "postgresql+psycopg"
    assert result.database == "control_release_check_test"
    assert result.password == "encoded@password"
    assert result.host == "postgres" and result.port == 5432
    assert result.query == {"sslmode": "require"}
    with pytest.raises(SystemExit, match="PostgreSQL"):
        cloud_migrations.restored_database_url("sqlite:///production.db", "control_release_check_test")


def responses():
    return {
        "/api/v1/health": (json.dumps({"status": "ok"}).encode(), "application/json"),
        "/api/v1/ready": (json.dumps({"status": "ready"}).encode(), "application/json"),
        "/api/v1/auth/methods": (b'{"mode":"cloud","googleEnabled":true}', "application/json"),
        "/": (b'<link href="/manifest.webmanifest" rel="manifest"><script src="/assets/index.js"></script><script src="/boot-recovery.js"></script>', "text/html"),
        "/manifest.webmanifest": (b'{"name":"Agent Control","start_url":"/chats","icons":[{"src":"/icon-192.png"}]}', "application/manifest+json"),
        "/icon-192.png": (b"png", "image/png"),
        "/assets/index.js": (b"window.app=true", "application/javascript"),
        "/boot-recovery.js": (b"window.boot=true", "application/javascript"),
        "/sw.js": (b'define(["./workbox-runtime"],function(){importScripts("/notification-sw.js")})', "application/javascript"),
        "/workbox-runtime.js": (b"self.runtime=true", "application/javascript"),
        "/notification-sw.js": (b"self.push=true", "application/javascript"),
    }


def stub_https(monkeypatch, files, redirects=None):
    visited = []
    class Response:
        def __init__(self, url, body, kind):
            self.url = url
            self.body = body
            self.headers = Message()
            self.headers["Content-Type"] = kind
        def read(self, size):
            return self.body[:size]
        def __enter__(self):
            return self
        def __exit__(self, *_):
            pass
    def open_url(url, timeout):
        path = urlsplit(url).path
        visited.append(path)
        body, kind = files[path]
        return Response((redirects or {}).get(path, url), body, kind)
    monkeypatch.setattr(verification.urllib.request, "urlopen", open_url)
    return visited


def test_public_verifier_checks_boot_and_service_worker_dependencies(monkeypatch):
    visited = stub_https(monkeypatch, responses())
    verification.verify("https://control.test")
    assert {"/api/v1/auth/methods", "/boot-recovery.js", "/workbox-runtime.js", "/notification-sw.js"} <= set(visited)


@pytest.mark.parametrize("mode", ["invite_only", "open"])
def test_public_verifier_checks_expected_registration_policy_and_capacity(monkeypatch, mode):
    files = responses()
    files["/api/v1/auth/methods"] = (json.dumps({
        "mode": "cloud", "googleEnabled": True, "registrationMode": mode, "betaMaxUsers": 20,
    }).encode(), "application/json")
    stub_https(monkeypatch, files)
    verification.verify("https://control.test")
    verification.verify("https://control.test", registration_mode=mode, beta_max_users=20)
    with pytest.raises(ValueError, match="policy"):
        verification.verify("https://control.test", registration_mode="open" if mode == "invite_only" else "invite_only")
    with pytest.raises(ValueError, match="capacity"):
        verification.verify("https://control.test", beta_max_users=19)


def test_public_verifier_requires_registration_metadata_when_expected(monkeypatch):
    stub_https(monkeypatch, responses())
    with pytest.raises(ValueError, match="policy"):
        verification.verify("https://control.test", registration_mode="open", beta_max_users=20)


@pytest.mark.parametrize("missing", ["/boot-recovery.js", "/notification-sw.js", "/workbox-runtime.js"])
def test_public_verifier_rejects_spa_fallback_in_place_of_required_scripts(monkeypatch, missing):
    files = responses()
    files[missing] = (b"<html>fallback</html>", "text/html")
    stub_https(monkeypatch, files)
    with pytest.raises(ValueError, match="missing"):
        verification.verify("https://control.test")


def test_public_verifier_rejects_downgrades_and_private_auth_mode(monkeypatch):
    files = responses()
    stub_https(monkeypatch, files, {"/manifest.webmanifest": "http://control.test/manifest.webmanifest"})
    with pytest.raises(ValueError, match="redirect"):
        verification.verify("https://control.test")
    files["/api/v1/auth/methods"] = (b'{"mode":"private","googleEnabled":false}', "application/json")
    stub_https(monkeypatch, files)
    with pytest.raises(ValueError, match="Google"):
        verification.verify("https://control.test")


@pytest.fixture
def fake_deployment(tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    config = tmp_path / "compose.env"
    old_image = "ghcr.io/test/control@sha256:" + "a" * 64
    new_image = "ghcr.io/test/control@sha256:" + "b" * 64
    config.write_text(f"HERMES_CONTROL_IMAGE={old_image}\n")
    backups = tmp_path / "backups"
    backups.mkdir()
    log = tmp_path / "commands.jsonl"
    docker = binaries / "docker"
    docker.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
log = Path(os.environ["TEST_DEPLOY_LOG"])
previous = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
with log.open("a") as output:
    output.write(json.dumps({"args":args,"image":os.environ.get("HERMES_CONTROL_IMAGE")})+"\\n")
failure = os.environ.get("TEST_DEPLOY_FAIL", "")
if "ps" in args:
    print("current-container")
elif "cloud_operations" in " ".join(args) and args[-1] == "drain":
    drains = sum(row["args"][-1:] == ["drain"] for row in previous) + 1
    if failure == "drain-first" or (failure == "drain-second" and drains == 2): sys.exit(41)
    print('{"safeToRestart":true,"draining":true}')
elif "pg_dump" in args:
    print("verified-archive")
elif "pg_restore" in args:
    sys.stdin.buffer.read()
elif "hermes_control_api.cloud_migrations" in args:
    if failure == "migration": sys.exit(42)
elif "up" in args:
    if failure == "cutover": sys.exit(43)
elif "-c" in args:
    print("https://control.test")
''')
    docker.chmod(0o755)
    flock = binaries / "flock"
    flock.write_text('#!/bin/sh\n[ "$TEST_DEPLOY_FAIL" != "lock" ]\n')
    flock.chmod(0o755)
    python = binaries / "python3"
    python.write_text(f"#!{sys.executable}\n" + '''import json, os
with open(os.environ["TEST_DEPLOY_LOG"], "a") as output:
    output.write(json.dumps({"args":["verify"]})+"\\n")
''')
    python.chmod(0o755)
    env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"], "TEST_DEPLOY_LOG": str(log)}
    # Simulate a stale caller environment: final cutover must still use the
    # immutable digest explicitly passed to release.sh.
    env["HERMES_CONTROL_IMAGE"] = old_image
    def run(failure=""):
        result = subprocess.run(["bash", str(REPO / "deploy/cloud/release.sh"), str(config), new_image, str(backups)],
                                env={**env, "TEST_DEPLOY_FAIL": failure}, text=True, capture_output=True, timeout=15)
        commands = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        return result, commands
    return run, config, backups, old_image, new_image


def test_release_rechecks_work_after_rehearsal_and_pins_cutover_digest(fake_deployment):
    run, config, backups, old_image, new_image = fake_deployment
    result, commands = run()
    assert result.returncode == 0, result.stderr
    args = [row["args"] for row in commands]
    drains = [i for i, command in enumerate(args) if command[-1:] == ["drain"]]
    migration = next(i for i, command in enumerate(args) if "hermes_control_api.cloud_migrations" in command)
    cutover = next(i for i, command in enumerate(args) if "up" in command)
    assert len(drains) == 2 and drains[0] < migration < drains[1] < cutover
    assert commands[migration]["image"] == commands[cutover]["image"] == new_image
    assert args[cutover + 2] == ["verify"]
    assert config.read_text() == f"HERMES_CONTROL_IMAGE={new_image}\n"
    assert config.with_suffix(".env.previous").read_text() == f"HERMES_CONTROL_IMAGE={old_image}\n"
    archives = list(backups.glob("control-*.dump"))
    assert len(archives) == 1 and archives[0].stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("failure", ["drain-first", "migration", "drain-second"])
def test_preflight_failure_resumes_old_service_without_changing_image(fake_deployment, failure):
    run, config, _, old_image, _ = fake_deployment
    result, commands = run(failure)
    assert result.returncode != 0
    assert config.read_text() == f"HERMES_CONTROL_IMAGE={old_image}\n"
    assert not any("up" in row["args"] for row in commands)
    assert sum(row["args"][-1:] == ["resume"] for row in commands) == 1
    assert not config.with_suffix(".env.previous").exists()


def test_failed_cutover_keeps_previous_config_without_blindly_resuming_or_downgrading(fake_deployment):
    run, config, _, old_image, new_image = fake_deployment
    result, commands = run("cutover")
    assert result.returncode != 0
    assert new_image in config.read_text()
    assert old_image in config.with_suffix(".env.previous").read_text()
    assert not any(row["args"][-1:] == ["resume"] for row in commands)


def test_release_refuses_a_contended_lock_before_touching_containers(fake_deployment):
    run, config, _, old_image, _ = fake_deployment
    result, commands = run("lock")
    assert result.returncode != 0
    assert commands == []
    assert old_image in config.read_text()
