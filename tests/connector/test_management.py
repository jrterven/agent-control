from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timedelta, timezone

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "packages/connector"))
sys.path.insert(0, str(REPO / "packages/hermes-client"))
from agent_control_connector import manage


def bundle(path: Path, version="r1", *, system=None, architecture=None):
    path.mkdir(parents=True)
    binary = path / "agent-control-connector"
    binary.write_text('#!/bin/sh\nif [ "$1" = "--help" ]; then echo "{connect,check-credentials,run,status}"; fi\nexit 0\n')
    binary.chmod(0o755)
    (path / "release.json").write_text(json.dumps({"revision":version, "protocol":1,
        "system":system or platform.system(), "architecture":architecture or platform.machine()}))
    return path


@pytest.fixture
def signing_key(tmp_path):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives import serialization
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = tmp_path / "private.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    private.chmod(0o600)
    public = tmp_path / "public.pem"
    public.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, public


def release_download(tmp_path, signing_key, monkeypatch, *, version="r2", with_link=False):
    home = tmp_path / "home"
    bundle(home / "current")
    shutil.copyfile(signing_key[1], home / "current/release-public-key.pem")
    (home / "config.json").write_text(json.dumps({"server":"https://control.example.test"}))
    source = bundle(tmp_path / "source", version)
    if with_link:
        (source / "internal-link").symlink_to("release.json")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    archive_name = manage.archive_platform()
    archive_path = downloads / archive_name
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source, arcname="agent-control-connector")
    checksum = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    sums = downloads / "SHA256SUMS"
    sums.write_text(f"{checksum}  {archive_name}\n")
    subprocess.run(["openssl","dgst","-sha256","-sign",str(signing_key[0]),"-out",str(downloads / "SHA256SUMS.sig"),str(sums)], check=True)
    monkeypatch.setattr(manage, "download", lambda url, target, maximum: shutil.copyfile(downloads / url.rsplit("/",1)[-1], target))
    stage = tmp_path / "stage"
    stage.mkdir()
    return home, stage, downloads


def test_release_signature_and_platform_bound_to_requested_version(tmp_path, signing_key, monkeypatch):
    home, stage, _ = release_download(tmp_path, signing_key, monkeypatch)
    result = manage.verified_release(home, stage, "r2")
    assert (result / "agent-control-connector").is_file()
    assert json.loads((result / "release.json").read_text())["revision"] == "r2"


def test_tampered_release_checksums_rejected(tmp_path, signing_key, monkeypatch):
    home, stage, downloads = release_download(tmp_path, signing_key, monkeypatch)
    (downloads / "SHA256SUMS").write_text("0" * 64 + "  forged.tar.gz\n")
    with pytest.raises(subprocess.CalledProcessError):
        manage.verified_release(home, stage, "r2")
    assert not (stage / "unpacked").exists()


def test_valid_signature_cannot_substitute_different_release(tmp_path, signing_key, monkeypatch):
    home, stage, _ = release_download(tmp_path, signing_key, monkeypatch, version="r1")
    with pytest.raises(ValueError, match="identity"):
        manage.verified_release(home, stage, "r2")


def test_update_archives_reject_even_internal_links(tmp_path, signing_key, monkeypatch):
    # Publisher materializes native framework links first; installers never
    # need symlink support in the signed format.
    home, stage, _ = release_download(tmp_path, signing_key, monkeypatch, with_link=True)
    with pytest.raises(ValueError, match="links"):
        manage.verified_release(home, stage, "r2")


def test_install_conflicting_executable_has_no_pairing_or_service_side_effect(tmp_path, monkeypatch):
    fake_home = tmp_path / "user"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    command = fake_home / ".local/bin/agent-control-connector"
    command.parent.mkdir(parents=True)
    command.write_text("unrelated application")
    source = bundle(tmp_path / "source")
    connector_home = tmp_path / "connector"
    connector_home.mkdir()
    monkeypatch.setattr(manage.subprocess, "run", lambda *a, **k: pytest.fail("must fail before executing any process"))
    with pytest.raises(ValueError, match="unrelated executable"):
        manage.install(connector_home, source, "r1", "https://control.example.test")
    assert not (connector_home / "current").exists()
    assert command.read_text() == "unrelated application"


