from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tarfile
import time

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from deploy.managed import build, manifest, prepare_release

REVISION = "a" * 40


@pytest.fixture
def signing_key(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = tmp_path / "signing.pem"
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    private.chmod(0o600)
    monkeypatch.setattr(prepare_release, "PUBLIC_KEY", key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return private


def runtime(path: Path, platform="linux-arm64"):
    for name in ("python/bin/python3", "hermes/pyproject.toml", "hermes/uv.lock", "bin/agent-control-setup", "licenses.json", "connector/engine.py"):
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("test runtime fixture\n")
    (path / "python/bin/python3").chmod(0o755)
    (path / "bin/agent-control-setup").chmod(0o755)
    (path / "build-provenance.json").write_text(json.dumps({"release": REVISION, "platform": platform,
        "hermesSourceSha": manifest.PINS["hermesSourceSha"], "pythonVersion": manifest.PINS["pythonVersion"]}))
    return path


def test_signed_inventory_covers_every_shipped_code_file_and_rejects_stale_signing(tmp_path, signing_key):
    root = runtime(tmp_path / "runtime")
    manifest.create_manifest(root, REVISION, "linux-arm64")
    signature = manifest.sign_manifest(root, signing_key)
    value = json.loads((root / "runtime-manifest.json").read_text())
    assert set(value["files"]) == {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()} - manifest.EXCLUDED
    assert value["hermesSourceSha"] == "939e45c91d751fadd94dcd1b873ac3cb44846213"
    subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(root / "runtime-public-key.pem"),
                    "-signature", str(signature), str(root / "runtime-manifest.json")], check=True, capture_output=True)
    (root / "connector/engine.py").write_text("unexpected code")
    with pytest.raises(ValueError, match="changed after"):
        manifest.sign_manifest(root, signing_key)


@pytest.mark.parametrize("kind", ["link", "fifo"])
def test_inventory_refuses_links_and_special_files(tmp_path, kind):
    import os
    root = runtime(tmp_path / "runtime")
    if kind == "link":
        (root / "unexpected").symlink_to("hermes/uv.lock")
    else:
        os.mkfifo(root / "unexpected")
    with pytest.raises(ValueError, match="link or special"):
        manifest.create_manifest(root, REVISION, "linux-arm64")


def test_manifest_refuses_unrelated_source_revision(tmp_path):
    root = runtime(tmp_path / "runtime")
    provenance = json.loads((root / "build-provenance.json").read_text())
    provenance["hermesSourceSha"] = "b" * 40
    (root / "build-provenance.json").write_text(json.dumps(provenance))
    with pytest.raises(ValueError, match="provenance"):
        manifest.create_manifest(root, REVISION, "linux-arm64")


def test_manifest_rejects_even_an_empty_non_object_extra_catalog(tmp_path):
    root = runtime(tmp_path / "runtime")
    with pytest.raises(ValueError, match="extra catalog"):
        manifest.create_manifest(root, REVISION, "linux-arm64", extras=[])


@pytest.mark.parametrize("field,value", [("platform", "linux-x86_64"), ("url", "https://untrusted.test/browser.tgz"), ("sha256", "invalid"), ("size", True)])
def test_extra_catalog_is_bound_to_platform_release_and_bounded_archive(tmp_path, field, value):
    root = runtime(tmp_path / "runtime")
    descriptor = {"schemaVersion": 1, "id": "browser", "version": "browser-1", "platform": "linux-arm64",
        "url": f"/downloads/agent-control/releases/{REVISION}/agent-control-browser-linux-arm64.tar.gz",
        "sha256": "b" * 64, "size": 1024}
    descriptor[field] = value
    with pytest.raises(ValueError, match="extra descriptor"):
        manifest.create_manifest(root, REVISION, "linux-arm64", extras={"browser": descriptor})


def test_python_materialization_preserves_internal_links_as_regular_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "python3.12").write_bytes(b"native interpreter")
    (source / "python3.12").chmod(0o755)
    (source / "python3").symlink_to("python3.12")
    result = tmp_path / "result"
    build.regular_copy(source, result, source.resolve())
    assert not (result / "python3").is_symlink()
    assert (result / "python3").read_bytes() == (source / "python3.12").read_bytes()
    assert (result / "python3").stat().st_mode & 0o111


