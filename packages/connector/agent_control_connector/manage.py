"""Per-user immutable connector installation and conservative lifecycle changes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager

LABEL = "com.agent-control.connector"
RELEASE_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,100}\Z")


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, check=check, capture_output=True, text=True)


def private_dir(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise ValueError("Connector directory must be a real directory")
    if path.exists() and path.stat().st_uid != os.getuid():
        raise ValueError("Connector directory must belong to this user")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)



@contextmanager
def management_lock(home: Path):
    fd = os.open(home / "management.lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Another connector lifecycle operation is running") from None
        yield



@contextmanager
def stopped_runtime(home: Path):
    """Hold the runtime lock while changing a stopped connector's identity."""
    fd = os.open(home / "runtime.lock", os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError("Connector is still running; local identity was preserved") from None
        yield


def runtime_running(home: Path) -> bool:
    try:
        with stopped_runtime(home):
            return False
    except ValueError:
        return True

def validate_service_target() -> None:
    target = service_path()
    if target.is_symlink():
        raise ValueError("Refusing to replace a symlinked service")
    if target.exists():
        if not target.is_file() or target.stat().st_size > 16384:
            raise ValueError("An unrelated service occupies the connector label")
        content = target.read_text()
        if sys.platform == "darwin":
            try:
                if plistlib.loads(target.read_bytes()).get("Label") != LABEL:
                    raise ValueError("An unrelated service occupies the connector label")
            except (plistlib.InvalidFileException, AttributeError):
                raise ValueError("An unrelated service occupies the connector label") from None
        elif not content.startswith("# Managed by agent-control-connector\n"):
            raise ValueError("An unrelated service occupies the connector label")


def validate_release(release: Path, version: str) -> None:
    metadata = release / "release.json"
    binary = release / "agent-control-connector"
    if metadata.is_symlink() or not metadata.is_file() or metadata.stat().st_size > 16384:
        raise ValueError("Release metadata missing or invalid")
    value = json.loads(metadata.read_text())
    if not isinstance(value, dict) or value.get("revision") != version or value.get("protocol") != 1:
        raise ValueError("Release identity does not match the requested version")
    expected_system = "Darwin" if sys.platform == "darwin" else "Linux"
    machine = platform.machine()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else machine
    actual_architecture = value.get("architecture")
    actual_architecture = "arm64" if actual_architecture in {"arm64", "aarch64"} else actual_architecture
    if value.get("system") != expected_system or actual_architecture != architecture:
        raise ValueError("Release platform does not match this computer")
    if binary.is_symlink() or not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError("Release executable missing or invalid")

def atomic_link(home: Path, name: str, destination: Path) -> None:
    temporary = home / ("." + name + ".new")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(destination)
    temporary.replace(home / name)


def service_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/LaunchAgents" / (LABEL + ".plist")
    return Path.home() / ".config/systemd/user/agent-control-connector.service"


def write_service(home: Path) -> None:
    binary = home / "current/agent-control-connector"
    target = service_path()
    validate_service_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        target.write_bytes(plistlib.dumps({
            "Label": LABEL,
            "ProgramArguments": [str(binary), "run", "--data-dir", str(home)],
            "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
            "EnvironmentVariables": {"AGENT_CONTROL_CONNECTOR_HOME": str(home)},
            "StandardOutPath": str(home / "service.log"),
            "StandardErrorPath": str(home / "service.log"),
        }))
    else:
        # systemd quoted words require escaping both specifiers and backslashes.
        def word(value: str) -> str:
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
        target.write_text(
            "# Managed by agent-control-connector\n[Unit]\nDescription=Agent Control connector\n"
            "After=network-online.target\n[Service]\nType=simple\n"
            f"ExecStart={word(str(binary))} run --data-dir {word(str(home))}\n"
            "Restart=on-failure\nRestartSec=10\nUMask=0077\nNoNewPrivileges=true\n"
            "[Install]\nWantedBy=default.target\n"
        )
    target.chmod(0o600)


def service(action: str) -> None:
    if sys.platform == "darwin":
        domain = f"gui/{os.getuid()}"
        if action == "stop":
            run("launchctl", "bootout", domain, str(service_path()), check=False)
        else:
            run("launchctl", "bootstrap", domain, str(service_path()))
    else:
        run("systemctl", "--user", "daemon-reload")
        if action == "start":
            run("systemctl", "--user", "enable", "--now", "agent-control-connector.service")
        else:
            run("systemctl", "--user", "stop", "agent-control-connector.service")


def status(home: Path) -> dict:
    path = home / "status.json"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 16_384:
        raise ValueError("Connector status is unavailable; leave the running service unchanged")
    value = json.loads(path.read_text())
    observed = datetime.fromisoformat(value["observedAt"].replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - observed).total_seconds()
    if not 0 <= age <= 45 or not value.get("fresh"):
        raise ValueError("Connector status is stale; leave the running service unchanged")
    return value


def drain(home: Path) -> None:
    request = home / "maintenance.request"
    if request.is_symlink():
        raise ValueError("Invalid maintenance marker")
    requested_at = datetime.now(timezone.utc)
    request.write_text(requested_at.isoformat())
    request.chmod(0o600)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            value = status(home)
            observed = datetime.fromisoformat(value["observedAt"].replace("Z", "+00:00"))
            if (value.get("maintenance") is True and observed >= requested_at
                and value.get("maintenanceRequestId") == requested_at.isoformat()):
                if value.get("activeWork") is not False:
                    raise ValueError("Hermes has active or uncertain work; try again when idle")
                return
            time.sleep(0.25)
        raise ValueError("Connector did not acknowledge maintenance; no restart performed")
    except Exception:
        request.unlink(missing_ok=True)
        raise


def archive_platform() -> str:
    system = "macos" if sys.platform == "darwin" else "linux"
    machine = platform.machine()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else machine
    if architecture not in {"x86_64", "arm64"}:
        raise ValueError("Unsupported architecture")
    return f"agent-control-connector-{system}-{architecture}.tar.gz"


def download(url: str, destination: Path, maximum: int) -> None:
    origin = urllib.parse.urlsplit(url)
    if origin.scheme != "https" or origin.username or origin.password:
        raise ValueError("HTTPS is required for connector releases")
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise ValueError("Release redirects are not allowed")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    with opener.open(url, timeout=60) as response, destination.open("xb") as output:
        total = 0
        while chunk := response.read(262_144):
            total += len(chunk)
            if total > maximum:
                raise ValueError("Release exceeds the download limit")
            output.write(chunk)


def verified_release(home: Path, stage: Path, version: str) -> Path:
    if not RELEASE_ID.fullmatch(version):
        raise ValueError("Invalid release identifier")
    config = json.loads((home / "config.json").read_text())
    server = config["server"].rstrip("/")
    base = f"{server}/downloads/connector/releases/{version}"
    archive = archive_platform()
    for name, maximum in (("SHA256SUMS", 16_384), ("SHA256SUMS.sig", 8_192), (archive, 300_000_000)):
        download(f"{base}/{name}", stage / name, maximum)
    run("openssl", "dgst", "-sha256", "-verify", str(home / "current/release-public-key.pem"),
        "-signature", str(stage / "SHA256SUMS.sig"), str(stage / "SHA256SUMS"))
    checksums = {}
    for line in (stage / "SHA256SUMS").read_text().splitlines():
        checksum, name = line.split()
        if name in checksums:
            raise ValueError("Duplicate release checksum")
        checksums[name] = checksum
    with (stage / archive).open("rb") as source:
        actual = hashlib.file_digest(source, "sha256").hexdigest()
    if checksums.get(archive) != actual:
        raise ValueError("Release checksum failed")
    unpack = stage / "unpacked"
    unpack.mkdir()
    with tarfile.open(stage / archive) as bundle:
        total = 0
        for member in bundle.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] != "agent-control-connector":
                raise ValueError("Unsafe release archive path")
            if not member.isfile() and not member.isdir():
                raise ValueError("Release archive contains unsupported links or special files")
            total += member.size
            if total > 800_000_000:
                raise ValueError("Unpacked release is too large")
        bundle.extractall(unpack, filter="data")
    release = unpack / "agent-control-connector"
    validate_release(release, version)
    return release