def test_drain_rejects_active_or_uncertain_work_and_cleans_marker(tmp_path, monkeypatch):
    def snapshot(home):
        return {"maintenance":True, "maintenanceRequestId":(home / "maintenance.request").read_text(),
                "observedAt":datetime.now(timezone.utc).isoformat(), "activeWork":None}
    monkeypatch.setattr(manage, "status", snapshot)
    with pytest.raises(ValueError, match="active or uncertain"):
        manage.drain(tmp_path)
    assert not (tmp_path / "maintenance.request").exists()


def test_drain_waits_for_exact_request_and_new_idle_scan(tmp_path, monkeypatch):
    calls = []
    def snapshot(home):
        calls.append(True)
        return {"maintenance":True, "maintenanceRequestId":("old-request" if len(calls) == 1 else (home / "maintenance.request").read_text()),
                "observedAt":datetime.now(timezone.utc).isoformat(), "activeWork":False}
    monkeypatch.setattr(manage, "status", snapshot)
    monkeypatch.setattr(manage.time, "sleep", lambda _: None)
    manage.drain(tmp_path)
    assert len(calls) == 2
    assert (tmp_path / "maintenance.request").exists()


def test_switch_rolls_back_atomically_when_new_service_fails(tmp_path, monkeypatch):
    releases = tmp_path / "releases"
    old = bundle(releases / "r1", "r1")
    new = bundle(releases / "r2", "r2")
    (tmp_path / "current").symlink_to(old)
    (tmp_path / "config.json").write_text('{"paired":true}')
    monkeypatch.setattr(manage, "drain", lambda home: None)
    actions = []
    def service(action):
        actions.append((action, (tmp_path / "current").resolve().name))
        if action == "start" and (tmp_path / "current").resolve() == new:
            raise ValueError("new release failed")
    monkeypatch.setattr(manage, "service", service)
    with pytest.raises(ValueError, match="new release failed"):
        manage.switch(tmp_path, new)
    assert (tmp_path / "current").resolve() == old
    assert actions == [("stop","r1"),("start","r2"),("stop","r2"),("start","r1")]
    assert (tmp_path / "config.json").read_text() == '{"paired":true}'


@pytest.mark.parametrize("failure", ["denied", "timeout"])
def test_macos_preflight_failure_never_drains_or_stops_working_service(tmp_path, monkeypatch, capsys, failure):
    monkeypatch.setattr(manage.sys, "platform", "darwin")
    old = bundle(tmp_path / "releases/r1", "r1", system="Darwin")
    new = bundle(tmp_path / "releases/r2", "r2", system="Darwin")
    (tmp_path / "current").symlink_to(old)
    calls = []
    def invoke(args, **kwargs):
        calls.append((args, kwargs))
        if args[-1] == "--help":
            return subprocess.CompletedProcess(args, 0, "{run,check-credentials}", "")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 300, output="secret-placeholder")
        return subprocess.CompletedProcess(args, 1, "secret-placeholder", "secret-placeholder")
    monkeypatch.setattr(manage.subprocess, "run", invoke)
    monkeypatch.setattr(manage, "drain", lambda _: pytest.fail("preflight must finish before requesting maintenance"))
    monkeypatch.setattr(manage, "service", lambda _: pytest.fail("working service must stay running"))
    with pytest.raises(ValueError, match="left unchanged") as error:
        manage.switch(tmp_path, new)
    assert calls[1][0] == [str(new / "agent-control-connector"), "check-credentials", "--data-dir", str(tmp_path)]
    assert calls[1][1]["timeout"] == 300
    assert "secret-placeholder" not in str(error.value) + capsys.readouterr().out
    assert (tmp_path / "current").resolve() == old
    assert not (tmp_path / "previous").exists()
    assert not (tmp_path / "maintenance.request").exists()