@pytest.mark.parametrize("kind", ["escape", "cycle"])
def test_python_materialization_rejects_escaping_and_cyclic_links(tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    (tmp_path / "outside").write_text("not included")
    (source / "bad").symlink_to("../outside" if kind == "escape" else ".")
    with pytest.raises(ValueError, match="escapes|cycle"):
        build.regular_copy(source, tmp_path / "result", source.resolve())


def test_portable_python_download_is_hash_pinned_and_bad_cache_not_trusted(tmp_path, monkeypatch):
    pin = manifest.PINS["python"]["linux-arm64"]
    archive = tmp_path / f"python-linux-arm64-{pin['sha256']}.tar.gz"
    archive.write_bytes(b"tampered cache")
    import io
    monkeypatch.setattr(build.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"tampered network"))
    with pytest.raises(ValueError, match="integrity"):
        build.fetch_python("linux-arm64", tmp_path)
    assert archive.read_bytes() == b"tampered cache"
    assert not archive.with_suffix(".download").exists()


def test_publisher_requires_complete_verified_set_and_keeps_previous_pointer(tmp_path, signing_key, monkeypatch):
    output = tmp_path / "downloads"
    (output / "agent-control").mkdir(parents=True)
    (output / "agent-control/VERSION").write_text("old\n")
    monkeypatch.setattr(prepare_release, "verify_dmg", lambda *a: {})
    with pytest.raises(ValueError, match="Expected one"):
        prepare_release.prepare(tmp_path / "empty", output, REVISION, signing_key, tmp_path / "app.dmg", tmp_path / "receipt.json")
    assert (output / "agent-control/VERSION").read_text() == "old\n"
    assert not (output / "agent-control/releases" / REVISION).exists()


def test_publisher_signs_linux_archives_and_exposes_only_verified_download_urls(tmp_path, signing_key, monkeypatch):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    for target in ("linux-x86_64", "linux-arm64"):
        root = runtime(tmp_path / target, target)
        with tarfile.open(artifacts / f"agent-control-runtime-{target}.tar.gz", "w:gz") as archive:
            archive.add(root, arcname="agent-control-runtime")
    dmg = tmp_path / "app.dmg"
    dmg.write_bytes(b"verified dmg fixture")
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}")
    monkeypatch.setattr(prepare_release, "verify_dmg", lambda *a: {})
    output = tmp_path / "downloads"
    release = prepare_release.prepare(artifacts, output, REVISION, signing_key, dmg, receipt)
    latest = json.loads((output / "agent-control/latest.json").read_text())
    assert latest["version"] == REVISION
    assert latest["downloads"]["linux"]["installerUrl"] == "/downloads/agent-control/install.sh"
    assert latest["downloads"]["macosArm64"]["sha256"] == build.digest(dmg)
    assert "__MANAGED_RELEASE_PUBLIC_KEY__" not in (output / "agent-control/install.sh").read_text()
    with tarfile.open(release / "agent-control-runtime-linux-arm64.tar.gz") as archive:
        archive.extractall(tmp_path / "unpack", filter="data")
    root = tmp_path / "unpack/agent-control-runtime"
    assert json.loads((root / "runtime-manifest.json").read_text())["files"] == manifest.file_inventory(root)
    subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(root / "runtime-public-key.pem"),
                    "-signature", str(release / "SHA256SUMS.sig"), str(release / "SHA256SUMS")], check=True, capture_output=True)
    subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(root / "runtime-public-key.pem"),
                    "-signature", str(output / "agent-control/latest.json.sig"), str(output / "agent-control/latest.json")], check=True, capture_output=True)
    with pytest.raises(ValueError, match="Immutable"):
        prepare_release.prepare(artifacts, output, REVISION, signing_key, dmg, receipt)


