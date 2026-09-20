import json
import os
import socket
import subprocess
import types
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from agent_control_connector import profile_export, profile_export_worker as worker
from agent_control_connector import profile_transfer as transfer
from hermes_client.compatibility import HERMES_0212_SHA
from test_profile_transfer import runtimes, message, archive_bytes


def test_socket_export_preserves_data_and_excludes_process_state_and_credentials(tmp_path, monkeypatch):
    source = tmp_path / "jarvis"
    source.mkdir()
    (source / "state").mkdir()
    (source / "SOUL.md").write_text("Jarvis")
    (source / "example.sock").write_bytes(b"regular user file")
    (source / "gateway.pid").write_text("123")
    (source / "auth.json").write_text("secret")
    (source / ".env.local").write_text("secret")
    (source / ".agent-control/background").mkdir(parents=True)
    (source / ".agent-control/background/runtime.json").write_text("source pid")
    sockets = [socket.socket(socket.AF_UNIX) for _ in range(2)]
    try:
        # macOS limits Unix socket path length to 104 bytes.
        monkeypatch.chdir(tmp_path)
        sockets[0].bind("jarvis/a")
        sockets[1].bind("jarvis/state/b")
        destination = tmp_path / "copy"
        worker.portable_copytree(source, destination)
        assert (destination / "SOUL.md").read_text() == "Jarvis"
        assert (destination / "example.sock").read_bytes() == b"regular user file"
        assert sorted(p.relative_to(destination).as_posix() for p in destination.rglob("*") if p.is_file()) == ["SOUL.md", "example.sock"]
        assert (source / "gateway.pid").read_text() == "123"
        assert (source / "auth.json").read_text() == "secret"
        assert (source / "a").is_socket() and (source / "state/b").is_socket()
    finally:
        for value in sockets:
            value.close()


@pytest.mark.parametrize("kind", ["link", "directory_link", "fifo"])
def test_special_files_never_read_other_paths_or_block(tmp_path, kind):
    source = tmp_path / "profile"
    source.mkdir()
    outside = tmp_path / "secret"
    outside.write_text("do not copy")
    if kind == "link":
        (source / "entry").symlink_to(outside)
    elif kind == "directory_link":
        (source / "entry").symlink_to(tmp_path, target_is_directory=True)
    else:
        os.mkfifo(source / "entry")
    with pytest.raises(ValueError, match="special file"):
        worker.portable_copytree(source, tmp_path / "copy")
    assert outside.read_text() == "do not copy"
    assert not (tmp_path / "copy/entry").exists()


@pytest.mark.parametrize("limit", ["members", "bytes"])
def test_portable_copy_limits_are_enforced_across_siblings(tmp_path, monkeypatch, limit):
    source = tmp_path / "profile"
    source.mkdir()
    for name in ("a", "b", "c"):
        (source / name).mkdir()
        (source / name / "file").write_bytes(b"0123456789")
    monkeypatch.setattr(worker, "MAX_MEMBERS" if limit == "members" else "MAX_EXPANDED_BYTES", 4 if limit == "members" else 20)
    with pytest.raises(ValueError):
        worker.portable_copytree(source, tmp_path / "copy")


def test_copy_rejects_file_replaced_by_link(tmp_path, monkeypatch):
    source = tmp_path / "profile"
    source.mkdir()
    target = source / "file"
    target.write_text("before")
    outside = tmp_path / "secret"
    outside.write_text("secret")
    original = os.open
    def changed(path, flags, *args, **kwargs):
        if path == "file":
            target.unlink()
            target.symlink_to(outside)
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(worker.os, "open", changed)
    with pytest.raises(OSError):
        worker.portable_copytree(source, tmp_path / "copy")
    assert not (tmp_path / "copy/file").exists()
    assert outside.read_text() == "secret"


def test_subprocess_uses_verified_source_and_does_not_inherit_secrets(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "profiles/jarvis").mkdir(parents=True)
    source = tmp_path / "source"
    python = source / "venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("unused")
    python.chmod(0o700)
    output = tmp_path / "output.tar.gz"
    from agent_control_connector import cli
    monkeypatch.setattr(cli, "detect_revision", lambda *_: (HERMES_0212_SHA, source))
    monkeypatch.setenv("OPENAI_API_KEY", "private-inherited-key")
    monkeypatch.setenv("HERMES_DASHBOARD_SESSION_TOKEN", "private-local-token")
    def run(args, **kwargs):
        assert args[:4] == [str(python), "-I", "-B", "-c"]
        assert "private-inherited-key" not in str(kwargs)
        assert "private-local-token" not in str(kwargs)
        request = json.loads(kwargs["input"])
        assert request["name"] == "jarvis" and request["source"] == str(source)
        assert kwargs["env"]["HERMES_HOME"] == str(home)
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        output.write_bytes(b"archive")
        return types.SimpleNamespace(returncode=0)
    monkeypatch.setattr(profile_export.subprocess, "run", run)
    profile_export.export_snapshot({"hermesHome": str(home), "hermesSource": str(source), "sourceSha": HERMES_0212_SHA}, "jarvis", output)


@pytest.mark.asyncio
async def test_native_export_500_uses_local_snapshot_without_import_or_deletion(runtimes, monkeypatch):
    source, destination = runtimes
    provider = source.providers["manager"]
    provider.export_profile_archive_to = AsyncMock(side_effect=httpx.HTTPStatusError("private error", request=httpx.Request("POST", "http://127.0.0.1/export"), response=httpx.Response(500)))
    calls = []
    def snapshot(config, name, path):
        calls.append(name)
        path.write_bytes(archive_bytes())
        path.chmod(0o600)
    monkeypatch.setattr(transfer, "export_snapshot", snapshot)
    request = message("profile_export", "control-dev", uuid4().hex)
    response = await source.execute(request)
    assert "error" not in response
    assert (await source.execute(request))["result"] == response["result"]
    assert calls == ["control-dev"]
    provider.export_profile_archive_to.assert_awaited_once()
    destination.providers["manager"].import_profile_archive_from.assert_not_called()
    assert source.config["profiles"] == ["manager", "control-dev"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 409, 413, 503])
async def test_export_refusals_do_not_trigger_local_snapshot(runtimes, monkeypatch, status):
    source, _ = runtimes
    source.providers["manager"].export_profile_archive_to = AsyncMock(side_effect=httpx.HTTPStatusError("private", request=httpx.Request("POST", "http://127.0.0.1/export"), response=httpx.Response(status)))
    monkeypatch.setattr(transfer, "export_snapshot", lambda *_: pytest.fail("must not bypass a refusal"))
    assert "error" in await source.execute(message("profile_export", "control-dev", uuid4().hex))
