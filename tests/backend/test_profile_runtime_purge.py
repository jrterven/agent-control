from __future__ import annotations

import pytest

from hermes_client import (
    HermesSessionRouter,
    InMemoryHermesProvider,
    NormalizedEvent,
    ProviderConnection,
    ProviderPool,
    SessionRoute,
)
from hermes_control_api.eventing import EventHub


@pytest.mark.asyncio
async def test_router_profile_purge_keeps_other_agent_routes() -> None:
    pool = ProviderPool(lambda connection: InMemoryHermesProvider(connection))
    router = HermesSessionRouter(pool)
    target_connection = ProviderConnection(
        "gateway-a", "move-me", "http://target", "ws://target"
    )
    other_connection = ProviderConnection(
        "gateway-a", "keep-me", "http://other", "ws://other"
    )
    target_provider = await pool.get(target_connection)
    other_provider = await pool.get(other_connection)
    target_session = await target_provider.create_session(title="Target")
    other_session = await other_provider.create_session(title="Other")

    target_route, _ = await router.submit_prompt(
        route=SessionRoute(
            "gateway-a", "move-me", target_session.stored_session_id
        ),
        connection=target_connection,
        prompt="target prompt",
        idempotency_key="target-operation",
    )
    other_route, _ = await router.submit_prompt(
        route=SessionRoute(
            "gateway-a", "keep-me", other_session.stored_session_id
        ),
        connection=other_connection,
        prompt="other prompt",
        idempotency_key="other-operation",
    )
    router.recovery_lock(target_route)
    router.recovery_lock(other_route)

    assert any(key[:2] == ("gateway-a", "move-me") for key in router._receipts)
    assert any(key[:2] == ("gateway-a", "keep-me") for key in router._receipts)

    router.purge_profile("gateway-a", "move-me")

    for mapping in (
        router._receipts,
        router._validated_runtime,
        router._runtime_owner,
        router._route_locks,
        router._recovery_locks,
    ):
        assert not any(key[:2] == ("gateway-a", "move-me") for key in mapping)
        assert any(key[:2] == ("gateway-a", "keep-me") for key in mapping)
    assert ("gateway-a", "move-me") not in router._owner_generation
    assert ("gateway-a", "keep-me") in router._owner_generation
    await pool.close()


@pytest.mark.asyncio
async def test_event_hub_profile_purge_drains_only_target_events() -> None:
    hub = EventHub()
    subscription = await hub.subscribe("owner-a")

    target = NormalizedEvent.create(
        event_id="target-event",
        type="approval.request",
        gateway_id="gateway-a",
        profile_name="move-me",
        stored_session_id="stored-target",
        runtime_session_id="runtime-target",
        runtime_generation="generation-target",
        sequence=1,
        replay_epoch="epoch-target",
        data={"request_id": "approval-target", "choices": ["deny"]},
    )
    other = NormalizedEvent.create(
        event_id="other-event",
        type="run.completed",
        gateway_id="gateway-a",
        profile_name="keep-me",
        stored_session_id="stored-other",
        runtime_session_id="runtime-other",
        runtime_generation="generation-other",
        sequence=1,
        replay_epoch="epoch-other",
        data={"run_id": "run-other", "job_id": "job-other"},
    )
    target_run = NormalizedEvent.create(
        event_id="target-run",
        type="run.completed",
        gateway_id="gateway-a",
        profile_name="move-me",
        data={"run_id": "run-target", "job_id": "job-target"},
    )
    hub.remember_correlation(target_run)
    hub.remember_correlation(other)
    await hub.publish(target)
    await hub.publish(other)

    assert subscription.queue.qsize() == 2
    assert subscription.queued_bytes > 0
    await hub.purge_profile("gateway-a", "move-me")

    assert not any(key[:2] == ("gateway-a", "move-me") for key in hub._replay)
    assert not any(
        hub._split_route_key(key)[:2] == ("gateway-a", "move-me")
        for key in hub._buffers
    )
    assert not any(
        key[:2] == ("gateway-a", "move-me") for key in hub._correlated_runs
    )
    assert not any(key[:2] == ("gateway-a", "move-me") for key in hub._interactions)
    assert any(key[:2] == ("gateway-a", "keep-me") for key in hub._replay)
    assert subscription.queue.qsize() == 1

    retained = await hub.next_event(subscription)
    assert retained["eventId"] == "other-event"
    assert subscription.queued_bytes == 0
    await hub.unsubscribe(subscription)