def test_macos_preflight_completes_before_maintenance(tmp_path, monkeypatch):
    monkeypatch.setattr(manage.sys, "platform", "darwin")
    old = bundle(tmp_path / "releases/r1", "r1", system="Darwin")
    new = bundle(tmp_path / "releases/r2", "r2", system="Darwin")
    (tmp_path / "current").symlink_to(old)
    actions = []
    def invoke(args, **kwargs):
        actions.append(args[1])
        assert (tmp_path / "current").resolve() == old
        return subprocess.CompletedProcess(args, 0, "{check-credentials,run}", "")
    monkeypatch.setattr(manage.subprocess, "run", invoke)
    monkeypatch.setattr(manage, "drain", lambda _: actions.append("drain"))
    monkeypatch.setattr(manage, "service", actions.append)
    monkeypatch.setattr(manage, "wait_for_connection", lambda *args, **kwargs: actions.append("ready"))
    manage.switch(tmp_path, new)
    assert actions == ["--help", "check-credentials", "drain", "stop", "start", "ready"]
    assert (tmp_path / "current").resolve() == new


def test_macos_legacy_preflight_is_only_permitted_for_explicit_rollback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(manage.sys, "platform", "darwin")
    calls = []
    def invoke(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "{connect,run,status,doctor}", "")
    monkeypatch.setattr(manage.subprocess, "run", invoke)
    with pytest.raises(ValueError, match="cannot check Keychain"):
        manage.credential_preflight(tmp_path, tmp_path / "legacy")
    manage.credential_preflight(tmp_path, tmp_path / "legacy", allow_legacy=True)
    assert "older rollback release" in capsys.readouterr().out
    assert all(args[-1] == "--help" for args in calls)


def test_failed_help_is_not_treated_as_legacy_rollback(tmp_path, monkeypatch):
    monkeypatch.setattr(manage.sys, "platform", "darwin")
    monkeypatch.setattr(manage.subprocess, "run", lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", ""))
    with pytest.raises(ValueError, match="left unchanged"):
        manage.credential_preflight(tmp_path, tmp_path / "legacy", allow_legacy=True)


def test_startup_requires_connection_observed_after_start(tmp_path, monkeypatch):
    started = datetime.now(timezone.utc)
    observations = [
        {"connected": True, "observedAt": (started - timedelta(seconds=1)).isoformat()},
        {"connected": False, "observedAt": started.isoformat()},
        {"connected": True, "observedAt": started.isoformat()},
    ]
    monkeypatch.setattr(manage, "status", lambda _: observations.pop(0))
    monkeypatch.setattr(manage.time, "sleep", lambda _: None)
    manage.wait_for_connection(tmp_path, started)
    assert observations == []


def test_install_does_not_report_success_before_service_connects(tmp_path, monkeypatch, capsys):
    fake_user = tmp_path / "user"
    fake_user.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_user))
    source = bundle(tmp_path / "source")
    home = tmp_path / "connector"
    home.mkdir()
    monkeypatch.setattr(manage, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(manage, "service", lambda _: None)
    monkeypatch.setattr(manage, "status", lambda _: {"connected": False, "observedAt": datetime.now(timezone.utc).isoformat()})
    ticks = iter([0, 0, 61])
    monkeypatch.setattr(manage.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(manage.time, "sleep", lambda _: None)
    with pytest.raises(ValueError, match="connection was not verified"):
        manage.install(home, source, "r1", "https://control.test")
    assert "installed and connection verified" not in capsys.readouterr().out


def test_failed_pairing_can_retry_same_verified_release_with_token_file(tmp_path, monkeypatch):
    fake_user = tmp_path / "user"
    fake_user.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_user))
    source = bundle(tmp_path / "source")
    home = tmp_path / "connector"
    home.mkdir()
    commands = []
    def pair(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 1 if len(commands) == 1 else 0)
    monkeypatch.setattr(manage.subprocess, "run", pair)
    monkeypatch.setattr(manage, "run", lambda *a, **k: None)
    services = []
    monkeypatch.setattr(manage, "service", services.append)
    monkeypatch.setattr(manage, "wait_for_connection", lambda *a, **k: None)
    with pytest.raises(ValueError, match="Pairing failed"):
        manage.install(home, source, "r1", "https://control.test")
    assert not (home / "current").exists()
    assert not manage.service_path().exists()
    assert services == []
    assert (home / "releases/r1").is_dir()
    token_file = str(tmp_path / "private token.txt")
    result = manage.management_main(["install-service", "--data-dir", str(home), "--source", str(source),
        "--release", "r1", "--server", "https://control.test", "--token-file", token_file])
    assert result == 0
    assert commands[-1][-2:] == ["--token-file", token_file]
    assert (home / "current").resolve() == home / "releases/r1"
    assert services == ["start"]


