from __future__ import annotations

import errno
import os
from pathlib import Path
import pty
import select
import subprocess
import sys
import time
from types import SimpleNamespace
import warnings

import pytest

from agent_control_connector import cli


@pytest.fixture
def token_context(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.delenv("HERMES_DASHBOARD_SESSION_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_CONTROL_HERMES_DASHBOARD_TOKEN", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return SimpleNamespace(token_file=None), tmp_path / ".hermes"


@pytest.mark.parametrize("location", ["control-services", "preview"])
def test_linux_managed_hermes_token_is_discovered_without_prompt(token_context, monkeypatch, location):
    args, home = token_context
    service_env = (home / "control-services/hermes-serve.env" if location == "control-services"
                   else home.parent / ".config/hermes-control-preview/hermes-serve.env")
    service_env.parent.mkdir(parents=True)
    home.mkdir(exist_ok=True)
    token = "test-token-" + "x" * 32
    service_env.write_text(f'HERMES_DASHBOARD_SESSION_TOKEN="{token}"\nHERMES_TUI_WS_ORPHAN_REAP_GRACE_S=300\n')
    service_env.chmod(0o600)
    # The managed service's explicit environment wins over an old .env value.
    (home / ".env").write_text("HERMES_DASHBOARD_SESSION_TOKEN=" + "y" * 40)
    (home / ".env").chmod(0o600)
    monkeypatch.setattr(cli.getpass, "getpass", lambda *a, **k: pytest.fail("Token was already configured"))
    assert cli.hermes_token(args, home) == token


@pytest.mark.parametrize("violation", ["permissions", "symlink", "owner", "size", "fifo"])
def test_explicit_token_file_rejects_unsafe_sources(token_context, monkeypatch, violation):
    args, home = token_context
    path = home.parent / "token"
    path.write_text("x" * 40)
    path.chmod(0o600)
    if violation == "permissions":
        path.chmod(0o640)
    elif violation == "symlink":
        link = path.with_name("link")
        link.symlink_to(path)
        path = link
    elif violation == "owner":
        real_fstat = cli.os.fstat
        def wrong_owner(fd):
            value = real_fstat(fd)
            return SimpleNamespace(st_uid=os.getuid() + 10000, st_mode=value.st_mode, st_size=value.st_size)
        monkeypatch.setattr(cli.os, "fstat", wrong_owner)
    elif violation == "size":
        path.write_text("x" * 1025)
    else:
        path.unlink()
        os.mkfifo(path, 0o600)
    args.token_file = str(path)
    with pytest.raises((OSError, ValueError)):
        cli.hermes_token(args, home)


def test_world_readable_managed_token_is_not_imported(token_context, monkeypatch):
    args, home = token_context
    service_env = home / "control-services/hermes-serve.env"
    service_env.parent.mkdir(parents=True)
    service_env.write_text("HERMES_DASHBOARD_SESSION_TOKEN=" + "x" * 40)
    service_env.chmod(0o644)
    def no_terminal(*args, **kwargs):
        raise OSError("no controlling terminal")
    monkeypatch.setattr(cli, "open", no_terminal, raising=False)
    with pytest.raises(ValueError, match="private Hermes dashboard token"):
        cli.hermes_token(args, home)


@pytest.mark.parametrize("failure", ["echo", "eof"])
def test_unavailable_hidden_input_fails_without_reading_or_echoing_stdin(token_context, monkeypatch, failure):
    args, home = token_context
    import io
    terminal = io.StringIO()
    modes = []
    def terminal_open(path, mode):
        assert path == "/dev/tty"
        modes.append(mode)
        return terminal
    def prompt(*args, **kwargs):
        if failure == "echo":
            warnings.warn("Cannot control echo on the terminal.", cli.getpass.GetPassWarning)
            pytest.fail("Must never use getpass's echoed input fallback")
        raise EOFError
    monkeypatch.setattr(cli, "open", terminal_open, raising=False)
    monkeypatch.setattr(cli.getpass, "getpass", prompt)
    with pytest.raises(ValueError, match="--token-file PATH"):
        cli.hermes_token(args, home)
    assert modes == ["w"]


def test_hidden_prompt_works_with_piped_stdin_and_a_real_controlling_terminal(tmp_path):
    """Exercise the curl | sh condition; old open('/dev/tty', 'r+') fails here."""
    master, slave = pty.openpty()
    token = "pty-test-token-" + "x" * 32
    script = r'''
import fcntl, os, signal, sys, termios
from pathlib import Path
from types import SimpleNamespace
from agent_control_connector import cli
os.setsid()
tty = os.open(sys.argv[1], os.O_RDWR)
fcntl.ioctl(tty, termios.TIOCSCTTY, 0)
signal.signal(signal.SIGHUP, signal.SIG_IGN)
cli.sys.platform = "linux"
Path.home = classmethod(lambda cls: Path(sys.argv[2]))
value = cli.hermes_token(SimpleNamespace(token_file=None), Path(sys.argv[2]) / ".hermes")
assert value == "pty-test-token-" + "x" * 32
print("HIDDEN_TOKEN_ACCEPTED", flush=True)
'''
    env = os.environ.copy()
    for key in ("HERMES_DASHBOARD_SESSION_TOKEN", "HERMES_CONTROL_HERMES_DASHBOARD_TOKEN"):
        env.pop(key, None)
    root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join(str(root / item) for item in ("packages/connector", "packages/hermes-client"))
    process = None
    captured = b""
    try:
        process = subprocess.Popen([sys.executable, "-c", script, os.ttyname(slave), str(tmp_path)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        os.close(slave)
        slave = None
        deadline = time.monotonic() + 15
        while b"Hermes dashboard token" not in captured and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                captured += os.read(master, 4096)
            if process.poll() is not None:
                pytest.fail("Prompt process exited before asking: " + process.stderr.read().decode())
        assert b"Hermes dashboard token" in captured, "The secure prompt never appeared"
        os.write(master, (token + "\n").encode())
        process.stdin.write(b"installer script, not a credential\n")
        process.stdin.close()
        process.stdin = None
        assert select.select([process.stdout], [], [], 15)[0], "Prompt did not accept hidden input"
        stdout = process.stdout.readline()
        while select.select([master], [], [], 0)[0]:
            try:
                captured += os.read(master, 4096)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
        # macOS can wait for the PTY master during session teardown. Close it
        # after collecting the transcript, before waiting for the process.
        os.close(master)
        master = None
        remaining, stderr = process.communicate(timeout=15)
        stdout += remaining
        assert process.returncode == 0, stderr.decode()
        assert b"HIDDEN_TOKEN_ACCEPTED" in stdout
        assert token.encode() not in captured + stdout + stderr
        assert b"Warning" not in captured + stdout + stderr
    finally:
        if master is not None:
            os.close(master)
        if slave is not None:
            os.close(slave)
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
