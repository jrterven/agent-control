"""Cloud traffic is isolated before it can consume another account's queue."""
from __future__ import annotations

import pytest

from hermes_client import NormalizedEvent
from hermes_control_api.eventing import EventHub


def event(owner: str, number: int = 0) -> NormalizedEvent:
    return NormalizedEvent.create(
        event_id=f"{owner}-{number}", type="message.delta", gateway_id=f"gateway-{owner}",
        profile_name="default", stored_session_id="same-local-session-name",
        runtime_generation=f"generation-{owner}", data={"delta": f"private text for {owner}: {number}"},
    )


@pytest.mark.asyncio
async def test_cloud_publish_only_queues_events_for_the_resolved_gateway_owner():
    hub = EventHub(queue_size=4)
    owner_phone = await hub.subscribe("alice")
    owner_desktop = await hub.subscribe("alice")
    other_user = await hub.subscribe("bob")

    await hub.publish(event("alice"), recipient_user_id="alice")

    assert other_user.queue.empty()
    assert other_user.queued_bytes == 0
    assert (await hub.next_event(owner_phone))["data"]["delta"] == "private text for alice: 0"
    assert (await hub.next_event(owner_desktop))["data"]["delta"] == "private text for alice: 0"
    assert owner_phone.queued_bytes == owner_desktop.queued_bytes == 0


@pytest.mark.asyncio
async def test_foreign_event_flood_cannot_evict_queued_owner_events_or_create_overflow():
    hub = EventHub(queue_size=2)
    alice = await hub.subscribe("alice")
    bob = await hub.subscribe("bob")
    await hub.publish(event("bob"), recipient_user_id="bob")
    original_bytes = bob.queued_bytes

    for number in range(25):
        await hub.publish(event("alice", number), recipient_user_id="alice")

    assert alice.queue.qsize() > 0
    assert bob.queue.qsize() == 1
    assert bob.queued_bytes == original_bytes
    delivered = await hub.next_event(bob)
    assert delivered["type"] == "message.delta"
    assert delivered["gatewayId"] == "gateway-bob"
    assert delivered["data"]["delta"] == "private text for bob: 0"
    assert bob.queue.empty()


@pytest.mark.asyncio
async def test_private_deployments_preserve_unscoped_event_fanout():
    hub = EventHub()
    alice = await hub.subscribe("alice")
    bob = await hub.subscribe("bob")
    await hub.publish(event("private"))
    assert (await hub.next_event(alice))["gatewayId"] == "gateway-private"
    assert (await hub.next_event(bob))["gatewayId"] == "gateway-private"


def test_remote_media_is_owner_scoped_and_never_cacheable(cloud, monkeypatch):
    from .test_cloud_accounts import seed_tenants
    from hermes_control_api.services import SessionMediaAsset, SessionService
    from unittest.mock import AsyncMock

    app, client, _ = cloud
    alice, bob = seed_tenants(app)
    media_id = "a" * 32
    media = AsyncMock(return_value=SessionMediaAsset(media_id=media_id, path=None, media_type="audio/mpeg", content=b"owned-voice-note"))
    monkeypatch.setattr(SessionService, "media", media)
    client.cookies.set("hc_session", alice[4])

    denied = client.get(f"/api/v1/sessions/{bob[3]}/media/{media_id}")
    assert denied.status_code == 404
    media.assert_not_awaited()
    allowed = client.get(f"/api/v1/sessions/{alice[3]}/media/{media_id}")
    assert allowed.status_code == 200
    assert allowed.content == b"owned-voice-note"
    assert "no-store" in allowed.headers["cache-control"]
    assert allowed.headers["x-content-type-options"] == "nosniff"
    assert allowed.headers["content-disposition"] == 'inline; filename="voice-note"'
    media.assert_awaited_once()


# Reuse the standalone cloud installation fixture; no test relies on private
# singleton gateways or environment credentials.
from .test_cloud_accounts import cloud  # noqa: E402,F401


def test_research_only_connector_refresh_uses_approved_discovery_profile(cloud, monkeypatch):
    from .test_cloud_accounts import seed_tenants
    from hermes_client import CapabilitySet
    from hermes_client.types import HermesProfile
    from hermes_control_api.connector_models import Connector
    from hermes_control_api.models import ProfileRef
    from sqlalchemy import select

    app, client, _ = cloud
    alice, _bob = seed_tenants(app)
    with app.state.session_factory() as db:
        existing = db.get(ProfileRef, alice[2])
        existing.profile_name = "research"
        existing.display_name = "Research"
        db.add(Connector(owner_id=alice[0], gateway_id=alice[1], name="Research Mac",
                         profiles=["research"], token_hash="c" * 64, version="0.1.0"))
        db.commit()
    observed_profiles = []

    class ResearchProvider:
        async def list_profiles(self):
            # A compromised or incorrectly scoped discovery response must not
            # make an unapproved local profile visible in the cloud account.
            return [HermesProfile("research", "Research"), HermesProfile("default", "Private default")]

        async def capabilities(self):
            return CapabilitySet(methods=frozenset({"profiles.list", "sessions.list"}))

    async def provider(connection):
        observed_profiles.append(connection.profile_name)
        assert connection.profile_name == "research", "must not contact an unshared default profile"
        return ResearchProvider()

    monkeypatch.setattr(app.state.services.provider_pool, "get", provider)
    client.cookies.set("hc_session", alice[4])
    response = client.post(f"/api/v1/profiles/refresh?gatewayId={alice[1]}",
                          headers={"X-CSRF-Token": alice[5], "Idempotency-Key": "research-discovery"})
    assert response.status_code == 200, response.text
    assert observed_profiles and set(observed_profiles) == {"research"}
    assert [profile["profileName"] for profile in response.json()] == ["research"]
    with app.state.session_factory() as db:
        names = db.scalars(select(ProfileRef.profile_name).where(ProfileRef.gateway_id == alice[1])).all()
        assert names == ["research"]
