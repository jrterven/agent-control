"""Exercise the bundled Hermes server with disposable data and no provider credentials."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def exercise(root: Path, work: Path) -> None:
    from agent_control_connector.visual_media import media_self_test
    from agent_control_connector.background_install import background_self_test
    media_self_test()
    background_self_test()
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="http-smoke-", dir=work) as temporary:
        home = Path(temporary)
        hermes_home = home / "hermes-home"
        hermes_home.mkdir()
        configuration = {"security": {"allow_lazy_installs": False},
                         "tools": {"enabled_toolsets": ["terminal", "file", "cronjob"]},
                         "platform_toolsets": {"cli": ["terminal", "file", "cronjob"]}}
        (hermes_home / "config.yaml").write_text(json.dumps(configuration))
        (hermes_home / "profile.yaml").write_text('display_name: Mi asistente\n')
        token = secrets.token_urlsafe(32)
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        env = {"HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
               "HERMES_HOME": str(hermes_home), "HERMES_DASHBOARD_SESSION_TOKEN": token,
               "HERMES_DISABLE_LAZY_INSTALLS": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
               "PYTHONPATH": os.pathsep.join((str(root / "connector"), str(root / "hermes"))),
               "SSL_CERT_FILE": str(root / "python/lib/python3.12/site-packages/certifi/cacert.pem")}
        command = [str(root / "python/bin/python3"), "-s", "-B", "-c", "from hermes_cli.main import main; main()",
                   "serve", "--host", "127.0.0.1", "--port", str(port), "--isolated"]
        # Discard logs: do not turn this smoke into a token or transcript logger.
        child = subprocess.Popen(command, env=env, cwd=hermes_home, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            url = f"http://127.0.0.1:{port}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 60
            while True:
                if child.poll() is not None:
                    raise ValueError(f"Bundled Hermes exited before readiness ({child.returncode})")
                try:
                    request = urllib.request.Request(url + "/api/profiles", headers={"X-Hermes-Session-Token": token})
                    with opener.open(request, timeout=2) as response:
                        profiles = json.load(response)
                    assert any(p.get("name") == "default" for p in profiles["profiles"])
                    break
                except (OSError, urllib.error.URLError):
                    if time.monotonic() >= deadline:
                        raise ValueError("Bundled Hermes did not become ready") from None
                    time.sleep(0.2)
            try:
                opener.open(url + "/api/profiles", timeout=2)
            except urllib.error.HTTPError as error:
                assert error.code == 401, "Unexpected local authentication response"
            else:
                raise ValueError("Bundled Hermes accepted an unauthenticated profiles request")
            async def capabilities():
                from hermes_client import HermesGatewayProvider, ProviderConnection
                provider = HermesGatewayProvider(ProviderConnection(gateway_id="managed-smoke", profile_name="default",
                    rest_url=url, ws_url=url.replace("http", "ws", 1) + "/api/ws", dashboard_token=token,
                    trusted_source_sha="939e45c91d751fadd94dcd1b873ac3cb44846213"))
                try:
                    assert (await provider.capabilities()).version == "0.21.2"
                    assert "default" in [p.name for p in await provider.list_profiles()]
                finally:
                    await provider.close()
            asyncio.run(asyncio.wait_for(capabilities(), timeout=45))
        finally:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
    print("Managed runtime HTTP, authentication, profile discovery and audited capabilities passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    args = parser.parse_args()
    exercise(args.root.resolve(), args.work.resolve())