@pytest.mark.parametrize("change", ["contents", "extra", "mode", "symlink"])
def test_pairing_retry_rejects_staged_files_different_from_verified_download(tmp_path, monkeypatch, change):
    fake_user = tmp_path / "user"
    fake_user.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_user))
    source = bundle(tmp_path / "source")
    (source / "library").write_text("verified bytes")
    home = tmp_path / "connector"
    staged = home / "releases/r1"
    shutil.copytree(source, staged)
    if change == "contents":
        (staged / "library").write_text("changed")
    elif change == "extra":
        (staged / "extra").write_text("unexpected")
    elif change == "mode":
        (staged / "library").chmod(0o755)
    else:
        (staged / "library").unlink()
        (staged / "library").symlink_to(source / "library")
    monkeypatch.setattr(manage.subprocess, "run", lambda *a, **k: pytest.fail("Cannot execute a mismatched staged release"))
    with pytest.raises(ValueError, match="differs|unsupported link"):
        manage.install(home, source, "r1", "https://control.test")
    assert not (home / "current").exists()
    assert not manage.service_path().exists()


def test_management_lock_prevents_overlapping_updates(tmp_path):
    with manage.management_lock(tmp_path):
        with pytest.raises(ValueError, match="Another"):
            with manage.management_lock(tmp_path):
                pytest.fail("second management process acquired lock")


def test_prepare_release_materializes_native_links_and_publishes_signed_set(tmp_path, signing_key):
    spec = importlib.util.spec_from_file_location("connector_prepare_release", REPO / "deploy/connector/prepare_release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for target in module.PLATFORMS:
        system, arch = target.split("-",1)
        source = bundle(tmp_path / target, "r2", system={"linux":"Linux","macos":"Darwin"}[system], architecture=arch)
        (source / "lib-real").write_text("framework")
        (source / "lib-link").symlink_to("lib-real")
        with tarfile.open(artifacts / f"agent-control-connector-{target}.tar.gz", "w:gz") as archive:
            archive.add(source, arcname="agent-control-connector")
    output = tmp_path / "output"
    approved = []
    def apple_signer(bundle_path, platform):
        assert (bundle_path / "release-public-key.pem").is_file()
        approved.append(platform)
    module.prepare(artifacts, output, "r2", signing_key[0], apple_signer=apple_signer)
    assert approved == ["macos-x86_64", "macos-arm64"]
    release = output / "connector/releases/r2"
    assert (output / "connector/VERSION").read_text() == "r2\n"
    assert "__CONNECTOR_RELEASE_PUBLIC_KEY__" not in (output / "connector/install.sh").read_text()
    for archive_path in release.glob("*.tar.gz"):
        with tarfile.open(archive_path) as archive:
            assert all(member.isfile() or member.isdir() for member in archive.getmembers())
            assert archive.extractfile("agent-control-connector/lib-link").read() == b"framework"
    subprocess.run(["openssl","dgst","-sha256","-verify",str(signing_key[1]),"-signature",str(release / "SHA256SUMS.sig"),str(release / "SHA256SUMS")],check=True)
    with pytest.raises(ValueError, match="Immutable"):
        module.prepare(artifacts, output, "r2", signing_key[0], apple_signer=apple_signer)
    from deploy.connector.macos_signing import NotarizationPending
    def pending(bundle_path, platform):
        raise NotarizationPending(platform)
    unpublished = tmp_path / "pending"
    with pytest.raises(NotarizationPending):
        module.prepare(artifacts, unpublished, "r2", signing_key[0], apple_signer=pending)
    assert not (unpublished / "connector/VERSION").exists()
    assert not (unpublished / "connector/releases/r2").exists()


@pytest.mark.parametrize("args", [["--server"], ["--token-file"], ["--server","https://control.test/path"], ["--server","https://control.test\nunsafe"]])
def test_installer_rejects_invalid_origin_before_network(args):
    result = subprocess.run(["sh",str(REPO / "deploy/connector/install.sh"),*args], capture_output=True, text=True)
    assert result.returncode == 2
