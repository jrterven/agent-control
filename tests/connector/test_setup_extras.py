from __future__ import annotations

from contextlib import contextmanager
import copy
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent_control_connector import managed_manifest, setup_extras, setup_service
from deploy.managed.extras import prepare

RELEASE = "a" * 40


@pytest.fixture
def signed_extra(tmp_path, monkeypatch):
    monkeypatch.setattr(setup_extras, "current_platform", lambda: "linux-arm64")
    private = tmp_path / "private.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private)], check=True, capture_output=True)
    private.chmod(0o600)
    public = subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout"], check=True, capture_output=True).stdout
    monkeypatch.setattr(managed_manifest, "PUBLIC_KEY", public)
    root = tmp_path / "browser-extra"
    files = {}
    for name in ("node/bin/node", "bin/agent-browser", "chromium/chrome"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 99\n")  # Installing must not execute these files.
        path.chmod(0o755)
        files[name] = {"sha256": setup_extras.digest(path), "size": path.stat().st_size, "mode": 0o755}
    manifest = {"schemaVersion": 1, "id": "browser", "release": RELEASE, "platform": "linux-arm64",
                "version": setup_extras.EXTRA_VERSION, "components": setup_extras.COMPONENTS,
                "pythonAbi": None, "certification": {"native": True, "offlineBrowser": True}, "files": files,
                "entrypoints": {"node": "node/bin/node", "agentBrowser": "bin/agent-browser", "chromium": "chromium/chrome"}}
    def sign(value=manifest):
        document = root / "extra-manifest.json"
        document.write_text(json.dumps(value))
        subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private), "-out", str(root / "extra-manifest.json.sig"), str(document)], check=True, capture_output=True)
    sign()
    archive = tmp_path / "browser.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(root, arcname="browser-extra")
    descriptor = {"schemaVersion": 1, "id": "browser", "version": setup_extras.EXTRA_VERSION, "platform": "linux-arm64",
                  "url": f"/downloads/agent-control/releases/{RELEASE}/agent-control-browser-linux-arm64.tar.gz",
                  "sha256": setup_extras.digest(archive), "size": archive.stat().st_size}
    return SimpleNamespace(root=root, manifest=manifest, archive=archive, descriptor=descriptor, sign=sign, public=public, private=private)


@pytest.fixture
def engine(tmp_path, signed_extra, monkeypatch):
    directory = tmp_path / "managed"
    home = directory / "hermes-home"
    home.mkdir(parents=True)
    config = {"tools": {"enabled_toolsets": ["terminal", "file", "cronjob"]}, "platform_toolsets": {"cli": ["terminal", "file", "cronjob"]}, "model": {"default": "keep-model"}}
    (home / "config.yaml").write_text(json.dumps(config))
    value = SimpleNamespace(directory=directory, root=tmp_path / "runtime", server="https://agentcontrol.example",
                            state={"mode": "managed", "hermesHome": str(home)}, save=Mock())
    monkeypatch.setattr(setup_extras, "verify_runtime", lambda *_: {"release": RELEASE, "extras": {"browser": signed_extra.descriptor}})
    monkeypatch.setattr(setup_service, "fetch", lambda _url, target, _max: shutil.copyfile(signed_extra.archive, target))
    monkeypatch.setattr(setup_extras, "missing_linux_libraries", lambda *_: [])
    monkeypatch.setattr(setup_extras, "probe_browser", lambda *_: None)
    @contextmanager
    def idle(_engine):
        yield
    monkeypatch.setattr(setup_service, "idle_installation", idle)
    return value


def test_explicit_install_verifies_then_enables_toolsets_without_service_restart(engine, signed_extra):
    result = setup_extras.install_extra(engine, {"id": "browser"})
    assert result["status"] == "installed" and result["restartRequired"] is True
    config = json.loads((Path(engine.state["hermesHome"]) / "config.yaml").read_text())
    assert config["model"] == {"default": "keep-model"}
    assert config["tools"]["enabled_toolsets"] == ["terminal", "file", "cronjob", "browser"]
    assert config["platform_toolsets"]["cli"][-1] == "browser"
    env = setup_extras.browser_environment(engine, "/base/python/bin:/usr/bin")
    assert env["AGENT_BROWSER_EXECUTABLE_PATH"].endswith("/chromium/chrome")
    assert env["PATH"].endswith("/base/python/bin:/usr/bin")
    assert engine.state["extras"]["browser"]["sha256"] == signed_extra.descriptor["sha256"]
    assert engine.state["extras"]["browser"]["restartRequired"] is True