def test_prepared_extra_is_copied_without_changing_signed_archive_bytes(tmp_path, monkeypatch):
    from deploy.managed.extras import prepare as extra_prepare
    extras = tmp_path / "extras"
    extras.mkdir()
    archive = extras / "agent-control-browser-linux-arm64.tar.gz"
    archive.write_bytes(b"already signed compressed browser")
    descriptor = {"schemaVersion": 1, "id": "browser", "version": "browser-1", "platform": "linux-arm64",
        "url": f"/downloads/agent-control/releases/{REVISION}/{archive.name}", "sha256": build.digest(archive), "size": archive.stat().st_size}
    archive.with_name(archive.name + ".descriptor.json").write_text(json.dumps(descriptor))
    verified = []
    monkeypatch.setattr(extra_prepare, "verify_prepared_extra", lambda *args: verified.append(args))
    release = tmp_path / "release"
    release.mkdir()
    public = tmp_path / "public.pem"
    catalog = prepare_release.prepared_extras(extras, release, REVISION, public)
    assert catalog == {"linux-arm64": {"browser": descriptor}}
    assert verified == [(archive, descriptor, REVISION, public)]
    assert (release / archive.name).read_bytes() == archive.read_bytes()


def test_prepared_extra_rejects_sidecar_symlink_before_publication(tmp_path):
    extras, release = tmp_path / "extras", tmp_path / "release"
    extras.mkdir()
    release.mkdir()
    archive = extras / "agent-control-browser-linux-arm64.tar.gz"
    archive.write_bytes(b"package")
    metadata = extras / "descriptor.json"
    metadata.write_text("{}")
    archive.with_name(archive.name + ".descriptor.json").symlink_to(metadata)
    with pytest.raises(ValueError, match="bounded descriptor"):
        prepare_release.prepared_extras(extras, release, REVISION, tmp_path / "public.pem")
    assert not list(release.iterdir())


@pytest.mark.parametrize("args", [["--server", "http://unsafe.test"], ["--server", "https://valid.test/path"], ["--server"], ["--data-dir", "relative"]])
def test_bootstrap_invalid_arguments_fail_before_network(args):
    result = subprocess.run(["sh", str(REPO / "deploy/managed/install.sh"), *args], capture_output=True, text=True)
    assert result.returncode == 2


def test_bootstrap_shell_syntax():
    subprocess.run(["sh", "-n", str(REPO / "deploy/managed/install.sh")], check=True)


