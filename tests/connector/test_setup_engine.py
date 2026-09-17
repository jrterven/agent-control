import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import tarfile
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from agent_control_connector import managed_manifest as manifest
from agent_control_connector import setup_engine as setup
from agent_control_connector import setup_service as service
from agent_control_connector.storage import atomic_json, read_json
from hermes_client.compatibility import HERMES_0212_SHA


@pytest.fixture
def signed_runtime(tmp_path, monkeypatch):
    root = tmp_path / "runtime"
    (root / "python/bin").mkdir(parents=True)
    (root / "hermes").mkdir()
    (root / "python/bin/python3").write_text("executable")
    (root / "python/bin/python3").chmod(0o755)
    (root / "hermes/main.py").write_text("audited source")
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(manifest, "PUBLIC_KEY", key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    data = {"schemaVersion": 1, "platform": manifest.current_platform(), "release": "a" * 40,
            "hermesSourceSha": HERMES_0212_SHA, "hermesVersion": "0.21.2", "files": {}}
    for path in root.rglob("*"):
        if path.is_file():
            data["files"][path.relative_to(root).as_posix()] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "size": path.stat().st_size, "mode": path.stat().st_mode & 0o777}
    document = json.dumps(data).encode()
    (root / "runtime-manifest.json").write_bytes(document)
    (root / "runtime-manifest.json.sig").write_bytes(key.sign(document, padding.PKCS1v15(), hashes.SHA256()))
    return root


def test_runtime_trusts_pinned_publisher_not_adjacent_key(signed_runtime):
    (signed_runtime / "runtime-public-key.pem").write_text("untrusted key supplied with archive")
    assert manifest.verify_runtime(signed_runtime)["hermesSourceSha"] == HERMES_0212_SHA
    raw = (signed_runtime / "runtime-manifest.json").read_bytes().replace(b"0.21.2", b"0.21.3")
    (signed_runtime / "runtime-manifest.json").write_bytes(raw)
    with pytest.raises(ValueError, match="signature"):
        manifest.verify_runtime(signed_runtime)


@pytest.mark.parametrize("change", ["bytes", "extra", "link", "mode"])
def test_packaged_runtime_fails_closed_on_tampering(signed_runtime, change):
    file = signed_runtime / "hermes/main.py"
    if change == "bytes":
        file.write_text("malicious code!")
    elif change == "extra":
        (signed_runtime / "hermes/sitecustomize.py").write_text("unexpected")
    elif change == "link":
        file.unlink()
        file.symlink_to("/etc/passwd")
    else:
        file.chmod(0o777)
    with pytest.raises(ValueError):
        manifest.verify_runtime(signed_runtime)


def test_revision_detection_accepts_verified_bundle_without_git(signed_runtime, monkeypatch):
    from agent_control_connector.cli import detect_revision
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    revision, source = detect_revision(signed_runtime / "data", str(signed_runtime / "hermes"))
    assert revision == HERMES_0212_SHA
    assert source == signed_runtime / "hermes"


@pytest.fixture
def engine(tmp_path, monkeypatch):
    secrets = {}
    class MemorySecrets:
        def __init__(self, path):
            self.path = str(path)
        def save(self, value):
            secrets[self.path] = value
        def load(self):
            return secrets[self.path]
    monkeypatch.setattr(setup, "SecretStore", MemorySecrets)
    instance = setup.SetupEngine(tmp_path / "runtime", tmp_path / "managed", "https://control.test", tmp_path / "connector")
    instance.state = {"mode": "managed", "server": instance.server, "hermesHome": str(tmp_path / "owned-hermes"),
        "hermesSource": str(tmp_path / "runtime/hermes"), "sourceSha": HERMES_0212_SHA, "hermesVersion": "0.21.2",
        "restUrl": "http://127.0.0.1:19119", "releaseRoot": str(instance.root), "providerReady": False}
    instance.save()
    MemorySecrets(instance.directory / "credentials").save({"hermesToken": "local-secret"})
    monkeypatch.setattr(instance, "local", lambda *args, **kwargs: {})
    return instance


def test_existing_pairing_never_overwritten_or_reinstalled(engine, monkeypatch):
    atomic_json(engine.connector_dir / "config.json", {"connectorId": "existing-owner"})
    monkeypatch.setattr(setup, "verify_runtime", lambda *_: pytest.fail("must not stage another runtime"))
    result = engine.install(mode="managed")
    assert result["alreadyPaired"]
    assert read_json(engine.connector_dir / "config.json")["connectorId"] == "existing-owner"