def test_unpublished_or_existing_installation_cannot_resolve_upstream(engine, monkeypatch):
    fetch = Mock()
    monkeypatch.setattr(setup_service, "fetch", fetch)
    monkeypatch.setattr(setup_extras, "verify_runtime", lambda *_: {"release": RELEASE})
    with pytest.raises(ValueError, match="todavía no está publicado"):
        setup_extras.install_extra(engine, {"id": "browser"})
    engine.state["mode"] = "existing"
    with pytest.raises(ValueError, match="instalación original"):
        setup_extras.install_extra(engine, {"id": "browser"})
    fetch.assert_not_called()


@pytest.mark.parametrize("change", [{"platform": "linux-x86_64"}, {"url": "https://untrusted.example/browser.tar.gz"}, {"size": 2_000_000_000}, {"version": "latest"}])
def test_descriptor_rejects_wrong_platform_unpinned_or_unbounded_download(signed_extra, change):
    with pytest.raises(ValueError):
        setup_extras.descriptor({"release": RELEASE, "extras": {"browser": {**signed_extra.descriptor, **change}}})


def test_download_hash_failure_never_enables_browser(engine, monkeypatch):
    monkeypatch.setattr(setup_service, "fetch", lambda _url, target, _max: target.write_bytes(b"different"))
    with pytest.raises(ValueError, match="no coincide"):
        setup_extras.install_extra(engine, {"id": "browser"})
    assert "extras" not in engine.state
    engine.save.assert_not_called()


def test_signature_inventory_and_native_certification_are_required(signed_extra):
    manifest = copy.deepcopy(signed_extra.manifest)
    manifest["certification"]["native"] = False
    signed_extra.sign(manifest)
    with pytest.raises(ValueError, match="certificado"):
        setup_extras.verify_extra(signed_extra.root)
    signed_extra.sign()
    (signed_extra.root / "chromium/chrome").write_text("replaced")
    with pytest.raises(ValueError, match="integridad"):
        setup_extras.verify_extra(signed_extra.root)


def test_wrong_release_and_unsigned_metadata_are_rejected(signed_extra):
    with pytest.raises(ValueError, match="certificado"):
        setup_extras.verify_extra(signed_extra.root, expected_release="b" * 40)
    (signed_extra.root / "extra-manifest.json").write_text(json.dumps({**signed_extra.manifest, "release": "b" * 40}))
    with pytest.raises(ValueError, match="signature"):
        setup_extras.verify_extra(signed_extra.root)


@pytest.mark.parametrize("name,kind", [("browser-extra/../../escape", "file"), ("browser-extra/link", "symlink"), ("/outside", "file"), ("browser-extra/fifo", "fifo")])
def test_archive_rejects_traversal_and_special_files(tmp_path, name, kind):
    archive = tmp_path / "unsafe.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        member = tarfile.TarInfo(name)
        if kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/tmp/outside"
        elif kind == "fifo":
            member.type = tarfile.FIFOTYPE
        bundle.addfile(member)
    with pytest.raises(ValueError):
        setup_extras.extract_extra(archive, tmp_path / "extracted")


def test_missing_system_libraries_explains_admin_action_without_modifying_config(engine, monkeypatch):
    monkeypatch.setattr(setup_extras, "missing_linux_libraries", lambda *_: ["libnss3.so"])
    result = setup_extras.install_extra(engine, {"id": "browser"})
    assert result["status"] == "dependencies-required"
    assert result["missingLibraries"] == ["libnss3.so"]
    assert "no ejecutará sudo" in result["message"]
    assert "extras" not in engine.state
    engine.save.assert_not_called()


