import asyncio
import hashlib
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from hermes_client import InMemoryHermesProvider, ProviderConnection
from hermes_client.connector_protocol import encode_message
from hermes_client.types import SessionRoute, PromptAttachment, PromptReceipt
from agent_control_connector.runtime import ConnectorRuntime
from agent_control_connector.storage import OperationLedger
from agent_control_connector.media import project_media, read_media
from agent_control_connector.cli import cloud_url, local_endpoint, status
from hermes_control_api.remote_provider import ConnectorLink, ConnectorRegistry, RemoteProvider


@pytest.fixture
def runtime(tmp_path):
    result = ConnectorRuntime(tmp_path, {"gatewayId": "gateway", "profiles": ["selected"], "restUrl": "http://127.0.0.1:9119", "wsUrl": "ws://127.0.0.1:9119/api/ws", "sourceSha": "f" * 40, "hermesHome": str(tmp_path)}, {"hermesToken": "secret"}, InMemoryHermesProvider)
    yield result
    result.ledger.close()


def request(operation, args=(), kwargs=None, profile="selected", operation_id="same-operation"):
    return {"v": 1, "type": "request", "id": uuid4().hex, "profile": profile, "operation": operation,
            "args": args, "kwargs": kwargs or {}, "operationId": operation_id}


@pytest.mark.asyncio
async def test_prompt_retry_replays_receipt_without_double_dispatch(runtime):
    provider = runtime.providers["selected"]
    provider.submit_prompt = AsyncMock(return_value=PromptReceipt("same-operation"))
    args = (SessionRoute("gateway", "selected", "stored", "runtime"), "Hello")
    first = await runtime.execute(request("submit_prompt", args, {"operation_id": "same-operation"}))
    second = await runtime.execute(request("submit_prompt", args, {"operation_id": "same-operation"}))
    assert first["result"] == second["result"]
    assert provider.submit_prompt.await_count == 1
    assert first["id"] != second["id"]


@pytest.mark.asyncio
async def test_crashed_operation_stays_unknown_after_restart(tmp_path):
    ledger = OperationLedger(tmp_path)
    message = request("submit_prompt", (SessionRoute("gateway", "selected", "s", "r"), "Hello"), {"operation_id": "same-operation"})
    digest = hashlib.sha256(encode_message({"v": 1, "profile": "selected", "operation": "submit_prompt", "args": message["args"], "kwargs": message["kwargs"]})).hexdigest()
    ledger.reserve("selected:submit_prompt:same-operation", digest)
    ledger.close()
    runtime = ConnectorRuntime(tmp_path, {"gatewayId": "gateway", "profiles": ["selected"], "restUrl": "http://127.0.0.1:9119", "wsUrl": "ws://127.0.0.1:9119/api/ws", "sourceSha": "f"*40, "hermesHome": str(tmp_path)}, {"hermesToken": "secret"}, InMemoryHermesProvider)
    runtime.providers["selected"].submit_prompt = AsyncMock()
    result = await runtime.execute(message)
    assert result["error"] == "PROMPT_DELIVERY_UNKNOWN"
    runtime.providers["selected"].submit_prompt.assert_not_called()
    runtime.ledger.close()


@pytest.mark.asyncio
async def test_foreign_profile_route_and_arbitrary_operation_rejected(runtime):
    assert (await runtime.execute(request("list_sessions", profile="unshared")))["error"] == "INVALID_OPERATION"
    assert (await runtime.execute(request("http_request", ("http://169.254.169.254",))))["error"] == "INVALID_OPERATION"
    foreign = SessionRoute("gateway", "unshared", "stored", "runtime")
    assert (await runtime.execute(request("submit_prompt", (foreign, "Hello"), {"operation_id": "same-operation"})))["error"] == "INVALID_OPERATION"


@pytest.mark.asyncio
async def test_local_profile_discovery_does_not_leak_unshared_profiles(runtime):
    result = await runtime.execute(request("list_profiles"))
    assert all(profile.name == "selected" for profile in result["result"])


@pytest.mark.asyncio
async def test_maintenance_prevents_new_mutation_before_ledger_reservation(runtime, tmp_path):
    (tmp_path / "maintenance.request").write_text("maintenance")
    provider = runtime.providers["selected"]
    provider.create_session = AsyncMock()
    result = await runtime.execute(request("create_session"))
    assert "error" in result
    provider.create_session.assert_not_called()
    assert runtime.ledger.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 0


@pytest.mark.asyncio
async def test_offline_remote_provider_does_not_dispatch():
    registry = ConnectorRegistry(AsyncMock())
    provider = RemoteProvider(ProviderConnection("gateway", "selected", "connector://gateway", "connector://gateway"), registry)
    with pytest.raises(ConnectionError):
        await provider.create_session()


@pytest.mark.asyncio
async def test_response_must_match_requested_profile():
    from hermes_client.connector_protocol import ProtocolError
    link = ConnectorLink("g", frozenset({"alice", "bob"}), AsyncMock(), AsyncMock())
    task = asyncio.create_task(link.call("alice", "list_sessions", (), {}))
    await asyncio.sleep(0)
    identifier = next(iter(link.pending))
    with pytest.raises(ProtocolError):
        link.response({"id": identifier, "profile": "bob", "result": []})
    link.response({"id": identifier, "profile": "alice", "result": []})
    assert await task == []


def test_transcript_bound_audio_and_path_escape_are_rejected(tmp_path):
    root = tmp_path / "profiles/selected/cache/audio"
    root.mkdir(parents=True)
    audio = root / "voice.mp3"
    audio.write_bytes(b"audio")
    secret = tmp_path / "secret.mp3"
    secret.write_bytes(b"secret")
    (root / "escape.mp3").symlink_to(secret)
    history = [{"role": "assistant", "content": f"MEDIA:{audio}\nMEDIA:{root / 'escape.mp3'}"}]
    projected = project_media(history, tmp_path, "selected", "session")
    assert len(projected[0]["controlMedia"]) == 1
    identifier = projected[0]["controlMedia"][0]["id"]
    assert read_media(history, tmp_path, "selected", "session", identifier)["content"] == b"audio"
    with pytest.raises(LookupError):
        read_media(history, tmp_path, "selected", "different-session", identifier)
    with pytest.raises(LookupError):
        read_media([], tmp_path, "selected", "session", identifier)


def test_loopback_only_hermes_and_https_cloud_origin():
    assert local_endpoint("http://127.0.0.1:9119")
    for endpoint in ["http://192.168.1.2", "http://localhost:9119", "http://127.0.0.1?token=secret", "https://example.com"]:
        with pytest.raises(ValueError):
            local_endpoint(endpoint)
    with pytest.raises(ValueError):
        cloud_url("http://example.com")


def test_audio_root_symlinks_cannot_escape_to_another_profile(tmp_path):
    other = tmp_path / "profiles/unshared/cache/audio"
    other.mkdir(parents=True)
    audio = other / "secret.mp3"
    audio.write_bytes(b"secret")
    selected = tmp_path / "profiles/selected"
    selected.mkdir(parents=True)
    (selected / "cache").symlink_to(other.parent, target_is_directory=True)
    history = [{"role":"assistant", "content":f"MEDIA:{selected / 'cache/audio/secret.mp3'}"}]
    assert "controlMedia" not in project_media(history, tmp_path, "selected", "session")[0]