def test_api_key_validation_precedes_local_write_and_confirms_catalog_model(engine, monkeypatch):
    writes = []
    monkeypatch.setattr(setup, "validate_key", lambda provider, key: writes.append(("validated", provider)))
    def local(method, path, **kwargs):
        writes.append((method, path))
        if path == "/api/model/options":
            return {"providers": [{"slug": "openai", "models": ["available-model"]}]}
        if path == "/api/model/recommended-default":
            return {"model": "available-model"}
        return {}
    monkeypatch.setattr(engine, "local", local)
    result = engine.configure_provider("openai", apiKey="private-key")
    assert writes[0] == ("validated", "openai")
    assert not result["providerReady"]
    with pytest.raises(ValueError, match="catálogo"):
        engine.configure_provider("openai", model="unavailable-model")
    assert ("POST", "/api/model/set") not in writes
    assert engine.configure_provider("openai", model="available-model")["providerReady"]


def test_existing_hermes_provider_configuration_is_not_modified(engine):
    engine.state["mode"] = "existing"
    with pytest.raises(ValueError, match="existente"):
        engine.configure_provider("openai", apiKey="private-key")


def test_pairing_resumes_same_unexpired_code_and_never_sends_local_secret(engine, monkeypatch):
    engine.state["providerReady"] = True
    async def profiles():
        return ["default", "chemistry"]
    monkeypatch.setattr(engine, "profiles", profiles)
    requests = []
    def cloud(path, body):
        requests.append((path, body))
        if path.endswith("authorize"):
            return httpx.Response(200, json={"deviceCode": "private-pair-secret", "userCode": "ABCD-EFGH", "expiresIn": 600})
        return httpx.Response(200, json={"accessToken": "private-cloud-secret", "connectorId": "device", "gatewayId": "gateway", "profiles": ["chemistry"]})
    monkeypatch.setattr(engine, "cloud", cloud)
    first = engine.pair_start()
    assert engine.pair_start() == first
    assert len(requests) == 1
    assert "local-secret" not in json.dumps(requests)
    assert "private-pair-secret" not in json.dumps(first)
    result = engine.pair_poll(first["flowId"])
    assert result["status"] == "complete"
    assert read_json(engine.connector_dir / "config.json")["profiles"] == ["chemistry"]
    assert not (engine.directory / "pairing.json").exists()
    assert engine.pair_poll(first["flowId"])["status"] == "complete"
    assert len(requests) == 2


def test_reject_foreign_approved_profile(engine, monkeypatch):
    engine.state["providerReady"] = True
    async def profiles():
        return ["default"]
    monkeypatch.setattr(engine, "profiles", profiles)
    def cloud(path, body):
        return httpx.Response(200, json={"deviceCode": "secret", "userCode": "ABCD-EFGH", "expiresIn": 600} if path.endswith("authorize") else
            {"accessToken": "secret", "connectorId": "device", "gatewayId": "gateway", "profiles": ["foreign"]})
    monkeypatch.setattr(engine, "cloud", cloud)
    flow = engine.pair_start()
    with pytest.raises(ValueError, match="selección"):
        engine.pair_poll(flow["flowId"])
    assert not (engine.connector_dir / "config.json").exists()


def test_rpc_does_not_expose_exception_payload_or_accept_arbitrary_commands(engine, monkeypatch):
    def explode(**kwargs):
        raise RuntimeError("provider api-key=never-print-me")
    monkeypatch.setattr(engine, "diagnose", explode)
    output = io.StringIO()
    setup.rpc(engine, io.StringIO('{"id":1,"method":"diagnose"}\n{"id":2,"method":"exec","params":{"command":"touch /tmp/unsafe"}}\n'), output)
    data = [json.loads(line) for line in output.getvalue().splitlines()]
    assert all("error" in row for row in data)
    assert "never-print-me" not in output.getvalue()


def test_runtime_environment_does_not_import_other_account_keys(engine, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "other-account")
    monkeypatch.setenv("PYTHONPATH", "/malicious")
    monkeypatch.setenv("HERMES_LAZY_INSTALL_TARGET", "/writable-lazy")
    env = service.runtime_environment(engine)
    assert "OPENAI_API_KEY" not in env and "HERMES_LAZY_INSTALL_TARGET" not in env
    assert "/malicious" not in env["PYTHONPATH"]
    assert env["HERMES_DISABLE_LAZY_INSTALLS"] == "1"
    assert env["HERMES_HOME"] == engine.state["hermesHome"]


@pytest.mark.parametrize("kind", ["traversal", "symlink", "device"])
def test_archive_rejects_unsafe_members(tmp_path, kind):
    archive = tmp_path / "payload.tar.gz"
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("../outside" if kind == "traversal" else "file")
        member.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.CHRTYPE if kind == "device" else tarfile.REGTYPE
        output.addfile(member)
    with pytest.raises(ValueError):
        service.extract_verified_archive(archive, tmp_path / "output")


def test_uncertain_work_prevents_lifecycle_stop(engine, monkeypatch):
    atomic_json(engine.connector_dir / "config.json", {"connectorId": "paired"})
    monkeypatch.setattr(service, "drain", lambda *_: (_ for _ in ()).throw(ValueError("active or uncertain")))
    monkeypatch.setattr(service, "systemctl", lambda *_: pytest.fail("must not stop service"))
    with pytest.raises(ValueError, match="uncertain"):
        service.lifecycle(engine, "uninstall", {})
