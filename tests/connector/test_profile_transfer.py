import hashlib
import io
import os
from pathlib import Path
import tarfile
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest

from agent_control_connector import profile_transfer as transfer
from agent_control_connector.runtime import ConnectorRuntime
from agent_control_connector.storage import read_json
from hermes_client import InMemoryHermesProvider
from hermes_client.compatibility import HERMES_0212_SHA
from hermes_client.types import CapabilitySet, HermesProfile


def archive_bytes(name="control-dev", entries=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        root = tarfile.TarInfo(name)
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        for path, data in (entries or {f"{name}/SOUL.md": b"Test agent"}).items():
            info = tarfile.TarInfo(path)
            if isinstance(data, tarfile.TarInfo):
                archive.addfile(data)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return output.getvalue()


def message(operation, *args, manager="manager", identity=None):
    return {"v": 1, "id": uuid4().hex, "type": "request", "profile": manager, "operation": operation,
            "args": args, "kwargs": {}, "operationId": identity or uuid4().hex}


@pytest.fixture
def runtimes(tmp_path):
    results = []
    for label, names in (("source", ["manager", "control-dev"]), ("destination", ["manager"])):
        directory = tmp_path / label
        instance = ConnectorRuntime(directory, {"gatewayId": label + uuid4().hex, "profiles": names.copy(),
            "restUrl": "http://127.0.0.1:9119", "wsUrl": "ws://127.0.0.1:9119/api/ws",
            "sourceSha": HERMES_0212_SHA, "hermesHome": str(directory)}, {"hermesToken": "local-secret"}, InMemoryHermesProvider)
        instance.profile_transfer_supported = True
        instance._background_snapshot = lambda _: {"complete": True, "activeCount": 0, "pendingDeliveryCount": 0}
        manager = instance.providers["manager"]
        native_profiles = [HermesProfile(name=name, display_name=name) for name in names]
        manager.list_profiles = AsyncMock(side_effect=lambda: native_profiles.copy())
        instance.native_profiles = native_profiles
        manager.capabilities = AsyncMock(return_value=CapabilitySet(protocol="test", version="0.21.2", source_sha=HERMES_0212_SHA,
            methods=frozenset({"profiles.export", "profiles.import", "profiles.transfer"}), features=frozenset()))
        manager.import_profile_archive_from = AsyncMock(side_effect=lambda name, path: HermesProfile(name=name, display_name=name))
        for name in names:
            instance.providers[name].list_sessions = AsyncMock(return_value=[])
        results.append(instance)
    yield results
    for instance in results:
        instance.ledger.close()


async def stage_import(runtime, content, name="control-dev"):
    identifier = uuid4().hex
    started = await runtime.execute(message("profile_import_begin", name, identifier, len(content), hashlib.sha256(content).hexdigest()))
    assert "error" not in started, started
    for offset in range(0, len(content), transfer.MAX_CHUNK_BYTES):
        receipt = await runtime.execute(message("profile_archive_write", identifier, offset, content[offset:offset + transfer.MAX_CHUNK_BYTES]))
        assert "error" not in receipt, receipt
    return identifier


@pytest.mark.asyncio
async def test_chunk_transfer_excludes_credentials_preserves_source_and_replays_small_receipts(runtimes):
    source, destination = runtimes
    payload = os.urandom(transfer.MAX_CHUNK_BYTES + 100)
    content = archive_bytes(entries={"control-dev/SOUL.md": b"Test agent", "control-dev/state.db": payload,
        "control-dev/.env": b"API_KEY=secret", "control-dev/auth.json": b'{"token":"private"}',
        "control-dev/.anthropic_oauth.json": b'{"refresh_token":"private"}'})

    async def export(name, path):
        assert name == "control-dev"
        path.write_bytes(content)
        path.chmod(0o600)
    source.providers["manager"].export_profile_archive_to = AsyncMock(side_effect=export)
    identifier = uuid4().hex
    export_request = message("profile_export", "control-dev", identifier)
    exported = await source.execute(export_request)
    assert "error" not in exported, exported
    assert (await source.execute(export_request))["result"] == exported["result"]
    source.providers["manager"].export_profile_archive_to.assert_awaited_once()
    assert {p.name for p in source.native_profiles} == {"manager", "control-dev"}
    receipt = exported["result"]
    assert set(receipt) == {"transferId", "size", "sha256", "offset"}
    begin = message("profile_import_begin", "control-dev", identifier, receipt["size"], receipt["sha256"])
    assert "error" not in await destination.execute(begin)
    downloaded = bytearray()
    for offset in range(0, receipt["size"], transfer.MAX_CHUNK_BYTES):
        length = min(transfer.MAX_CHUNK_BYTES, receipt["size"] - offset)
        chunk = (await source.execute(message("profile_archive_read", identifier, offset, length)))["result"]
        downloaded.extend(chunk)
        write = message("profile_archive_write", identifier, offset, chunk)
        first = await destination.execute(write)
        assert first["result"]["offset"] == offset + length
        assert (await destination.execute(write))["result"] == first["result"]
    with tarfile.open(fileobj=io.BytesIO(downloaded)) as archive:
        assert archive.getnames() == ["control-dev", "control-dev/SOUL.md", "control-dev/state.db"]
        assert archive.extractfile("control-dev/state.db").read() == payload
    finish = message("profile_import_finish", identifier)
    imported = await destination.execute(finish)
    assert imported["result"].name == "control-dev"
    assert (await destination.execute(finish))["result"] == imported["result"]
    destination.providers["manager"].import_profile_archive_from.assert_awaited_once()
    assert "control-dev" in destination.providers
    assert "control-dev" in read_json(destination.directory / "config.json")["profiles"]
    for runtime in (source, destination):
        assert "error" not in await runtime.execute(message("profile_archive_cleanup", identifier))
        stage = runtime.directory / "profile-transfers" / identifier
        assert sorted(p.name for p in stage.iterdir()) == ["meta.json"]
        assert read_json(stage / "meta.json")["state"] == "cleaned"
        assert runtime.ledger.db.execute("SELECT max(length(result)) FROM operations").fetchone()[0] < 64 * 1024


@pytest.mark.asyncio
async def test_transfer_capability_requires_new_cloud_and_audited_runtime(runtimes):
    runtime = runtimes[0]
    result = (await runtime.execute(message("capabilities")))["result"]
    assert "connector.profileTransferV1" in result.features
    runtime.profile_transfer_supported = False
    result = (await runtime.execute(message("capabilities")))["result"]
    assert "profiles.transfer" not in result.methods
    assert "error" in await runtime.execute(message("profile_export", "control-dev", uuid4().hex))
    runtime.profile_transfer_supported = True
    runtime.config["sourceSha"] = "0" * 40
    result = (await runtime.execute(message("capabilities")))["result"]
    assert "profiles.transfer" not in result.methods


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["default", "unshared", "../control-dev"])
async def test_export_rejects_unshared_default_or_path_names_before_dispatch(runtimes, name):
    runtime = runtimes[0]
    runtime.providers["manager"].export_profile_archive_to = AsyncMock()
    assert "error" in await runtime.execute(message("profile_export", name, uuid4().hex))
    runtime.providers["manager"].export_profile_archive_to.assert_not_called()