def signed_bootstrap_fixture(tmp_path: Path, signing_key: Path, architecture: str, *, tamper: str | None = None):
    """Exercise the actual shell, archive extraction and signed runtime verifier.

    Platform detection and downloads are fixtures; tar, OpenSSL and the verifier
    run normally. The tiny Python launcher uses the test interpreter so the same
    archive-permission regression can run on macOS and both Linux architectures.
    """
    target = "linux-" + ("arm64" if architecture == "aarch64" else architecture)
    root = runtime(tmp_path / "runtime", target)
    public_key = subprocess.run(["openssl", "pkey", "-in", str(signing_key), "-pubout"],
                                check=True, capture_output=True).stdout
    for package, module in (("agent_control_connector", "managed_manifest"), ("hermes_client", "compatibility")):
        destination = root / "connector" / package
        destination.mkdir()
        (destination / "__init__.py").write_text("")
        source = REPO / "packages" / ("connector" if package == "agent_control_connector" else "hermes-client")
        shutil.copyfile(source / package / f"{module}.py", destination / f"{module}.py")
    # Replace only the external trust/platform inputs. The production verifier's
    # signature, inventory, size, permission and content checks stay unmodified.
    (root / "python/bin/python3").write_text(f"#!{sys.executable}\n"
        "import sys\nfrom agent_control_connector import managed_manifest\n"
        f"managed_manifest.PUBLIC_KEY = {public_key!r}\n"
        f"managed_manifest.current_platform = lambda: {target!r}\n"
        "position = sys.argv.index('-c')\ncode = sys.argv[position + 1]\n"
        "sys.argv = ['-c', *sys.argv[position + 2:]]\nexec(code)\n")
    # Explicit modes make the signed fixture independent of the test runner's umask.
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() or path.relative_to(root).as_posix() in
                   {"python/bin/python3", "bin/agent-control-setup"} else 0o644)
    manifest.create_manifest(root, REVISION, target)
    manifest.sign_manifest(root, signing_key)
    if tamper == "mode":
        (root / "hermes/uv.lock").chmod(0o640)
    elif tamper == "content":
        (root / "hermes/uv.lock").write_text("TEST runtime fixture\n")  # Same length; exercises the hash check.
    elif tamper == "signature":
        signature = root / "runtime-manifest.json.sig"
        signature.write_bytes(b"\0" * signature.stat().st_size)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    archive = downloads / f"agent-control-runtime-{target}.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(root, arcname="agent-control-runtime")
    checksums = downloads / "SHA256SUMS"
    checksums.write_text(f"{build.digest(archive)}  {archive.name}\n")
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(signing_key), "-out",
                    str(downloads / "SHA256SUMS.sig"), str(checksums)], check=True, capture_output=True)
    (downloads / "VERSION").write_text(REVISION + "\n")
    installer = tmp_path / "install.sh"
    installer.write_text((REPO / "deploy/managed/install.sh").read_text().replace(
        "__MANAGED_RELEASE_PUBLIC_KEY__", public_key.decode().strip()))
    tools = tmp_path / "tools"
    tools.mkdir()
    stubs = {
        "uname": f'#!/bin/sh\ncase "$1" in -s) echo Linux ;; -m) echo {architecture} ;; *) exit 2 ;; esac\n',
        "systemctl": "#!/bin/sh\nexit 0\n",
        "curl": f"#!{sys.executable}\nimport pathlib, shutil, sys\n"
            f"source = pathlib.Path({str(downloads)!r}) / sys.argv[-1].rsplit('/', 1)[-1]\n"
            "if '--output' in sys.argv:\n    shutil.copyfile(source, sys.argv[sys.argv.index('--output') + 1])\n"
            "else:\n    sys.stdout.write(source.read_text())\n",
        "awk": '#!/bin/sh\nfor arg; do last=$arg; done\nif [ "$last" = /etc/os-release ]; then\n'
            'case "$*" in *VERSION_ID*) echo 24.04 ;; *) echo ubuntu ;; esac\nelse\n'
            f'exec {shlex.quote(shutil.which("awk"))} "$@"\nfi\n',
    }
    if sys.platform != "linux":
        # Only Linux-specific host preflight/locking needs substitutes on macOS.
        stubs["stat"] = f"#!{sys.executable}\nimport os, sys\nassert sys.argv[1:3] == ['-c', '%u']\nprint(os.stat(sys.argv[3]).st_uid)\n"
        stubs["flock"] = "#!/bin/sh\nexit 0\n"
    for name, content in stubs.items():
        utility = tools / name
        utility.write_text(content)
        utility.chmod(0o755)
    return installer, {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"]}


@pytest.mark.skipif(os.getuid() == 0, reason="Non-root extraction reproduces the user's umask filtering")
@pytest.mark.parametrize("architecture", ["x86_64", "aarch64"])
@pytest.mark.parametrize("initial_umask", [0o022, 0o077])
def test_bootstrap_preserves_signed_modes_under_restrictive_umask(tmp_path, signing_key, architecture, initial_umask):
    installer, env = signed_bootstrap_fixture(tmp_path, signing_key, architecture)
    managed = tmp_path / "managed"
    result = subprocess.run(["sh", str(installer), "--data-dir", str(managed)], env=env,
                            capture_output=True, text=True, timeout=20, start_new_session=True, umask=initial_umask)
    # A headless bootstrap must reach the interactive setup handoff, without
    # starting services, asking for credentials, or pairing the test machine.
    assert result.returncode == 2 and "Runtime verified." in result.stdout, result.stderr
    assert "Open an interactive terminal and run:" in result.stderr
    release = managed / "releases" / REVISION
    expected = json.loads((release / "runtime-manifest.json").read_text())["files"]
    assert manifest.file_inventory(release) == expected
    assert (release / "hermes/uv.lock").stat().st_mode & 0o777 == 0o644
    assert (release / "python/bin/python3").stat().st_mode & 0o777 == 0o755
    assert managed.stat().st_mode & 0o777 == 0o700
    assert (managed / "releases").stat().st_mode & 0o777 == 0o700
    assert (managed / ".install.lock").stat().st_mode & 0o777 == 0o600
    assert not list(managed.glob(".download.*"))
    assert not (managed / "current").exists()


@pytest.mark.skipif(os.getuid() == 0, reason="Non-root extraction reproduces the user's umask filtering")
@pytest.mark.parametrize("tamper, error", [("mode", "Runtime file metadata changed"),
    ("content", "Runtime integrity check failed"), ("signature", "Release signature is not from Agent Control")])
def test_bootstrap_still_rejects_tampered_signed_runtime(tmp_path, signing_key, tamper, error):
    installer, env = signed_bootstrap_fixture(tmp_path, signing_key, "x86_64", tamper=tamper)
    managed = tmp_path / "managed"
    result = subprocess.run(["sh", str(installer), "--data-dir", str(managed)], env=env,
                            capture_output=True, text=True, timeout=20, start_new_session=True, umask=0o077)
    assert result.returncode != 0 and error in result.stderr
    assert "Runtime verified." not in result.stdout
    assert not (managed / "releases" / REVISION).exists()
    assert not list(managed.glob(".download.*"))


@pytest.mark.skipif(sys.platform != "linux" or os.getuid() == 0 or shutil.which("flock") is None,
                    reason="Exercises the real supported Linux kernel lock as a regular user")
def test_bootstrap_rejects_concurrent_install_and_recovers_after_sigkill(tmp_path, signing_key):
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "systemctl").write_text("#!/bin/sh\nexit 0\n")
    (tools / "curl").write_text('#!/bin/sh\nprintf "started\\n" >> "$PROBE_LOG"\n'
        'if [ "$PROBE_ACTION" = fail ]; then exit 22; fi\nwhile :; do sleep 1; done\n')
    for utility in tools.iterdir():
        utility.chmod(0o755)
    public_key = subprocess.run(["openssl", "pkey", "-in", str(signing_key), "-pubout"],
                                check=True, capture_output=True, text=True).stdout.strip()
    installer = tmp_path / "install.sh"
    installer.write_text((REPO / "deploy/managed/install.sh").read_text().replace("__MANAGED_RELEASE_PUBLIC_KEY__", public_key))
    managed = tmp_path / "managed"
    log = tmp_path / "downloads-started"
    env = {**os.environ, "PATH": str(tools) + os.pathsep + os.environ["PATH"],
           "PROBE_LOG": str(log), "PROBE_ACTION": "wait"}
    command = ["sh", str(installer), "--data-dir", str(managed)]
    first = subprocess.Popen(command, env=env, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 10
        while not log.exists():
            if first.poll() is not None:
                pytest.fail("Installer failed before reaching the locked download: " + first.stderr.read().decode())
            if time.monotonic() >= deadline:
                pytest.fail("Installer did not reach its first download")
            time.sleep(0.05)
        second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=10)
        assert second.returncode == 2 and "Another installer is running" in second.stderr
        assert log.read_text().splitlines() == ["started"]
        os.killpg(first.pid, signal.SIGKILL)
        first.wait(timeout=5)
        deadline = time.monotonic() + 5
        while subprocess.run(["flock", "-n", str(managed / ".install.lock"), "true"], capture_output=True).returncode:
            assert time.monotonic() < deadline, "Killed installer retained its kernel lock"
            time.sleep(0.05)
        retry = subprocess.run(command, env={**env, "PROBE_ACTION": "fail"}, capture_output=True, text=True, timeout=10)
        assert retry.returncode == 22, retry.stderr
        assert log.read_text().splitlines() == ["started", "started"]
        assert (managed / ".install.lock").stat().st_mode & 0o777 == 0o600
    finally:
        if first.poll() is None:
            os.killpg(first.pid, signal.SIGKILL)
            first.wait(timeout=5)
        first.stdout.close()
        first.stderr.close()
