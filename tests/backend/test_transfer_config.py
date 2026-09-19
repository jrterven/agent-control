from __future__ import annotations

import ast
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from hermes_client import HermesGatewayProvider, InMemoryHermesProvider, ProviderConnection
from hermes_client.admin import AdminResourceSnapshot, contains_secret_fields
from hermes_client.connector_protocol import (
    READ_OPERATIONS, WRITE_OPERATIONS, decode_message, encode_message, type_hints,
    validate_arguments, value_matches_type,
)
from hermes_client.limits import MAX_UPSTREAM_STRING_BYTES, UpstreamPayloadError
from hermes_client.provider import HermesProvider
from hermes_client.transfer_config import parse_transfer_config
from hermes_control_api.providers import FailoverProvider


def native_helpers():
    path = Path(__file__).parents[1] / "fixtures/hermes_939e_config.py"
    module = ast.parse(path.read_text())
    functions = [node for node in module.body if isinstance(node, ast.FunctionDef)]
    namespace = {"Dict": dict, "Any": object}
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), namespace)
    return namespace["_normalize_config_for_web"], namespace["_main_model_fields"]


def connection():
    return ProviderConnection(gateway_id="transfer-test", profile_name="control-dev",
        rest_url="http://hermes.test", ws_url="ws://hermes.test/api/ws",
        dashboard_token="local-test-token")


async def transport(provider, handler):
    await provider.http.aclose()
    provider.http = httpx.AsyncClient(base_url=provider.connection.rest_url,
        transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_raw_transfer_roundtrip_preserves_native_model_routing_and_leaves_ui_normalized():
    normalize, main_model_fields = native_helpers()
    original = {
        "model": {"default": "test-model", "provider": "openai-codex",
                  "base_url": "https://inference.example/v1", "api_mode": "responses",
                  "context_length": 64000, "key_env": "LOCAL_PROVIDER_KEY"},
        "security": {"redact_secrets": True},
        "terminal": {"cwd": "/source/workspace"},
    }
    stored = json.loads(json.dumps(original))
    provider = HermesGatewayProvider(connection())
    calls = []

    async def handler(request):
        nonlocal stored
        calls.append((request.method, request.url.path))
        assert request.url.params["profile"] == "control-dev"
        if request.url.path == "/api/config":
            return httpx.Response(200, json=normalize(stored))
        assert request.url.path == "/api/config/raw"
        if request.method == "GET":
            return httpx.Response(200, json={"yaml": yaml.safe_dump(stored), "path": "/private/config.yaml"})
        assert request.method == "PUT"
        payload = json.loads(await request.aread())
        assert payload["profile"] == "control-dev"
        stored = yaml.safe_load(payload["yaml_text"])
        return httpx.Response(200, json={"ok": True})

    await transport(provider, handler)
    try:
        ui = await provider.get_config()
        assert ui.data["model"] == "test-model"
        assert ui.data["model_context_length"] == 64000
        # Reproduces the former normalized -> raw replacement regression.
        assert main_model_fields(ui.data["model"])[1] == ""
        assert normalize(ui.data)["model_context_length"] == 0
        snapshot = await provider.get_transfer_config()
        assert snapshot.data == original
        await provider.replace_config(snapshot.data)
        assert (await provider.get_transfer_config()).data == original
        assert stored == original
        assert main_model_fields(stored["model"]) == ("test-model", "openai-codex")
    finally:
        await provider.close()
    assert calls == [("GET", "/api/config"), ("GET", "/api/config/raw"),
                     ("PUT", "/api/config/raw"), ("GET", "/api/config/raw")]


@pytest.mark.asyncio
async def test_transfer_config_removes_credentials_locally_before_wire_encoding():
    provider = HermesGatewayProvider(connection())
    secret = "PRIVATE_VALUE_NEVER_ON_CLOUD_WIRE"
    document = {"model": {"provider": "custom", "default": "model", "api_key": secret,
                           "key_env": "LOCAL_PROVIDER_KEY"},
                "env": {"LOCAL_PROVIDER_KEY": secret}, "auth": {"value": secret},
                "headers": {"Authorization": secret}, "refreshToken": secret,
                "security": {"redact_secrets": False}}

    async def handler(request):
        assert request.method == "GET" and request.url.path == "/api/config/raw"
        return httpx.Response(200, json={"yaml": yaml.safe_dump(document), "path": "/private/" + secret})

    await transport(provider, handler)
    try:
        snapshot = await provider.get_transfer_config()
    finally:
        await provider.close()
    assert snapshot.data == {"model": {"provider": "custom", "default": "model", "key_env": "LOCAL_PROVIDER_KEY"},
                             "security": {"redact_secrets": False}}
    assert not contains_secret_fields(snapshot.data)
    wire = encode_message({"v": 1, "result": snapshot})
    assert secret.encode() not in wire and b"/private/" not in wire
    assert decode_message(wire)["result"] == snapshot


@pytest.mark.parametrize("document", [
    "model: [broken", "- list", "null", "text", "a: 1\na: 2", "1: nonstring-key",
    "a: !!python/object/apply:builtins.str [private]", "a: !!binary cHJpdmF0ZQ==",
    "a: 2026-09-19", "a: .inf", "a: .nan", "a: &cycle [*cycle]",
    "a: &base {value: 1}\nb: *base", "a: " + "[" * 30 + "0" + "]" * 30,
    "security: {redact_secrets: PRIVATE_VALUE_NEVER_ON_CLOUD_WIRE}",
    "a: " + "x" * (MAX_UPSTREAM_STRING_BYTES + 1),
    "a: " + "9" * 5000, "a: \ud800",
    "items: [" + ",".join("0" for _ in range(20001)) + "]",
])
def test_raw_config_rejects_unsafe_or_nonmapping_yaml_without_quoting_private_content(document):
    with pytest.raises(UpstreamPayloadError) as error:
        parse_transfer_config(document)
    assert "PRIVATE_VALUE_NEVER_ON_CLOUD_WIRE" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, [], {}, {"yaml": None}, {"yaml": 3}, {"yaml": []}])