@pytest.mark.asyncio
async def test_export_rejects_uncertain_background_work(runtimes):
    runtime = runtimes[0]
    runtime._background_snapshot = lambda _: {"complete": False, "activeCount": None, "pendingDeliveryCount": None}
    runtime.providers["manager"].export_profile_archive_to = AsyncMock()
    assert "error" in await runtime.execute(message("profile_export", "control-dev", uuid4().hex))
    runtime.providers["manager"].export_profile_archive_to.assert_not_called()


@pytest.mark.asyncio
async def test_import_collision_with_private_native_profile_is_not_adopted(runtimes):
    runtime = runtimes[1]
    runtime.native_profiles.append(HermesProfile(name="control-dev", display_name="private"))
    response = await runtime.execute(message("profile_import_begin", "control-dev", uuid4().hex, 1, "a" * 64))
    assert "error" in response
    assert "control-dev" not in runtime.providers
    runtime.providers["manager"].import_profile_archive_from.assert_not_called()


@pytest.mark.asyncio
async def test_archive_identity_is_bound_to_manager_and_write_offsets(runtimes):
    runtime = runtimes[0]
    content = archive_bytes(name="new-agent")
    identifier = uuid4().hex
    assert "error" not in await runtime.execute(message("profile_import_begin", "new-agent", identifier, len(content), hashlib.sha256(content).hexdigest()))
    for command in (message("profile_archive_write", identifier, 0, content, manager="control-dev"),
                    message("profile_archive_cleanup", identifier, manager="control-dev"),
                    message("profile_archive_write", identifier, 1, content),
                    message("profile_archive_write", identifier, 0, b"x" * (transfer.MAX_CHUNK_BYTES + 1)),
                    message("profile_archive_write", "../outside", 0, content)):
        assert "error" in await runtime.execute(command)
    assert (runtime.directory / "profile-transfers" / identifier / "archive.tar.gz").stat().st_size == 0


