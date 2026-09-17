"""Run the real shell bootstrap against signed, isolated release fixtures.

Only network/platform discovery and the final native executable are stand-ins;
archive extraction and signature/checksum verification use the real tools.
"""
import errno
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace

import pytest


REPO = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def installer_signing_key(tmp_path_factory):
    if not shutil.which("openssl"):
        pytest.skip("openssl is required to verify connector release fixtures")
    directory = tmp_path_factory.mktemp("installer-signing")
    private = directory / "private.pem"
    public = directory / "public.pem"
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                    "rsa_keygen_bits:2048", "-out", str(private)],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", str(private), "-pubout", "-out", str(public)],
                   check=True, capture_output=True)
    return private, public.read_text()


def executable(path, source):
    path.write_text(f"#!{sys.executable}\n" + source)
    path.chmod(0o700)


@pytest.fixture
def installer(tmp_path, installer_signing_key):
    private, public = installer_signing_key
    user = tmp_path / "user with spaces"
    home = user / "connector with spaces"
    bundle = home / "releases" / "existing-release"
    bundle.mkdir(parents=True)
    (bundle / "keep.txt").write_text("existing immutable release")
    (home / "current").symlink_to(bundle)
    identity = {"config.json": '{"server":"https://control.test","connectorId":"old"}',
                "secrets.json": "private-fixture-credentials", "operations.sqlite3": "deduplication-ledger"}
    for name, contents in identity.items():
        (home / name).write_text(contents)
        (home / name).chmod(0o600)
    service = user / ".config/systemd/user/agent-control-connector.service"
    service.parent.mkdir(parents=True)
    service.write_text("existing service must remain untouched")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    stage = tmp_path / "temporary files"
    stage.mkdir()
    commands = tmp_path / "mock commands"
    commands.mkdir()
    native_log = tmp_path / "native.json"
    curl_log = tmp_path / "curl.jsonl"
    release = tmp_path / "fixture" / "agent-control-connector"
    release.mkdir(parents=True)
    executable(release / "agent-control-connector", """import json,os,sys
from pathlib import Path
Path(os.environ["NATIVE_LOG"]).write_text(json.dumps(sys.argv[1:]))
""")
    checksums = []
    for platform in ("linux", "macos"):
        for architecture in ("arm64", "x86_64"):
            name = f"agent-control-connector-{platform}-{architecture}.tar.gz"
            with tarfile.open(downloads / name, "w:gz") as archive:
                archive.add(release, arcname="agent-control-connector")
            checksums.append(f"{hashlib.sha256((downloads / name).read_bytes()).hexdigest()}  {name}\n")
    (downloads / "SHA256SUMS").write_text("".join(checksums))
    subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(private), "-out",
                    str(downloads / "SHA256SUMS.sig"), str(downloads / "SHA256SUMS")],
                   check=True, capture_output=True)
    (downloads / "VERSION").write_text("verified-release\n")
    executable(commands / "curl", """import json,os,shutil,sys
from pathlib import Path
args=sys.argv[1:]
name=args[-1].rsplit("/",1)[-1]
with Path(os.environ["CURL_LOG"]).open("a") as output:
    output.write(json.dumps(args)+"\\n")
if name == os.environ.get("FAIL_DOWNLOAD"):
    sys.exit(22)
source=Path(os.environ["DOWNLOADS"])/name
if "--output" in args:
    shutil.copyfile(source,args[args.index("--output")+1])
else:
    sys.stdout.write(source.read_text())
""")
    executable(commands / "uname", """import os,sys
print(os.environ.get("FIXTURE_SYSTEM","Linux") if sys.argv[1] == "-s" else os.environ.get("FIXTURE_ARCH","aarch64"))
""")
    script = tmp_path / "install.sh"
    script.write_text((REPO / "deploy/connector/install.sh").read_text().replace(
        "__CONNECTOR_RELEASE_PUBLIC_KEY__", public.rstrip()))
    environment = {**os.environ, "HOME": str(user), "AGENT_CONTROL_CONNECTOR_HOME": str(home),
                   "TMPDIR": str(stage), "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                   "NATIVE_LOG": str(native_log), "CURL_LOG": str(curl_log), "DOWNLOADS": str(downloads)}
    command = ["/bin/sh", str(script), "--server", "https://control.test/"]
    def run(*args, stdin=""):
        # Detaching guarantees this branch cannot inherit pytest's terminal.
        return subprocess.run([*command, *args], env=environment, input=stdin,
                              capture_output=True, text=True, start_new_session=True, timeout=20)
    def unchanged():
        for name, contents in identity.items():
            assert (home / name).read_text() == contents
        assert (home / "current").resolve() == bundle
        assert (bundle / "keep.txt").read_text() == "existing immutable release"
        assert service.read_text() == "existing service must remain untouched"
    return SimpleNamespace(run=run, command=command, env=environment, home=home, stage=stage,
                           downloads=downloads, native_log=native_log, curl_log=curl_log,
                           unchanged=unchanged)


def terminal_run(installer, answer):
    """Give sh a controlling tty and answer only after the prompt appears."""
    pid, terminal = pty.fork()
    if pid == 0:
        os.execve("/bin/sh", installer.command, installer.env)
    output = bytearray()
    answered = False
    reaped = False
    deadline = time.monotonic() + 20
    try:
        while time.monotonic() < deadline:
            if select.select([terminal], [], [], 0.1)[0]:
                try:
                    part = os.read(terminal, 65536)
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    part = b""
                output.extend(part)
                if not answered and b"Reconnect this computer? [y/N]" in output:
                    os.write(terminal, answer.encode() + b"\n")
                    answered = True
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                reaped = True
                assert answered, output.decode(errors="replace")
                return os.waitstatus_to_exitcode(status), output.decode(errors="replace")
        pytest.fail("Installer did not complete its terminal confirmation")
    finally:
        if not reaped:
            os.kill(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(terminal)


def test_existing_pairing_requires_terminal_or_explicit_flag(installer):
    result = installer.run(stdin="yes\n")
    assert result.returncode != 0
    assert "--reconnect" in result.stderr and "No terminal available" in result.stderr
    assert not installer.curl_log.exists() and not installer.native_log.exists()
    installer.unchanged()


@pytest.mark.parametrize("answer", ["", "n", "anything"])
def test_terminal_decline_preserves_installation_without_downloading(installer, answer):
    code, output = terminal_run(installer, answer)
    assert code == 0 and "Cancelled" in output
    assert not installer.curl_log.exists() and not installer.native_log.exists()
    installer.unchanged()


@pytest.mark.parametrize("answer", ["y", "yes", "s", "si"])
def test_terminal_confirmation_downloads_verified_runtime_then_reconnects(installer, answer):
    code, output = terminal_run(installer, answer)
    assert code == 0, output
    args = json.loads(installer.native_log.read_text())
    assert args[0] == "install-service" and "--reconnect" in args
    assert args[args.index("--data-dir") + 1] == str(installer.home)
    installer.unchanged()


@pytest.mark.parametrize(("system", "architecture", "archive"), [
    ("Linux", "aarch64", "linux-arm64"), ("Linux", "x86_64", "linux-x86_64"),
    ("Darwin", "arm64", "macos-arm64"), ("Darwin", "x86_64", "macos-x86_64"),
])
def test_explicit_reconnect_preserves_arguments_and_verifies_platform_release(installer, system, architecture, archive):
    installer.env.update(FIXTURE_SYSTEM=system, FIXTURE_ARCH=architecture)
    token = installer.home / "token file with spaces"
    result = installer.run("--reconnect", "--token-file", str(token))
    assert result.returncode == 0, result.stderr
    args = json.loads(installer.native_log.read_text())
    assert args == ["install-service", "--data-dir", str(installer.home), "--source",
                    args[4], "--release", "verified-release", "--server", "https://control.test",
                    "--token-file", str(token), "--reconnect"]
    downloads = [json.loads(line)[-1] for line in installer.curl_log.read_text().splitlines()]
    assert downloads[-1].endswith(f"agent-control-connector-{archive}.tar.gz")
    assert len(downloads) == 4
    assert not list(installer.stage.iterdir())
    installer.unchanged()


def test_unpaired_existing_release_resumes_without_confirmation_or_reconnect_flag(installer):
    (installer.home / "config.json").unlink()
    result = installer.run()
    assert result.returncode == 0, result.stderr
    args = json.loads(installer.native_log.read_text())
    assert args[0] == "install-service" and "--reconnect" not in args
    assert (installer.home / "current").is_symlink()
    assert (installer.home / "operations.sqlite3").read_text() == "deduplication-ledger"


@pytest.mark.parametrize("failure", ["download", "signature", "checksum"])
def test_failed_release_verification_never_touches_pairing_or_runs_native(installer, failure):
    if failure == "download":
        installer.env["FAIL_DOWNLOAD"] = "agent-control-connector-linux-arm64.tar.gz"
    elif failure == "signature":
        installer.downloads.joinpath("SHA256SUMS.sig").write_bytes(b"invalid-signature")
    else:
        with installer.downloads.joinpath("agent-control-connector-linux-arm64.tar.gz").open("ab") as archive:
            archive.write(b"tampered archive")
    result = installer.run("--reconnect")
    assert result.returncode != 0
    assert not installer.native_log.exists()
    assert not list(installer.stage.iterdir())
    installer.unchanged()