def restore_service(home: Path) -> None:
    current = (home / "current").resolve()
    if current.parent != (home / "releases").resolve() or not RELEASE_ID.fullmatch(current.name):
        raise ValueError("Current release is not an installed connector release")
    validate_release(current, current.name)
    if not (home / "config.json").is_file():
        raise ValueError("Run connect before installing the service")
    if runtime_running(home):
        raise ValueError("Connector is already running")
    link = Path.home() / ".local/bin/agent-control-connector"
    if (link.exists() or link.is_symlink()) and (not link.is_symlink() or link.resolve() != (current / "agent-control-connector").resolve()):
        raise ValueError("An unrelated executable occupies the connector command")
    write_service(home)
    if sys.platform != "darwin":
        run("loginctl", "enable-linger", getpass.getuser())
    service("start")
    link.parent.mkdir(parents=True, exist_ok=True)
    if not link.is_symlink():
        link.symlink_to(home / "current/agent-control-connector")

def install(home: Path, source: Path, release: str, server: str) -> None:
    if not RELEASE_ID.fullmatch(release):
        raise ValueError("Invalid release identifier")
    if (home / "current").exists() or (home / "current").is_symlink():
        if (home / "config.json").exists() or runtime_running(home):
            raise ValueError("Already installed; use update or install-service to restore its service")
        current = (home / "current").resolve()
        if current.parent != (home / "releases").resolve():
            raise ValueError("Current release is outside installed releases")
        validate_release(current, current.name)
        result = subprocess.run([str(current / "agent-control-connector"), "connect", "--server", server, "--data-dir", str(home)])
        if result.returncode:
            raise ValueError("Pairing failed; existing release was preserved")
        restore_service(home)
        return
    validate_release(source, release)
    validate_service_target()
    bindir = Path.home() / ".local/bin"
    link = bindir / "agent-control-connector"
    expected_link = home / "current/agent-control-connector"
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != expected_link.resolve():
            raise ValueError("An unrelated executable already occupies ~/.local/bin/agent-control-connector")
    releases = home / "releases"
    private_dir(releases)
    destination = releases / release
    if destination.exists():
        raise ValueError("This release is already staged; inspect the previous install failure")
    shutil.copytree(source, destination, symlinks=False)
    atomic_link(home, "current", destination)
    binary = destination / "agent-control-connector"
    # Pair interactively before registering any background service.
    result = subprocess.run([str(binary), "connect", "--server", server, "--data-dir", str(home)])
    if result.returncode:
        (home / "current").unlink()
        raise ValueError("Pairing failed; staged release retained for diagnosis")
    write_service(home)
    if sys.platform != "darwin":
        run("loginctl", "enable-linger", getpass.getuser())
    service("start")
    bindir.mkdir(parents=True, exist_ok=True)
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != binary.resolve():
            raise ValueError("An unrelated executable already occupies ~/.local/bin/agent-control-connector")
    else:
        link.symlink_to(home / "current/agent-control-connector")
    print(f"Connector installed. Command: {link}. Hermes remains managed separately.")


