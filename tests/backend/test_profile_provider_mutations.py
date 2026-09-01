from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

import hermes_client.provider as provider_module
from hermes_client import (
    HermesGatewayProvider,
    InMemoryHermesProvider,
    ProviderConnection,
)
from hermes_client.limits import UpstreamPayloadError, UpstreamPayloadTooLarge
from hermes_control_api.providers import FailoverProvider


def _connection(gateway_id: str, *, token: str = "dashboard-token") -> ProviderConnection:
    return ProviderConnection(
        gateway_id=gateway_id,
        profile_name="control-dev",
        rest_url=f"http://{gateway_id}.test",
        ws_url=f"ws://{gateway_id}.test/api/ws",
        dashboard_token=token,
        trusted_source_sha="9978706e9303dbf990d90e744b131361449d73b9",
    )


async def _use_transport(provider: HermesGatewayProvider, handler) -> None:
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(
        base_url=provider.connection.rest_url,
        headers={"X-Hermes-Session-Token": provider.connection.dashboard_token or ""},
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_delete_profile_uses_one_quoted_native_request():
    provider = HermesGatewayProvider(_connection("source"))
    calls: list[tuple[str, bytes]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.raw_path))
        assert request.headers["X-Hermes-Session-Token"] == "dashboard-token"
        return httpx.Response(200, json={"ok": True, "path": "/profiles/research"})

    await _use_transport(provider, handler)
    try:
        await provider.delete_profile("research/blue")
    finally:
        await provider.close()

    assert calls == [("DELETE", b"/api/profiles/research%2Fblue")]


@pytest.mark.asyncio
async def test_delete_profile_transport_failure_is_ambiguous_and_not_retried():
    provider = HermesGatewayProvider(_connection("source"))
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("reply lost", request=request)

    await _use_transport(provider, handler)
    try:
        with pytest.raises(RuntimeError, match="^MUTATION_DELIVERY_UNKNOWN$"):
            await provider.delete_profile("researcher")
    finally:
        await provider.close()

    assert calls == 1


@pytest.mark.asyncio
async def test_delete_profile_rejects_unconfirmed_response():
    provider = HermesGatewayProvider(_connection("source"))

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False})

    await _use_transport(provider, handler)
    try:
        with pytest.raises(UpstreamPayloadError):
            await provider.delete_profile("researcher")
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_replace_config_uses_parsed_raw_endpoint_with_json_yaml():
    provider = HermesGatewayProvider(_connection("source"))
    config = {
        "model": {"provider": "openai-codex", "default": "gpt-5.6-sol"},
        "security": {"redact_secrets": True},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/api/config/raw"
        assert request.url.params["profile"] == "control-dev"
        payload = json.loads(await request.aread())
        assert payload["profile"] == "control-dev"
        assert json.loads(payload["yaml_text"]) == config
        return httpx.Response(200, json={"ok": True})

    await _use_transport(provider, handler)
    try:
        result = await provider.replace_config(config)
    finally:
        await provider.close()

    assert result.resource == "config"
    assert result.data == {"ok": True}


@pytest.mark.asyncio
async def test_transfer_streams_native_archive_imports_and_cleans_both_gateways():
    source = HermesGatewayProvider(_connection("source", token="source-token"))
    destination = HermesGatewayProvider(
        _connection("destination", token="destination-token")
    )
    archive = b"native-hermes-profile-archive"
    source_calls: list[str] = []
    destination_calls: list[str] = []
    destination_path = ""

    async def source_handler(request: httpx.Request) -> httpx.Response:
        source_calls.append(f"{request.method} {request.url.path}")
        assert request.headers["X-Hermes-Session-Token"] == "source-token"
        if request.method == "POST":
            assert json.loads(await request.aread()) == {"output": "", "extra_files": {}}
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "archive": "/source/profile-exports/jemai.tar.gz",
                },
            )
        if request.method == "GET":
            assert request.url.params["path"] == "/source/profile-exports/jemai.tar.gz"
            return httpx.Response(200, content=archive)
        assert json.loads(await request.aread()) == {
            "path": "/source/profile-exports/jemai.tar.gz",
            "recursive": False,
        }
        return httpx.Response(200, json={"ok": True, "path": "/source/profile-exports/jemai.tar.gz"})

    async def destination_handler(request: httpx.Request) -> httpx.Response:
        nonlocal destination_path
        destination_calls.append(f"{request.method} {request.url.path}")
        assert request.headers["X-Hermes-Session-Token"] == "destination-token"
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "path": "/opt/data",
                    "locked_root": "/opt/data",
                    "can_change_path": False,
                    "entries": [],
                },
            )
        if request.url.path == "/api/files/upload-stream":
            body = await request.aread()
            assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
            assert archive in body
            marker = b'name="path"\r\n\r\n'
            destination_path = body.split(marker, 1)[1].split(b"\r\n", 1)[0].decode()
            assert destination_path.startswith("/opt/data/.agent-control-transfers/")
            return httpx.Response(200, json={"ok": True, "path": destination_path})
        payload = json.loads(await request.aread())
        if request.url.path == "/api/profiles/import":
            assert payload == {"archive": destination_path, "name": "jemai"}
            return httpx.Response(
                200,
                json={"ok": True, "name": "jemai", "path": "/profiles/jemai"},
            )
        assert payload == {"path": destination_path, "recursive": False}
        return httpx.Response(200, json={"ok": True, "path": destination_path})

    await _use_transport(source, source_handler)
    await _use_transport(destination, destination_handler)
    try:
        imported = await source.transfer_profile_to(destination, name="jemai")
    finally:
        await source.close()
        await destination.close()

    assert imported.name == "jemai"
    assert imported.status == "unknown"
    assert source_calls == [
        "POST /api/profiles/jemai/export",
        "GET /api/files/download",
        "DELETE /api/files",
    ]
    assert destination_calls == [
        "GET /api/files",
        "POST /api/files/upload-stream",
        "POST /api/profiles/import",
        "DELETE /api/files",
    ]


