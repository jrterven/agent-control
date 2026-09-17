"""Validate the generated user unit without registering or starting host services."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch


def verify(root: Path, work: Path) -> None:
    if sys.platform != "linux" or not shutil.which("systemd-analyze"):
        raise ValueError("Run this verification on the native Linux systemd CI runner")
    from agent_control_connector import setup_service as service
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="systemd-contract-", dir=work) as temporary:
        temporary = Path(temporary)
        home = temporary / "home"
        runtime_dir = temporary / "user-runtime"
        managed = home / "managed path with spaces %"
        for path in (home, runtime_dir, managed):
            path.mkdir(parents=True, mode=0o700)
        unit = home / ".config/systemd/user" / service.UNIT
        engine = SimpleNamespace(root=root, directory=managed, server="https://control.example",
                                 state={"releaseRoot": str(root)})
        # Exercise the production renderer while making every service-manager
        # action an explicit test boundary; no user bus or lingering is changed.
        calls = []
        def systemctl(*args, **_):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0)
        def loginctl(args, **_):
            if args[:2] != ["loginctl", "show-user"]:
                raise AssertionError("Unexpected OS mutation in unit rendering")
            return subprocess.CompletedProcess(args, 0, "yes\n", "")
        with patch.object(service, "service_file", lambda: unit), patch.object(service, "systemctl", systemctl), \
             patch.object(service.subprocess, "run", loginctl):
            service.install_linux_service(engine)
        if calls != [("daemon-reload",), ("enable", "--now", service.UNIT)]:
            raise AssertionError("Unexpected unit registration contract")
        if unit.stat().st_mode & 0o777 != 0o600:
            raise AssertionError("Generated service unit is not private")
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LC_ALL": "C",
               "XDG_CONFIG_HOME": str(home / ".config"), "XDG_RUNTIME_DIR": str(runtime_dir),
               "SYSTEMD_LOG_LEVEL": "warning", "SYSTEMD_PAGER": ""}
        # verify uses systemd's test manager and checks directives, dependencies
        # and the real bundled ExecStart executable. It does not load a service
        # into the runner's active user manager or require its D-Bus session.
        subprocess.run(["systemd-analyze", "--user", "--man=no", "verify", str(unit)],
                       env=env, check=True, timeout=30)
    print("Generated user unit passed native systemd verification; no host service was installed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    verify(args.root.resolve(), args.work.resolve())
