from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest

import hermes_client.provider as provider_module
from hermes_client import HermesAutomation, HermesGatewayProvider, ProviderConnection, SessionRoute
from hermes_client.compatibility import HERMES_0212_SHA, PROFILE_TRANSFER_PAIRS
from hermes_client.history import project_history_message
from hermes_control_api.api.routes import public_capability_flags
from hermes_control_api.services import SessionService


def connection():
    return ProviderConnection(
        gateway_id="test", profile_name="control-dev",
        rest_url="http://127.0.0.1:19119", ws_url="ws://127.0.0.1:19119/api/ws",
        trusted_source_sha=HERMES_0212_SHA,
    )


@pytest.mark.parametrize("version,allowed", [("0.21.2", True), ("0.21.1", False), ("0.20.6", False)])
def test_new_lifecycle_contract_requires_matching_version(version, allowed):
    flags = public_capability_flags(
        {"version": version, "methods": ["profiles.delete", "profiles.transfer"], "features": []},
        profile_name="control-dev", mutable_profiles=["control-dev"],
        trusted_source_sha_configured=True, trusted_source_sha=HERMES_0212_SHA,
    )
    assert flags["profileDelete"] is allowed
    assert flags["profileTransfer"] is allowed
    assert all(left == right for left, right in PROFILE_TRANSFER_PAIRS)


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule", ["30 8 * * FRI", "30 8 * * MON-FRI", "0 16 * * FRI"])
async def test_new_cron_is_created_paused_atomically(monkeypatch, schedule):
    provider = HermesGatewayProvider(connection())
    calls = []
    monkeypatch.setattr(provider, "_assert_automation_timezone", AsyncMock(return_value="UTC"))

    async def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        body = kwargs["json"]
        assert body["schedule"] == schedule
        assert body["paused"] is True
        return {"id": "job", "name": "Paused", "schedule": {"kind": "cron", "expr": schedule}, "enabled": False, "state": "paused"}

    monkeypatch.setattr(provider, "_cron_mutation", request)
    try:
        result = await provider.create_automation(HermesAutomation(
            automation_id="", name="Paused", schedule=schedule, timezone="UTC", enabled=False, prompt="No-op",
        ))
        assert result.enabled is False
        assert len(calls) == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_session_delete_selects_the_owned_profile(monkeypatch):
    provider = HermesGatewayProvider(connection())
    calls = []

    async def request(client, method, path, **kwargs):
        calls.append((method, path, kwargs))
        assert kwargs["params"] == {"profile": "control-dev"}

    monkeypatch.setattr(provider_module, "bounded_empty_request", request)
    try:
        await provider.delete_session(SessionRoute("test", "control-dev", "stored-id", "runtime"))
        assert len(calls) == 1
    finally:
        await provider.close()


@pytest.mark.asyncio
async def test_partially_saved_cron_is_not_treated_as_a_rejected_create(monkeypatch):
    provider = HermesGatewayProvider(connection())
    request = httpx.Request("POST", "http://127.0.0.1:19119/api/cron/jobs")
    response = httpx.Response(424, request=request)
    monkeypatch.setattr(provider_module, "bounded_json_request", AsyncMock(
        side_effect=httpx.HTTPStatusError("registration failed", request=request, response=response),
    ))
    try:
        with pytest.raises(RuntimeError, match="MUTATION_DELIVERY_UNKNOWN"):
            await provider._cron_mutation("POST", "/api/cron/jobs", json={})
        assert provider_module.bounded_json_request.await_count == 1
    finally:
        await provider.close()


def sidecar(phase, text):
    return {"type": "message", "role": "assistant", "phase": phase,
            "content": [{"type": "output_text", "text": text}]}


@pytest.mark.parametrize("encode", [lambda value: value, json.dumps])
def test_history_recovers_final_response_and_removes_private_sidecars(encode):
    raw = {"role": "assistant", "content": "", "codex_message_items": encode([
        sidecar("analysis", "private analysis"), sidecar("commentary", "private narration"),
        sidecar("final_answer", "Visible reply"),
    ]), "codex_reasoning_items": [{"text": "private"}]}
    projected = project_history_message(raw)
    assert projected == {"role": "assistant", "content": "Visible reply"}
    assert SessionService._is_terminal_assistant_message(raw)
    assert "codex_message_items" in raw  # The upstream row is never mutated.


@pytest.mark.parametrize("items", ["bad JSON", {}, [], [sidecar("analysis", "private")],
                                   [{**sidecar("final_answer", "unfinished"), "status": "in_progress"}],
                                   [{**sidecar(None, ""), "content": [{"type": [], "text": "invalid"}]}],
                                   [sidecar("final_answer", "x" * 65_537)]])
def test_invalid_or_private_sidecars_do_not_complete_a_prompt(items):
    raw = {"role": "assistant", "text": "", "codex_message_items": items}
    assert project_history_message(raw) == {"role": "assistant", "text": ""}
    assert not SessionService._is_terminal_assistant_message(raw)


def test_existing_public_text_is_not_duplicated():
    raw = {"role": "assistant", "content": "Visible", "codex_message_items": [sidecar(None, "Visible")]}
    assert project_history_message(raw) == {"role": "assistant", "content": "Visible"}