@pytest.mark.asyncio
async def test_transfer_enforces_streamed_size_cap_and_still_cleans(monkeypatch):
    monkeypatch.setattr(provider_module, "_MAX_PROFILE_ARCHIVE_BYTES", 8)
    source = HermesGatewayProvider(_connection("source"))
    destination = HermesGatewayProvider(_connection("destination"))
    source_cleanup = destination_cleanup = 0

    async def source_handler(request: httpx.Request) -> httpx.Response:
        nonlocal source_cleanup
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "archive": "/exports/jemai.tar.gz"})
        if request.method == "GET":
            return httpx.Response(200, content=b"123456789")
        source_cleanup += 1
        return httpx.Response(200, json={"ok": True, "path": "/exports/jemai.tar.gz"})

    async def destination_handler(request: httpx.Request) -> httpx.Response:
        nonlocal destination_cleanup
        if request.method == "GET":
            return httpx.Response(200, json={"path": "/data", "entries": []})
        if request.method == "DELETE":
            destination_cleanup += 1
            payload = json.loads(await request.aread())
            return httpx.Response(200, json={"ok": True, "path": payload["path"]})
        raise AssertionError("oversized archive must not be uploaded or imported")

    await _use_transport(source, source_handler)
    await _use_transport(destination, destination_handler)
    try:
        with pytest.raises(UpstreamPayloadTooLarge):
            await source.transfer_profile_to(destination, name="jemai")
    finally:
        await source.close()
        await destination.close()

    assert source_cleanup == 1
    assert destination_cleanup == 1


@pytest.mark.asyncio
async def test_transfer_import_timeout_is_ambiguous_once_and_cleanup_does_not_mask_it():
    source = HermesGatewayProvider(_connection("source"))
    destination = HermesGatewayProvider(_connection("destination"))
    import_calls = source_cleanup = destination_cleanup = 0
    destination_path = ""

    async def source_handler(request: httpx.Request) -> httpx.Response:
        nonlocal source_cleanup
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "archive": "/exports/jemai.tar.gz"})
        if request.method == "GET":
            return httpx.Response(200, content=b"archive")
        source_cleanup += 1
        return httpx.Response(500, json={"detail": "cleanup failed"})

    async def destination_handler(request: httpx.Request) -> httpx.Response:
        nonlocal import_calls, destination_cleanup, destination_path
        if request.method == "GET":
            return httpx.Response(200, json={"path": "/data", "entries": []})
        if request.url.path == "/api/files/upload-stream":
            body = await request.aread()
            marker = b'name="path"\r\n\r\n'
            destination_path = body.split(marker, 1)[1].split(b"\r\n", 1)[0].decode()
            return httpx.Response(200, json={"ok": True, "path": destination_path})
        if request.url.path == "/api/profiles/import":
            import_calls += 1
            raise httpx.ReadTimeout("reply lost", request=request)
        destination_cleanup += 1
        return httpx.Response(500, json={"detail": "cleanup failed"})

    await _use_transport(source, source_handler)
    await _use_transport(destination, destination_handler)
    try:
        with pytest.raises(RuntimeError, match="^MUTATION_DELIVERY_UNKNOWN$"):
            await source.transfer_profile_to(destination, name="jemai")
    finally:
        await source.close()
        await destination.close()

    assert import_calls == 1
    assert source_cleanup == 1
    assert destination_cleanup == 1


@pytest.mark.asyncio
async def test_in_memory_and_failover_transfer_keep_source_until_explicit_delete():
    suffix = uuid4().hex
    source = InMemoryHermesProvider(_connection(f"source-{suffix}"))
    destination = InMemoryHermesProvider(_connection(f"destination-{suffix}"))
    source_fallback = InMemoryHermesProvider(_connection(f"source-fallback-{suffix}"))
    destination_fallback = InMemoryHermesProvider(
        _connection(f"destination-fallback-{suffix}")
    )
    source_failover = FailoverProvider(source, source_fallback, allow_fallback=False)  # type: ignore[arg-type]
    destination_failover = FailoverProvider(
        destination,
        destination_fallback,
        allow_fallback=False,
    )  # type: ignore[arg-type]

    await source.create_profile(name="jemai", display_name="JemAI")
    imported = await source_failover.transfer_profile_to(
        destination_failover,
        name="jemai",
    )

    assert imported.display_name == "JemAI"
    assert any(row.name == "jemai" for row in await source.list_profiles())
    assert any(row.name == "jemai" for row in await destination.list_profiles())

    await source_failover.delete_profile("jemai")
    assert all(row.name != "jemai" for row in await source.list_profiles())

    await source_failover.close()
    await destination_failover.close()