async def test_transfer_read_requires_bounded_raw_response_shape(response):
    provider = HermesGatewayProvider(connection())
    await transport(provider, lambda request: httpx.Response(200, json=response))
    try:
        with pytest.raises(UpstreamPayloadError):
            await provider.get_transfer_config()
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_in_memory_transfer_snapshot_and_protocol_match_real_return_contract():
    provider = InMemoryHermesProvider(connection())
    config = {"model": {"provider": "mock", "default": "model", "api_mode": "responses"},
              "security": {"redact_secrets": True}}
    await provider.replace_config(config)
    snapshot = await provider.get_transfer_config()
    assert isinstance(snapshot, AdminResourceSnapshot) and snapshot.resource == "config"
    assert snapshot.data == config
    assert "get_transfer_config" in READ_OPERATIONS
    assert "get_transfer_config" not in WRITE_OPERATIONS
    validate_arguments("get_transfer_config", (), {})
    assert value_matches_type(snapshot, type_hints(HermesProvider.get_transfer_config)["return"])
    with pytest.raises(TypeError):
        validate_arguments("get_transfer_config", ("other-profile",), {})


@pytest.mark.asyncio
async def test_transfer_raw_config_never_uses_mock_fallback_on_real_connection_failure():
    real = AsyncMock(spec=HermesGatewayProvider)
    real.connection = connection()
    fallback = InMemoryHermesProvider(connection())
    fallback.get_transfer_config = AsyncMock()
    provider = FailoverProvider(real, fallback, allow_fallback=True)
    snapshot = AdminResourceSnapshot(resource="config", data={"model": {"default": "real", "provider": "real"}})
    real.get_transfer_config.return_value = snapshot
    assert await provider.get_transfer_config() is snapshot
    real.get_transfer_config.side_effect = ConnectionError("Unavailable")
    with pytest.raises(ConnectionError, match="Unavailable"):
        await provider.get_transfer_config()
    fallback.get_transfer_config.assert_not_called()
    provider.active = fallback
    with pytest.raises(ConnectionError, match="read-only"):
        await provider.get_transfer_config()
    fallback.get_transfer_config.assert_not_called()