def switch(home: Path, destination: Path) -> None:
    private_dir(home / "releases")
    releases = (home / "releases").resolve()
    destination = destination.resolve()
    if destination.parent != releases or not RELEASE_ID.fullmatch(destination.name):
        raise ValueError("Rollback target is not an installed release")
    validate_release(destination, destination.name)
    previous = (home / "current").resolve()
    if previous.parent != releases:
        raise ValueError("Current release is outside the installed releases")
    validate_release(previous, previous.name)
    drain(home)
    try:
        service("stop")
        atomic_link(home, "previous", previous)
        atomic_link(home, "current", destination)
        (home / "maintenance.request").unlink(missing_ok=True)
        (home / "status.json").unlink(missing_ok=True)
        service("start")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                if status(home).get("connected") is True:
                    print("Connector release activated and connection verified.")
                    return
            except (ValueError, KeyError, FileNotFoundError):
                pass
            time.sleep(0.5)
        raise ValueError("Updated connector did not become ready")
    except Exception:
        service("stop")
        atomic_link(home, "current", previous)
        (home / "maintenance.request").unlink(missing_ok=True)
        service("start")
        raise
    finally:
        (home / "maintenance.request").unlink(missing_ok=True)


def management_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install-service", "update", "rollback", "uninstall"])
    parser.add_argument("--data-dir", default=os.environ.get("AGENT_CONTROL_CONNECTOR_HOME", str(Path.home() / ".agent-control-connector")))
    parser.add_argument("--source")
    parser.add_argument("--release")
    parser.add_argument("--server")
    parser.add_argument("--forget", action="store_true", help="Remove local pairing credentials after uninstall; deduplication ledger is retained")
    args = parser.parse_args(argv)
    try:
        home = Path(args.data_dir).expanduser().absolute()
        if any(character in str(home) for character in ("\n", "\r", "\0")):
            raise ValueError("Invalid data directory")
        private_dir(home)
        with management_lock(home):
            return execute_management(args, home)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(str(exc) if isinstance(exc, ValueError) else "Connector lifecycle operation failed; configuration was preserved.", file=sys.stderr)
        return 1


