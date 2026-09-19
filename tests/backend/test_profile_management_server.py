"""The audited server must never lose the home it is currently serving."""
import json
import httpx
import pytest

from hermes_client import HermesGatewayProvider, ProviderConnection
from hermes_client.compatibility import HERMES_0212_SHA
from hermes_client.provider import ProfileManagementServerRequired


def connection(gateway="source", sha=HERMES_0212_SHA):
    return ProviderConnection(gateway_id=gateway, profile_name="control-dev",
        rest_url=f"http://{gateway}.test", ws_url=f"ws://{gateway}.test/api/ws",
        dashboard_token="private-token", trusted_source_sha=sha)


async def transport(provider, handler):
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(base_url=provider.connection.rest_url,
        headers={"X-Hermes-Session-Token": "private-token"}, transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["export", "import", "delete"])
@pytest.mark.parametrize("proof", [
    {"active": "default", "current": "control-dev"}, {"active": "default"},
    {"current": None}, {"current": False}, {"current": ["default"]}, [],
    "unavailable", "disconnect", "oversized",
])
async def test_unverified_server_never_dispatches_native_mutation(tmp_path, operation, proof):
    provider = HermesGatewayProvider(connection())
    calls = []
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"test archive")

    async def handler(request):
        calls.append((request.method, request.url.path))
        assert request.method == "GET" and request.url.path == "/api/profiles/active"
        assert not request.url.query  # a routed profile must not influence this proof
        assert request.headers["X-Hermes-Session-Token"] == "private-token"
        if proof == "unavailable":
            return httpx.Response(503, text="private upstream error")
        if proof == "disconnect":
            raise httpx.ConnectError("private address", request=request)
        if proof == "oversized":
            return httpx.Response(200, json={"current": "default", "extra": "x" * 5000})
        return httpx.Response(200, json=proof)

    await transport(provider, handler)
    try:
        with pytest.raises(ProfileManagementServerRequired, match="hermes -p default serve") as error:
            if operation == "export":
                await provider.export_profile_archive_to("control-dev", tmp_path / "export.tar.gz")
            elif operation == "import":
                await provider.import_profile_archive_from("control-dev", archive)
            else:
                await provider.delete_profile("control-dev")
        assert "private" not in str(error.value)
        assert calls == [("GET", "/api/profiles/active")]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_current_default_allows_named_routing_but_delete_rechecks_after_import(tmp_path):
    provider = HermesGatewayProvider(connection(sha=HERMES_0212_SHA.upper()))
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"test archive")
    current = "default"
    calls = []
    uploaded = ""

    async def handler(request):
        nonlocal uploaded
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/profiles/active":
            assert not request.url.query
            return httpx.Response(200, json={"active": "control-dev", "current": current})
        if request.method == "GET" and request.url.path == "/api/files":
            return httpx.Response(200, json={"locked_root": "/managed"})
        if request.url.path == "/api/files/upload-stream":
            body = await request.aread()
            uploaded = body.split(b'name="path"\r\n\r\n')[1].split(b"\r\n")[0].decode()
            return httpx.Response(200, json={"ok": True, "path": uploaded})
        if request.url.path == "/api/profiles/import":
            assert json.loads(await request.aread()) == {"name": "control-dev", "archive": uploaded}
            return httpx.Response(200, json={"ok": True, "name": "control-dev"})
        assert request.method == "DELETE" and request.url.path in {"/api/files", "/api/profiles/control-dev"}
        return httpx.Response(200, json={"ok": True})

    await transport(provider, handler)
    try:
        assert (await provider.import_profile_archive_from("control-dev", archive)).name == "control-dev"
        assert calls.count(("GET", "/api/profiles/active")) == 2
        current = "control-dev"
        with pytest.raises(ProfileManagementServerRequired):
            await provider.delete_profile("control-dev")
        assert ("DELETE", "/api/profiles/control-dev") not in calls
        current = "default"
        await provider.delete_profile("control-dev")
        assert calls[-2:] == [("GET", "/api/profiles/active"), ("DELETE", "/api/profiles/control-dev")]
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_import_rechecks_server_after_upload_before_native_creation(tmp_path):
    provider = HermesGatewayProvider(connection())
    archive = tmp_path / "archive.tar.gz"
    archive.write_bytes(b"archive")
    calls, probes = [], 0

    async def handler(request):
        nonlocal probes
        calls.append((request.method, request.url.path))
        if request.url.path == "/api/profiles/active":
            probes += 1
            return httpx.Response(200, json={"current": "default" if probes == 1 else "control-dev"})
        if request.method == "GET":
            return httpx.Response(200, json={"locked_root": "/managed"})
        if request.url.path == "/api/files/upload-stream":
            path = (await request.aread()).split(b'name="path"\r\n\r\n')[1].split(b"\r\n")[0].decode()
            return httpx.Response(200, json={"ok": True, "path": path})
        assert request.method == "DELETE" and request.url.path == "/api/files"
        return httpx.Response(200, json={"ok": True})

    await transport(provider, handler)
    try:
        with pytest.raises(ProfileManagementServerRequired):
            await provider.import_profile_archive_from("control-dev", archive)
        assert probes == 2
        assert ("POST", "/api/profiles/import") not in calls
        assert calls[-1] == ("DELETE", "/api/files")
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unsafe", ["source", "destination"])
async def test_direct_transfer_checks_both_servers_before_export(unsafe):
    providers = [HermesGatewayProvider(connection(name)) for name in ("source", "destination")]
    calls = []
    async def handler(request):
        calls.append((request.url.host, request.method, request.url.path))
        assert request.method == "GET" and request.url.path == "/api/profiles/active"
        return httpx.Response(200, json={"current": "control-dev" if request.url.host == unsafe + ".test" else "default"})
    for provider in providers:
        await transport(provider, handler)
    try:
        with pytest.raises(ProfileManagementServerRequired):
            await providers[0].transfer_profile_to(providers[1], name="control-dev")
        assert all(method == "GET" for _, method, _ in calls)
    finally:
        for provider in providers:
            await provider.close()
