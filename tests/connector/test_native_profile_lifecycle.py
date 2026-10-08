"""Opt-in lifecycle proof with two real Hermes servers and disposable homes.

HERMES_NATIVE_TEST_RUNTIME=/absolute/managed/runtime
No model provider or user credentials are used.
"""
import asyncio
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import tarfile
import time
from uuid import uuid4

import httpx
import pytest

from agent_control_connector.runtime import ConnectorRuntime
from agent_control_connector import background_install, chat_modes_install, media_install
from agent_control_connector import profile_export_worker
from agent_control_connector.profile_transfer import MAX_CHUNK_BYTES
from agent_control_connector.storage import read_json
from hermes_client.compatibility import HERMES_0216_SHA


@pytest.mark.skipif(not os.environ.get("HERMES_NATIVE_TEST_RUNTIME"), reason="Requires native managed Hermes payload")
@pytest.mark.asyncio
async def test_native_profile_transfer_config_and_delete(tmp_path):
    root = Path(os.environ["HERMES_NATIVE_TEST_RUNTIME"]).resolve()
    assert json.loads((root / "build-provenance.json").read_text())["hermesSourceSha"] == HERMES_0216_SHA
    repo = Path(__file__).resolve().parents[2]
    children, runtimes, logs = [], [], []
    try:
        for label in ("source", "destination"):
            base = tmp_path / label
            home = base / "hermes"
            home.mkdir(parents=True)
            (home / "config.yaml").write_text(json.dumps({"security": {"allow_lazy_installs": False},
                "model": {"provider": "custom", "base_url": "http://127.0.0.1:1/v1", "default": "offline-model"}}))
            chat_modes_install.install_profile(home, HERMES_0216_SHA)
            background_install.install_profile(home, HERMES_0216_SHA)
            media_install.install_profile(home)
            token = secrets.token_urlsafe(32)
            with socket.socket() as reserve:
                reserve.bind(("127.0.0.1", 0))
                port = reserve.getsockname()[1]
            url = f"http://127.0.0.1:{port}"
            env = {"HOME": str(base), "HERMES_HOME": str(home), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
                "HERMES_DASHBOARD_SESSION_TOKEN": token, "HERMES_DISABLE_LAZY_INSTALLS": "1",
                "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                "PYTHONPATH": os.pathsep.join((str(repo / "packages/connector"),
                    str(repo / "packages/hermes-client"), str(root / "hermes")))}
            log = (base / "native.log").open("w+")
            logs.append(log)
            child = subprocess.Popen([str(root / "python/bin/python3"), "-s", "-B", "-c",
                "from hermes_cli.main import main; main()", "serve", "--host", "127.0.0.1", "--port", str(port), "--isolated"],
                cwd=home, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
            children.append(child)
            deadline = time.monotonic() + 60
            async with httpx.AsyncClient(trust_env=False) as probe:
                while True:
                    if child.poll() is not None:
                        raise AssertionError(f"Native {label} exited ({child.returncode})")
                    try:
                        response = await probe.get(url + "/api/profiles", headers={"X-Hermes-Session-Token": token}, timeout=1)
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert time.monotonic() < deadline, f"Native {label} did not become ready"
                    await asyncio.sleep(.1)
            runtime = ConnectorRuntime(base / "connector", {"gatewayId": label, "profiles": ["default"],
                "restUrl": url, "wsUrl": url.replace("http", "ws", 1) + "/api/ws", "sourceSha": HERMES_0216_SHA,
                "hermesHome": str(home), "hermesSource": str(root / "hermes")}, {"hermesToken": token})
            runtime.profile_transfer_supported = True
            runtimes.append(runtime)
            # Canonical startup must activate the actual installed plugin files,
            # including their source audits, using native PluginContext APIs.
            assert background_install.probe_profile(home, HERMES_0216_SHA)["state"] == "ready"
            marker = read_json(home / ".agent-control/background/runtime.json")
            assert marker["profileDeliveryMode"] == "native-tui" and marker["profileDeliveryShim"] is None
            assert media_install.probe_profile(home)["state"] == "ready"
            capabilities = await runtime.providers["default"].capabilities()
            assert {"session.mode.temporary", "session.mode.memory_read_only"} <= capabilities.features

        async def execute(runtime, operation, *args, kwargs=None, profile="default", replay=False):
            request = {"v": 1, "id": uuid4().hex, "type": "request", "profile": profile,
                "operation": operation, "args": args, "kwargs": kwargs or {}, "operationId": uuid4().hex}
            result = await runtime.execute(request)
            assert "error" not in result, (operation, result)
            if replay:
                assert (await runtime.execute(request))["result"] == result["result"]
            return result["result"]

        source, destination = runtimes
        name = "portable-fixture"
        created = await execute(source, "create_profile", kwargs={"name": name, "display_name": name}, replay=True)
        assert created.name == name
        source_home = Path(source.config["hermesHome"]) / "profiles" / name
        source_provider = source.providers[name]
        await source_provider.update_config({"display": {"compact": True}, "agent": {"max_turns": 7}})
        config = await source_provider.get_transfer_config()
        assert config.data["agent"]["max_turns"] == 7
        (source_home / "SOUL.md").write_text("Portable native fixture soul")
        (source_home / "memories").mkdir(exist_ok=True)
        (source_home / "memories/MEMORY.md").write_text("Portable native fixture memory")
        (source_home / ".env").write_text("PRIVATE_NATIVE_CANARY=do-not-transfer\n")
        (source_home / "auth.json").write_text('{"token":"do-not-transfer"}')
        (source_home / ".anthropic_oauth.json").write_text('{"token":"do-not-transfer"}')
        # Exercise the bounded fallback with the real new exporter and sealed
        # PM dependency activation too; the normal native export succeeds below.
        fallback = tmp_path / "fallback.tar.gz"
        request = {"source": str(root / "hermes"), "home": source.config["hermesHome"],
            "name": name, "output": str(fallback), "activateDependencies": True}
        result = await asyncio.to_thread(subprocess.run,
            [str(root / "python/bin/python3"), "-I", "-B", "-c", Path(profile_export_worker.__file__).read_text()],
            input=json.dumps(request), capture_output=True, text=True, timeout=30,
            cwd=source_home, env={"HOME": str(source_home.parent.parent.parent), "HERMES_HOME": source.config["hermesHome"],
                "PATH": "/usr/bin:/bin", "HERMES_DISABLE_LAZY_INSTALLS": "1", "PYTHONDONTWRITEBYTECODE": "1"})
        assert result.returncode == 0, "Isolated native fallback export failed"
        with tarfile.open(fallback) as archive:
            assert archive.extractfile(name + "/SOUL.md").read() == b"Portable native fixture soul"
            assert not any(item.endswith(("/.env", "/auth.json", "/.anthropic_oauth.json")) for item in archive.getnames())
        transfer_id = uuid4().hex
        exported = await execute(source, "profile_export", name, transfer_id, replay=True)
        with tarfile.open(source.directory / "profile-transfers" / transfer_id / "archive.tar.gz") as archive:
            assert not any(item.endswith(("/.env", "/auth.json", "/.anthropic_oauth.json")) for item in archive.getnames())
            assert archive.extractfile(name + "/SOUL.md").read() == b"Portable native fixture soul"
        await execute(destination, "profile_import_begin", name, transfer_id, exported["size"], exported["sha256"])
        for offset in range(0, exported["size"], MAX_CHUNK_BYTES):
            chunk = await execute(source, "profile_archive_read", transfer_id, offset, min(MAX_CHUNK_BYTES, exported["size"] - offset))
            await execute(destination, "profile_archive_write", transfer_id, offset, chunk, replay=True)
        imported = await execute(destination, "profile_import_finish", transfer_id, replay=True)
        assert imported.name == name
        destination_home = Path(destination.config["hermesHome"]) / "profiles" / name
        assert (destination_home / "SOUL.md").read_text() == "Portable native fixture soul"
        assert (destination_home / "memories/MEMORY.md").read_text() == "Portable native fixture memory"
        assert not (destination_home / "auth.json").exists()
        assert not (destination_home / ".anthropic_oauth.json").exists()
        if (destination_home / ".env").exists():
            assert "PRIVATE_NATIVE_CANARY" not in (destination_home / ".env").read_text()
        # Transfer configuration is applied through the same native raw endpoint
        # as production, then a regular partial update must preserve other keys.
        dest_provider = destination.providers[name]
        await dest_provider.replace_config(config.data)
        await dest_provider.update_config({"agent": {"max_turns": 9}})
        restored = await dest_provider.get_transfer_config()
        assert restored.data["agent"]["max_turns"] == 9
        assert restored.data["display"]["compact"] is True
        # A failed parse must never be reported as a successful config save.
        config_file = destination_home / "config.yaml"
        good_config = config_file.read_bytes()
        config_file.write_text("broken: [\n")
        with pytest.raises(RuntimeError, match="^MUTATION_DELIVERY_UNKNOWN$") as refused:
            await dest_provider.update_config({"agent": {"max_turns": 11}})
        assert isinstance(refused.value.__cause__, httpx.HTTPStatusError)
        assert refused.value.__cause__.response.status_code == 500
        assert config_file.read_text() == "broken: [\n"
        config_file.write_bytes(good_config)
        for runtime in runtimes:
            await execute(runtime, "profile_archive_cleanup", transfer_id)
            assert not runtime.profile_transfers.has_pending()
        # The source remains intact until explicit move settlement. Both native
        # deletes must remove local sharing exactly once without deleting default.
        assert source_home.exists()
        for runtime, directory in ((source, source_home), (destination, destination_home)):
            result = await execute(runtime, "delete_profile", name, replay=True)
            assert result is None or result == {"identity_settlement_pending": True}
            assert not directory.exists()
            assert name not in runtime.providers
            assert name not in read_json(runtime.directory / "config.json")["profiles"]
            assert "default" in {profile.name for profile in await runtime.providers["default"].list_profiles()}
    finally:
        for runtime in runtimes:
            for provider in runtime.providers.values():
                await provider.close()
            runtime.ledger.close()
            runtime.temporary_ledger.close()
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait(timeout=5)
        for log in logs:
            log.close()