def execute_management(args, home: Path) -> int:
    try:
        if args.command != "uninstall" and args.forget:
            raise ValueError("--forget is only supported with uninstall")
        if args.command == "install-service":
            if not any((args.source, args.release, args.server)):
                restore_service(home)
            else:
                if not args.source or not args.release or not args.server:
                    raise ValueError("Install requires source, release and server")
                install(home, Path(args.source), args.release, args.server)
        elif args.command == "update":
            private_dir(home / "releases")
            if not args.release:
                raise ValueError("Specify the release to install with --release")
            destination = home / "releases" / args.release
            if destination.exists():
                raise ValueError("Release already exists; use rollback for an installed release")
            with tempfile.TemporaryDirectory(dir=home, prefix=".download-") as temporary:
                source = verified_release(home, Path(temporary), args.release)
                shutil.move(str(source), destination)
            switch(home, destination)
        elif args.command == "rollback":
            if args.release and not RELEASE_ID.fullmatch(args.release):
                raise ValueError("Invalid release identifier")
            destination = home / "releases" / args.release if args.release else home / "previous"
            switch(home, destination)
        else:
            validate_service_target()
            if runtime_running(home):
                drain(home)
            try:
                if service_path().exists():
                    service("stop")
                    if sys.platform != "darwin":
                        run("systemctl", "--user", "disable", "agent-control-connector.service")
                    service_path().unlink(missing_ok=True)
                with stopped_runtime(home):
                    link = Path.home() / ".local/bin/agent-control-connector"
                    if link.is_symlink() and link.resolve() == (home / "current/agent-control-connector").resolve():
                        link.unlink()
                    if args.forget:
                        from .storage import SecretStore
                        SecretStore(home).delete()
                        for name in ("config.json", "status.json"):
                            (home / name).unlink(missing_ok=True)
            finally:
                (home / "maintenance.request").unlink(missing_ok=True)
            if args.forget:
                print("Service and local pairing removed. Revoke the computer in Agent Control. Run the installer again to reconnect; Hermes and the operation ledger were preserved.")
            else:
                print("Service removed; pairing and Hermes data preserved. Run install-service to restore it, or uninstall --forget to remove the local identity.")

        return 0
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.SubprocessError) as exc:
        # Subprocess output may contain local paths; never echo captured output.
        print(str(exc) if isinstance(exc, ValueError) else "Connector lifecycle operation failed; configuration was preserved.", file=sys.stderr)
        return 1
