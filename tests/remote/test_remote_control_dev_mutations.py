"""Explicitly opt-in mutation contract against the isolated control-dev profile.

This module is intentionally separate from the read-only remote suite.  It is
never selected by the normal test target, and it performs every safety check
before constructing a network client.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4

import pytest
import pytest_asyncio

from hermes_client import (
    CapabilitySet,
    HermesAutomation,
    HermesGatewayProvider,
    NormalizedEvent,
    ProviderConnection,
    SessionRoute,
)

from .safety import require_control_dev_mutation


pytestmark = pytest.mark.skipif(
    os.getenv("HERMES_REMOTE_MUTATION_TESTS") != "1",
    reason=(
        "Set HERMES_REMOTE_MUTATION_TESTS=1 and use the dedicated "
        "test-remote-control-dev-mutations target"
    ),
)

_PROFILE = "control-dev"
_OWNED_PREFIX = "hc-test-run-"
_AUDITED_SHA = re.compile(r"^[0-9a-fA-F]{40}$")


@dataclass(slots=True)
class RemoteMutationContext:
    run_id: str
    provider: HermesGatewayProvider
    capabilities: CapabilitySet
    events: asyncio.Queue[NormalizedEvent]
    sessions: dict[str, tuple[SessionRoute, str]] = field(default_factory=dict)
    automations: dict[str, str] = field(default_factory=dict)

    def require_methods(self, *methods: str) -> None:
        missing = sorted(set(methods).difference(self.capabilities.methods))
        if missing:
            pytest.skip(
                "control-dev did not verify the required capabilities: "
                + ", ".join(missing)
            )


def _required_env(name: str, *, label: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.skip(f"{label} is not configured for the control-dev remote suite")
    return value


def _connection(event_sink) -> ProviderConnection:
    # The existing guard checks both the exact profile and the explicit
    # mutation sentinel.  Run it before reading endpoints or constructing a
    # provider so a mistaken active profile can never become a network call.
    profile = os.getenv("HERMES_TEST_PROFILE", "")
    require_control_dev_mutation(profile)
    if os.getenv("HERMES_REMOTE_TESTS") != "1":
        pytest.skip("Set HERMES_REMOTE_TESTS=1 in addition to the mutation opt-in")

    trusted_sha = _required_env(
        "HERMES_CONTROL_HERMES_SOURCE_SHA", label="The independently verified source SHA"
    ).lower()
    if not _AUDITED_SHA.fullmatch(trusted_sha):
        pytest.skip("The control-dev source SHA must be exactly 40 hexadecimal characters")

    api_url = os.getenv("HERMES_CONTROL_HERMES_API_URL", "").strip() or None
    api_key = os.getenv("HERMES_CONTROL_HERMES_API_KEY", "").strip() or None
    if bool(api_url) != bool(api_key):
        pytest.skip("The optional control-dev 8642 URL and API key must be configured together")

    return ProviderConnection(
        gateway_id="remote-control-dev-opt-in",
        profile_name=_PROFILE,
        rest_url=_required_env(
            "HERMES_CONTROL_HERMES_DASHBOARD_URL", label="The Hermes dashboard URL"
        ),
        ws_url=_required_env(
            "HERMES_CONTROL_HERMES_DASHBOARD_WS", label="The Hermes dashboard WebSocket URL"
        ),
        dashboard_token=_required_env(
            "HERMES_CONTROL_HERMES_DASHBOARD_TOKEN", label="The Hermes dashboard token"
        ),
        api_url=api_url,
        api_key=api_key,
        trusted_source_sha=trusted_sha,
    )


async def _wait_for_event(
    context: RemoteMutationContext,
    route: SessionRoute,
    event_types: set[str],
    *,
    timeout: float = 30.0,
) -> NormalizedEvent:
    deadline = monotonic() + timeout
    while True:
        remaining = deadline - monotonic()
        if remaining <= 0:
            pytest.fail(
                "Timed out waiting for control-dev event: "
                + ", ".join(sorted(event_types))
            )
        event = await asyncio.wait_for(context.events.get(), timeout=remaining)
        if event.gateway_id != route.gateway_id or event.profile_name != _PROFILE:
            continue
        if event.stored_session_id not in {None, route.stored_session_id}:
            continue
        if event.runtime_session_id not in {None, route.runtime_session_id}:
            continue
        if event.type in event_types:
            return event


async def _cleanup(context: RemoteMutationContext) -> list[str]:
    failures: list[str] = []
    for automation_id, owned_name in list(context.automations.items()):
        if not owned_name.startswith(_OWNED_PREFIX):
            failures.append(f"refused non-owned cron id {automation_id}")
            continue
        try:
            await context.provider.delete_automation(automation_id)
            context.automations.pop(automation_id, None)
        except Exception as exc:  # pragma: no cover - remote-only diagnostic path
            failures.append(f"cron {automation_id} ({type(exc).__name__})")

    for stored_id, (route, owned_title) in list(context.sessions.items()):
        if not owned_title.startswith(_OWNED_PREFIX):
            failures.append(f"refused non-owned session id {stored_id}")
            continue
        try:
            await context.provider.delete_session(route)
            context.sessions.pop(stored_id, None)
        except Exception as exc:  # pragma: no cover - remote-only diagnostic path
            failures.append(f"session {stored_id} ({type(exc).__name__})")
    return failures


@pytest_asyncio.fixture
async def control_dev() -> RemoteMutationContext:
    events: asyncio.Queue[NormalizedEvent] = asyncio.Queue()

    async def event_sink(event: NormalizedEvent) -> None:
        events.put_nowait(event)

    provider = HermesGatewayProvider(_connection(event_sink), event_sink)
    try:
        try:
            capabilities = await provider.capabilities()
            profiles = await provider.list_profiles()
        except Exception as exc:
            await provider.close()
            pytest.skip(
                "control-dev dashboard/auth API is unavailable "
                f"({type(exc).__name__})"
            )
        if _PROFILE not in {profile.name for profile in profiles}:
            await provider.close()
            pytest.skip("The remote Hermes installation has no control-dev profile")

        context = RemoteMutationContext(
            run_id=f"{_OWNED_PREFIX}{uuid4().hex[:12]}",
            provider=provider,
            capabilities=capabilities,
            events=events,
        )
        yield context
    finally:
        if "context" in locals():
            failures = await _cleanup(context)
            await provider.close()
            if failures:
                pytest.fail(
                    "Remote cleanup needs manual inspection; only this run's IDs "
                    "were attempted: " + "; ".join(failures)
                )


@pytest.mark.asyncio
async def test_control_dev_prompt_stream_interrupt_and_resume(
    control_dev: RemoteMutationContext,
) -> None:
    control_dev.require_methods(
        "session.create",
        "session.resume",
        "session.history",
        "prompt.submit",
        "session.interrupt",
        "session.delete",
    )
    title = f"{control_dev.run_id}-session"
    marker = f"{control_dev.run_id}-prompt"
    session = await control_dev.provider.create_session(title=title)
    route = SessionRoute(
        gateway_id=control_dev.provider.connection.gateway_id,
        profile_name=_PROFILE,
        stored_session_id=session.stored_session_id,
        runtime_session_id=session.runtime_session_id,
    )
    control_dev.sessions[session.stored_session_id] = (route, title)

    generation = control_dev.provider.runtime_generation
    receipt = await control_dev.provider.submit_prompt(
        route,
        (
            f"Remote integration marker: {marker}. Do not call tools. "
            "Write 500 short numbered lines so the client can observe streaming."
        ),
        operation_id=marker,
        expected_runtime_generation=generation,
    )
    assert receipt.operation_id == marker
    assert receipt.status in {"accepted", "streaming", "completed"}

    # Always attempt the interrupt after dispatch, even if a broken remote
    # stream fails to produce a delta before the assertion timeout. This keeps
    # a failing opt-in test from leaving a deliberately long response running.
    try:
        await _wait_for_event(control_dev, route, {"message.delta"})
    finally:
        await control_dev.provider.interrupt(
            route, expected_runtime_generation=generation
        )
    await _wait_for_event(
        control_dev,
        route,
        {
            "message.interrupted",
            "message.cancelled",
            "message.complete",
            "message.completed",
            "message.done",
            "run.completed",
            "run.failed",
        },
    )

    resumed = await control_dev.provider.resume_session(session.stored_session_id)
    assert resumed.stored_session_id == session.stored_session_id
    assert resumed.runtime_session_id
    resumed_route = SessionRoute(
        route.gateway_id,
        route.profile_name,
        resumed.stored_session_id,
        resumed.runtime_session_id,
    )
    control_dev.sessions[session.stored_session_id] = (resumed_route, title)
    history = await control_dev.provider.history(
        resumed_route,
        expected_runtime_generation=control_dev.provider.runtime_generation,
    )
    assert marker in json.dumps(history, ensure_ascii=False, default=str)


@pytest.mark.asyncio
async def test_control_dev_cron_crud(control_dev: RemoteMutationContext) -> None:
    control_dev.require_methods(
        "cron.list", "cron.create", "cron.update", "cron.delete"
    )
    try:
        config = await control_dev.provider.get_config()
        timezone_name = config.data.get("timezone")
    except Exception as exc:
        pytest.skip(f"control-dev config/timezone API is unavailable ({type(exc).__name__})")
    if not isinstance(timezone_name, str) or not timezone_name.strip():
        pytest.skip("control-dev has no configured IANA timezone for cron mutation")

    name = f"{control_dev.run_id}-cron"
    created = await control_dev.provider.create_automation(
        HermesAutomation(
            automation_id="",
            name=name,
            schedule="0 0 1 1 *",
            timezone=timezone_name,
            enabled=False,
            prompt=f"No-op integration marker {control_dev.run_id}",
        )
    )
    assert created.automation_id
    assert created.name == name
    assert created.enabled is False
    control_dev.automations[created.automation_id] = name

    inventory = await control_dev.provider.list_automations()
    assert any(item.automation_id == created.automation_id for item in inventory)

    updated_name = f"{name}-updated"
    updated = await control_dev.provider.update_automation(
        created.automation_id,
        {
            "name": updated_name,
            "schedule": "15 0 1 1 *",
            "timezone": timezone_name,
            "prompt": f"Updated no-op integration marker {control_dev.run_id}",
            "enabled": False,
        },
    )
    assert updated.automation_id == created.automation_id
    assert updated.name == updated_name
    assert updated.enabled is False
    control_dev.automations[created.automation_id] = updated_name

    await control_dev.provider.delete_automation(created.automation_id)
    control_dev.automations.pop(created.automation_id, None)
    remaining = await control_dev.provider.list_automations()
    assert all(item.automation_id != created.automation_id for item in remaining)
