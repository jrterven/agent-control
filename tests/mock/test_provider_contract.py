from __future__ import annotations

import asyncio
import socket

import pytest
import uvicorn

from hermes_client import HermesAutomation, HermesGatewayProvider, ProviderConnection, SessionRoute
from mock_hermes import MockHermesState, create_dashboard_app


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_real_provider_adapter_against_network_mock() -> None:
    try:
        port = free_loopback_port()
    except PermissionError:
        pytest.skip("sandbox does not allow binding a loopback test server")
    asyncio.run(_exercise_provider_contract(port))


async def _exercise_provider_contract(port: int) -> None:
    app = create_dashboard_app(MockHermesState())
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
        )
    )
    server_task = asyncio.create_task(server.serve())
    provider: HermesGatewayProvider | None = None
    try:
        for _ in range(200):
            if server.started:
                break
            if server_task.done():
                await server_task
            await asyncio.sleep(0.01)
        assert server.started

        events = []

        async def collect(event):  # type: ignore[no-untyped-def]
            events.append(event)

        provider = HermesGatewayProvider(
            ProviderConnection(
                gateway_id="mock-gateway",
                profile_name="control-dev",
                rest_url=f"http://127.0.0.1:{port}",
                ws_url=f"ws://127.0.0.1:{port}/api/ws",
                dashboard_token="mock-dashboard-token",
            ),
            collect,
        )

        capabilities = await provider.capabilities()
        assert capabilities.version == "0.20.6"
        config = await provider.get_config()
        assert config.data["auxiliary"]["vision"] == {"model": "mock-vision"}
        profiles = await provider.list_profiles()
        assert {profile.name for profile in profiles} == {"default", "jarvis", "control-dev"}

        created = await provider.create_session(title="Provider contract")
        assert created.runtime_session_id
        route = SessionRoute(
            gateway_id="mock-gateway",
            profile_name="control-dev",
            stored_session_id=created.stored_session_id,
            runtime_session_id=created.runtime_session_id,
        )
        receipt = await provider.submit_prompt(route, "contract prompt", operation_id="op-0001")
        assert receipt.status == "streaming"
        for _ in range(200):
            if any(event.type == "message.complete" for event in events):
                break
            await asyncio.sleep(0.01)
        assert any(event.type == "message.complete" for event in events)
        assert all(
            event.correlation_id == "op-0001"
            for event in events
            if event.type.startswith(("message.", "tool.", "approval."))
        )

        sessions = await provider.list_sessions()
        assert sessions[0].stored_session_id == created.stored_session_id
        resumed = await provider.resume_session(created.stored_session_id)
        assert resumed.runtime_session_id != created.runtime_session_id

        automation = await provider.create_automation(
            HermesAutomation(
                "",
                "Contract cron",
                "0 9 * * 1",
                "America/Mexico_City",
                True,
                "mock",
            )
        )
        assert automation.name == "Contract cron"
        assert (await provider.list_automations())[0].automation_id == automation.automation_id
        run = await provider.trigger_automation(automation.automation_id)
        assert run.run_id and run.run_id.startswith("stored-")
        assert run.status == "completed"
        assert run.stored_session_id
    finally:
        if provider is not None:
            await provider.close()
        server.should_exit = True
        await asyncio.wait_for(server_task, timeout=3)