def test_active_work_and_state_write_failure_keep_existing_configuration(engine, monkeypatch):
    config = Path(engine.state["hermesHome"]) / "config.yaml"
    original = config.read_bytes()
    @contextmanager
    def busy(_):
        raise ValueError("active work")
        yield
    monkeypatch.setattr(setup_service, "idle_installation", busy)
    with pytest.raises(ValueError, match="active work"):
        setup_extras.install_extra(engine, {"id": "browser"})
    assert config.read_bytes() == original
    @contextmanager
    def idle(_):
        yield
    monkeypatch.setattr(setup_service, "idle_installation", idle)
    engine.save.side_effect = OSError("disk full")
    with pytest.raises(OSError):
        setup_extras.install_extra(engine, {"id": "browser"})
    assert config.read_bytes() == original and "extras" not in engine.state


def test_environment_refuses_modified_installed_files(engine):
    setup_extras.install_extra(engine, {"id": "browser"})
    installed = Path(engine.state["extras"]["browser"]["root"])
    (installed / "unexpected-code.py").write_text("unexpected")
    with pytest.raises(ValueError, match="inventario"):
        setup_extras.browser_environment(engine, "/usr/bin")


@pytest.mark.parametrize("extras", [[], "browser", None])
def test_malformed_catalog_fails_with_diagnostic(extras):
    with pytest.raises(ValueError, match="catálogo"):
        setup_extras.descriptor({"release": RELEASE, "extras": extras})


def test_sandbox_failure_never_activates_tools(engine, monkeypatch):
    config = Path(engine.state["hermesHome"]) / "config.yaml"
    before = config.read_bytes()
    monkeypatch.setattr(setup_extras, "probe_browser", lambda *_: {"status": "sandbox-blocked", "id": "browser"})
    assert setup_extras.install_extra(engine, {"id": "browser"})["status"] == "sandbox-blocked"
    assert config.read_bytes() == before and "extras" not in engine.state
    engine.save.assert_not_called()


@pytest.mark.parametrize("output,error,status", [("<html></html>", "", None), ("", "No usable sandbox!", "sandbox-blocked"), ("", "unknown failure", "diagnostic-failed")])
def test_explicit_diagnostic_uses_only_temporary_profile_and_cleans_own_group(signed_extra, monkeypatch, output, error, status):
    process = Mock(pid=4321, returncode=0 if output else 1)
    process.communicate.return_value = (output, error)
    popen = Mock(return_value=process)
    kill = Mock()
    monkeypatch.setattr(setup_extras.subprocess, "Popen", popen)
    monkeypatch.setattr(setup_extras.os, "killpg", kill)
    result = setup_extras.probe_browser(signed_extra.root, signed_extra.manifest)
    assert (result["status"] if result else None) == status
    arguments, options = popen.call_args
    assert arguments[0][-1] == "about:blank"
    assert "--proxy-server=http://127.0.0.1:9" in arguments[0]
    assert "--no-sandbox" not in arguments[0]
    assert options["start_new_session"] is True
    assert not Path(options["env"]["HOME"]).exists()
    kill.assert_called_once_with(process.pid, setup_extras.signal.SIGKILL)


def test_diagnostic_timeout_cleans_only_its_process_group(signed_extra, monkeypatch):
    process = Mock(pid=4321)
    process.communicate.side_effect = [subprocess.TimeoutExpired("chrome", 30), ("", "")]
    monkeypatch.setattr(setup_extras.subprocess, "Popen", Mock(return_value=process))
    kill = Mock()
    monkeypatch.setattr(setup_extras.os, "killpg", kill)
    assert setup_extras.probe_browser(signed_extra.root, signed_extra.manifest)["status"] == "diagnostic-failed"
    kill.assert_called_once_with(process.pid, setup_extras.signal.SIGKILL)


def test_preparation_and_publisher_verify_signed_archive_on_another_host(signed_extra, tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "PUBLIC_KEY", signed_extra.public)
    output = tmp_path / "prepared"
    entry = prepare.prepare_extra(signed_extra.archive, output, RELEASE, signed_extra.private)
    archive = output / "agent-control-browser-linux-arm64.tar.gz"
    public = tmp_path / "trusted.pem"
    public.write_bytes(signed_extra.public)
    assert json.loads(Path(str(archive) + ".descriptor.json").read_text()) == entry
    monkeypatch.setattr(setup_extras, "current_platform", lambda: "macos-arm64")
    assert prepare.verify_prepared_extra(archive, entry, RELEASE, public)["platform"] == "linux-arm64"
    with pytest.raises(ValueError, match="Immutable"):
        prepare.prepare_extra(signed_extra.archive, output, RELEASE, signed_extra.private)
    archive.write_bytes(archive.read_bytes() + b"modified")
    with pytest.raises(ValueError, match="catalog"):
        prepare.verify_prepared_extra(archive, entry, RELEASE, public)