@pytest.mark.asyncio
async def test_import_timeout_never_adopts_or_reimports_and_cleanup_retains_uncertainty(runtimes):
    runtime = runtimes[1]
    identifier = await stage_import(runtime, archive_bytes())
    runtime.providers["manager"].import_profile_archive_from.side_effect = TimeoutError("private-local-details")
    finish = message("profile_import_finish", identifier)
    first = await runtime.execute(finish)
    assert first["error"] == "CONNECTOR_DELIVERY_UNKNOWN"
    assert (await runtime.execute(finish))["error"] == "CONNECTOR_DELIVERY_UNKNOWN"
    assert "error" in await runtime.execute(message("profile_import_finish", identifier))
    runtime.providers["manager"].import_profile_archive_from.assert_awaited_once()
    assert "control-dev" not in runtime.providers
    assert "error" not in await runtime.execute(message("profile_archive_cleanup", identifier))
    stage = runtime.directory / "profile-transfers" / identifier
    assert read_json(stage / "meta.json")["state"] == "importing"
    assert sorted(p.name for p in stage.iterdir()) == ["meta.json"]


@pytest.mark.asyncio
async def test_collision_at_finish_and_checksum_failure_are_proven_refusals(runtimes):
    runtime = runtimes[1]
    identifier = await stage_import(runtime, archive_bytes())
    archive = runtime.directory / "profile-transfers" / identifier / "archive.tar.gz"
    original = archive.read_bytes()
    archive.write_bytes(b"x" * len(original))
    assert (await runtime.execute(message("profile_import_finish", identifier)))["error"] == "PROFILE_TRANSFER_IMPORT_REFUSED"
    archive.write_bytes(original)
    runtime.native_profiles.append(HermesProfile(name="control-dev", display_name="private"))
    assert (await runtime.execute(message("profile_import_finish", identifier)))["error"] == "PROFILE_TRANSFER_IMPORT_REFUSED"
    runtime.providers["manager"].import_profile_archive_from.assert_not_called()
    assert "control-dev" not in runtime.providers


@pytest.mark.asyncio
async def test_native_http_conflict_has_explicit_no_import_receipt(runtimes):
    runtime = runtimes[1]
    identifier = await stage_import(runtime, archive_bytes())
    response = httpx.Response(400, request=httpx.Request("POST", "http://127.0.0.1/api/profiles/import"))
    runtime.providers["manager"].import_profile_archive_from.side_effect = httpx.HTTPStatusError("private", request=response.request, response=response)
    assert (await runtime.execute(message("profile_import_finish", identifier)))["error"] == "PROFILE_TRANSFER_IMPORT_REFUSED"
    assert "control-dev" not in runtime.providers


