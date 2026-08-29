from __future__ import annotations

import os

import pytest

from hermes_client import HermesGatewayProvider, ProviderConnection


pytestmark = pytest.mark.skipif(
    os.getenv("HERMES_REMOTE_TESTS") != "1",
    reason="Set HERMES_REMOTE_TESTS=1 to enable read-only remote probes",
)


def remote_connection(profile_name: str) -> ProviderConnection:
    dashboard_url = os.getenv("HERMES_CONTROL_HERMES_DASHBOARD_URL")
    dashboard_ws = os.getenv("HERMES_CONTROL_HERMES_DASHBOARD_WS")
    if not dashboard_url or not dashboard_ws:
        pytest.skip("Remote dashboard URL/WS variables are not configured")
    return ProviderConnection(
        gateway_id="remote-opt-in",
        profile_name=profile_name,
        rest_url=dashboard_url,
        ws_url=dashboard_ws,
        dashboard_token=os.getenv("HERMES_CONTROL_HERMES_DASHBOARD_TOKEN"),
        api_url=(
            os.getenv("HERMES_CONTROL_HERMES_API_URL")
            if profile_name == "control-dev"
            else None
        ),
        api_key=(
            os.getenv("HERMES_CONTROL_HERMES_API_KEY")
            if profile_name == "control-dev"
            else None
        ),
    )


@pytest.mark.asyncio
async def test_remote_profile_inventory_is_read_only() -> None:
    provider = HermesGatewayProvider(remote_connection("default"))
    try:
        profiles = await provider.list_profiles()
        technical_names = {profile.name for profile in profiles}
        assert {"default", "jarvis"}.issubset(technical_names)
    finally:
        await provider.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_name", ["default", "jarvis"])
async def test_newton_and_jarvis_read_only_contract(profile_name: str) -> None:
    # capabilities() and list_sessions() perform only GET/list/ping probes. This
    # suite never calls create/resume/prompt/interrupt/cron/config/delete APIs.
    provider = HermesGatewayProvider(remote_connection(profile_name))
    try:
        capabilities = await provider.capabilities()
        sessions = await provider.list_sessions()
        assert capabilities.version
        assert capabilities.protocol in {"dashboard-jsonrpc", "openai-compatible"}
        assert all(session.stored_session_id for session in sessions)
    finally:
        await provider.close()