def test_preparation_rejects_uncertified_and_mac_archives(signed_extra, tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "PUBLIC_KEY", signed_extra.public)
    for platform, certified, message in (("linux-arm64", False, "certificado"), ("macos-arm64", True, "Mac browser")):
        value = {**signed_extra.manifest, "platform": platform, "certification": {"native": True, "offlineBrowser": certified}}
        signed_extra.sign(value)
        with tarfile.open(signed_extra.archive, "w:gz") as archive:
            archive.add(signed_extra.root, arcname="browser-extra")
        with pytest.raises(ValueError, match=message):
            prepare.prepare_extra(signed_extra.archive, tmp_path / "prepared", RELEASE, signed_extra.private)


def test_extra_survives_update_and_rollback_using_its_signed_origin(engine, signed_extra, tmp_path, monkeypatch):
    setup_extras.install_extra(engine, {"id": "browser"})
    source = engine.root
    target = tmp_path / "next-runtime"
    source_manifest = {"release": RELEASE, "platform": "linux-arm64", "hermesSourceSha": "c" * 40,
                       "extras": {"browser": signed_extra.descriptor}}
    target_manifest = {"release": "b" * 40, "platform": "linux-arm64", "hermesSourceSha": "c" * 40}
    def verify(root):
        return source_manifest if root == source else target_manifest
    monkeypatch.setattr(setup_extras, "verify_runtime", verify)
    old_state = copy.deepcopy(engine.state)
    carried = setup_extras.validate_extra_transition(engine, target)
    assert engine.state == old_state
    assert carried["browser"]["releaseRoot"] == str(source)
    engine.state.update(releaseRoot=str(target), extras=carried)
    assert setup_extras.browser_environment(engine, "/bin")["AGENT_BROWSER_EXECUTABLE_PATH"].endswith("chromium/chrome")
    assert setup_extras.validate_extra_transition(engine, source) == carried
    target_manifest["hermesSourceSha"] = "d" * 40
    with pytest.raises(ValueError, match="compatibilidad"):
        setup_extras.validate_extra_transition(engine, target)
    with pytest.raises(ValueError, match="compatibilidad"):
        setup_extras.browser_environment(engine, "/bin")


def test_transition_rejects_changed_origin_and_unverified_extra(engine, signed_extra, tmp_path, monkeypatch):
    setup_extras.install_extra(engine, {"id": "browser"})
    saved = copy.deepcopy(engine.state)
    monkeypatch.setattr(setup_extras, "verify_runtime", lambda _: {"release": "b" * 40, "extras": {"browser": signed_extra.descriptor}})
    with pytest.raises(ValueError, match="origen"):
        setup_extras.validate_extra_transition(engine, tmp_path / "target")
    assert engine.state == saved
    monkeypatch.setattr(setup_extras, "verify_runtime", lambda _: {"release": RELEASE, "extras": {"browser": signed_extra.descriptor}})
    (Path(saved["extras"]["browser"]["root"]) / "chromium/chrome").write_text("changed")
    with pytest.raises(ValueError, match="integridad"):
        setup_extras.validate_extra_transition(engine, tmp_path / "target")
    assert engine.state == saved


def test_transition_preserves_verified_relocated_origin_and_no_extra_is_noop(engine, tmp_path, monkeypatch):
    assert setup_extras.validate_extra_transition(engine, tmp_path / "unused") == {}
    setup_extras.install_extra(engine, {"id": "browser"})
    saved = copy.deepcopy(engine.state)
    relocated = tmp_path / "immutable-origin"
    result = setup_extras.validate_extra_transition(engine, tmp_path / "target", origin_root=relocated)
    assert result["browser"]["releaseRoot"] == str(relocated)
    assert engine.state == saved