@pytest.mark.parametrize("bad_path", ["../escape", "/absolute", "other/SOUL.md", "control-dev/../../escape", "control-dev\\escape", "C:/escape", "control-dev/.env", "control-dev/auth.json"])
def test_archive_validation_rejects_paths_and_credentials(tmp_path, bad_path):
    path = tmp_path / "archive.tar.gz"
    path.write_bytes(archive_bytes(entries={bad_path: b"secret"}))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        transfer.validate_archive(path, "control-dev")


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE])
def test_archive_validation_rejects_links_and_special_files(tmp_path, kind):
    info = tarfile.TarInfo("control-dev/link")
    info.type = kind
    info.linkname = "/outside"
    path = tmp_path / "archive.tar.gz"
    path.write_bytes(archive_bytes(entries={info.name: info}))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        transfer.validate_archive(path, "control-dev")


def test_archive_validation_enforces_expansion_and_member_limits(tmp_path, monkeypatch):
    path = tmp_path / "archive.tar.gz"
    path.write_bytes(archive_bytes(entries={"control-dev/large": b"x" * 1000}))
    path.chmod(0o600)
    monkeypatch.setattr(transfer, "MAX_EXPANDED_BYTES", 999)
    with pytest.raises(ValueError):
        transfer.validate_archive(path, "control-dev")
    monkeypatch.setattr(transfer, "MAX_EXPANDED_BYTES", 2000)
    monkeypatch.setattr(transfer, "MAX_MEMBERS", 1)
    with pytest.raises(ValueError):
        transfer.validate_archive(path, "control-dev")


@pytest.mark.asyncio
async def test_crash_after_dispatch_preserves_unknown_reservation_across_restart(runtimes):
    import asyncio
    original = runtimes[1]
    identifier = await stage_import(original, archive_bytes())
    native_import = AsyncMock(side_effect=asyncio.CancelledError())
    original.providers["manager"].import_profile_archive_from = native_import
    finish = message("profile_import_finish", identifier)
    with pytest.raises(asyncio.CancelledError):
        await original.execute(finish)
    original.ledger.close()
    restarted = ConnectorRuntime(original.directory, original.config.copy(), {"hermesToken": "secret"}, InMemoryHermesProvider)
    restarted.profile_transfer_supported = True
    restarted.providers["manager"].import_profile_archive_from = native_import
    try:
        assert (await restarted.execute(finish))["error"] == "CONNECTOR_DELIVERY_UNKNOWN"
        assert "error" in await restarted.execute(message("profile_import_finish", identifier))
        native_import.assert_awaited_once()
        assert "control-dev" not in restarted.providers
        assert read_json(restarted.directory / "profile-transfers" / identifier / "meta.json")["state"] == "importing"
    finally:
        restarted.ledger.close()


@pytest.mark.asyncio
async def test_cleanup_never_follows_symlink_or_reuses_completed_identity(runtimes, tmp_path):
    runtime = runtimes[1]
    identifier = await stage_import(runtime, archive_bytes())
    path = runtime.directory / "profile-transfers" / identifier / "archive.tar.gz"
    secret = tmp_path / "unrelated.txt"
    secret.write_text("preserve")
    secret.chmod(0o600)
    path.unlink()
    path.symlink_to(secret)
    assert "error" in await runtime.execute(message("profile_archive_cleanup", identifier))
    assert secret.read_text() == "preserve"
    path.unlink()
    assert "error" not in await runtime.execute(message("profile_archive_cleanup", identifier))
    assert "error" in await runtime.execute(message("profile_import_begin", "another", identifier, 1, "a" * 64))


def test_pax_header_decompression_is_bounded_before_tar_yields_a_member(tmp_path):
    import gzip
    header = tarfile.TarInfo("control-dev/pax")
    header.type = tarfile.XHDTYPE
    header.size = transfer.MAX_EXPANDED_BYTES + transfer.MAX_MEMBERS * 4096 + 1
    path = tmp_path / "archive.tar.gz"
    path.write_bytes(gzip.compress(header.tobuf() + b"\0" * 1024))
    path.chmod(0o600)
    with pytest.raises(ValueError):
        transfer.validate_archive(path, "control-dev")
