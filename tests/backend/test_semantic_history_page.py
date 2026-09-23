from unittest.mock import AsyncMock

import pytest

from hermes_client import HermesGatewayProvider, ProviderConnection
from hermes_client.connector_protocol import READ_OPERATIONS, WRITE_OPERATIONS, decode_message, encode_message, validate_arguments
from hermes_client.limits import UpstreamPayloadTooLarge


@pytest.mark.asyncio
async def test_readonly_page_passes_profile_offset_without_creating_runtime(monkeypatch):
    provider = HermesGatewayProvider(ProviderConnection(gateway_id="g", profile_name="default",
        rest_url="http://127.0.0.1:19119", ws_url="ws://127.0.0.1:19119/api/ws"))
    request = AsyncMock(return_value={"messages": [{"id": "5001", "role": "user", "content": "older history"}]})
    monkeypatch.setattr("hermes_client.provider.bounded_json_request", request)
    provider.resume_session = AsyncMock(side_effect=AssertionError("Must not resume"))
    try:
        page = await provider.history_page("stored/one", offset=5000, limit=100)
        assert page["next_offset"] == 5001 and page["complete"] is True
        assert request.call_args.args[2] == "/api/sessions/stored%2Fone/messages"
        assert request.call_args.kwargs["params"] == {"profile": "default", "offset": 5000, "limit": 100, "order": "oldest"}
        assert not provider.resume_session.called
        with pytest.raises(ValueError):
            await provider.history_page("ac_tmp_private")
        with pytest.raises(ValueError):
            await provider.history_page("stored", offset=-1)
    finally:
        await provider.close()


def test_history_page_is_typed_readonly_connector_operation():
    assert "history_page" in READ_OPERATIONS and "history_page" not in WRITE_OPERATIONS
    validate_arguments("history_page", ("session",), {"offset": 5000, "limit": 100})
    page = {"v": 1, "result": {"messages": [{"role": "user", "content": "text"}], "next_offset": 5001, "complete": True}}
    assert decode_message(encode_message(page)) == page


@pytest.mark.asyncio
async def test_large_history_pages_shrink_without_skipping_content(monkeypatch):
    provider = HermesGatewayProvider(ProviderConnection(gateway_id="g", profile_name="default",
        rest_url="http://127.0.0.1:19119", ws_url="ws://127.0.0.1:19119/api/ws"))
    request = AsyncMock(side_effect=[UpstreamPayloadTooLarge("large page"),
        {"messages": [{"role": "user", "content": "text"}] * 50}])
    monkeypatch.setattr("hermes_client.provider.bounded_json_request", request)
    try:
        page = await provider.history_page("stored", offset=5000)
        assert page["next_offset"] == 5050 and page["complete"] is False
        assert [call.kwargs["params"]["offset"] for call in request.call_args_list] == [5000, 5000]
        assert [call.kwargs["params"]["limit"] for call in request.call_args_list] == [100, 50]
        assert all(call.kwargs["max_bytes"] == 1024 * 1024 for call in request.call_args_list)
    finally:
        await provider.close()
