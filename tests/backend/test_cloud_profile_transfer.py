"""Cloud lifecycle integration at the authenticated HTTP boundary.

Only the native Hermes transport is replaced by a deterministic provider. The
real account scope, connector records, lifecycle service, CSRF and idempotency
middleware remain active; transfer chunk framing has separate protocol tests.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from hermes_client import InMemoryHermesProvider, ProviderConnection
from hermes_client.types import HermesAutomation, HermesProfile, HermesSession
from hermes_control_api.connector_models import Connector
from hermes_control_api.models import AuditEvent, Gateway, GatewayCredential, ProfileRef, SessionLink, VisualMedia, utc_now
from hermes_control_api.remote_provider import ConnectorLink, ProfileTransferNotImported, ProfileTransferOutcomeUnknown
from hermes_control_api.visual_media import VisualMediaService

from .conftest import mutation_headers
from .test_cloud_accounts import cloud, seed_tenants  # noqa: F401
from .test_visual_media import FakeStore, picture


HERMES_SHA = "939e45c91d751fadd94dcd1b873ac3cb44846213"
TRANSFER_FEATURE = "connector.profileTransferV2"


@pytest.fixture
def cloud_transfer(cloud):
    app, client, settings = cloud
    alice, bob = seed_tenants(app)
    client.cookies.set("hc_session", alice[4])
    native_calls = []
    providers = {}
    older_connectors = set()
    transfer_fault = {}

    class NativeTransferFixture(InMemoryHermesProvider):
        async def capabilities(self):
            capabilities = await super().capabilities()
            if self.connection.gateway_id in older_connectors:
                return capabilities
            return replace(capabilities, features=capabilities.features | {TRANSFER_FEATURE})

        async def transfer_profile_to(self, destination, *, name, operation_id=None):
            native_calls.append(("transfer", self.connection.gateway_id, destination.connection.gateway_id, name))
            if transfer_fault.get("kind") == "collision":
                # The native profile appeared after cloud preflight. This copy
                # belongs to a different operation and must never be rolled back.
                destination._created_profiles[name] = HermesProfile(name=name, display_name="Unrelated native profile")
                raise ProfileTransferNotImported("Profile appeared before import")
            imported = await super().transfer_profile_to(destination, name=name)
            source = factory(replace(self.connection, profile_name=name))
            target = factory(replace(destination.connection, profile_name=name))
            for field in ("_sessions", "_messages", "_automations", "_automation_runs", "_config", "_model", "_soul"):
                setattr(target, field, deepcopy(getattr(source, field)))
            if transfer_fault.get("kind") == "unknown":
                raise ProfileTransferOutcomeUnknown("Import response was lost")
            return imported

        async def delete_profile(self, name):
            native_calls.append(("delete", self.connection.gateway_id, name))
            await super().delete_profile(name)

    def factory(connection):
        key = (connection.gateway_id, connection.profile_name)
        if key not in providers:
            providers[key] = NativeTransferFixture(connection)
        return providers[key]

    app.state.services.provider_pool.factory = factory
    with app.state.session_factory() as db:
        source = db.get(ProfileRef, alice[2])
        source.profile_name = "control-dev"
        source.display_name = "Control Dev"
        source.managed_by_control = True
        db.add(ProfileRef(gateway_id=alice[1], profile_name="default", display_name="Source manager"))
        source.avatar_mime_type = "image/png"
        source.avatar_data = b"preserved-avatar"
        session = db.get(SessionLink, alice[3])
        session.profile_name = "control-dev"
        session.status = "idle"
        session.runtime_session_id = "old-runtime"
        source_stored_session_id = session.stored_session_id
        destination = Gateway(
            name="alice-destination", owner_id=alice[0], transport_kind="connector",
            rest_url="connector://alice-destination", ws_url="connector://alice-destination",
            health_status="online", last_health_at=utc_now(),
        )
        db.add(destination)
        db.flush()
        destination_id = destination.id
        db.add(ProfileRef(gateway_id=destination_id, profile_name="default", display_name="Destination"))
        connector_ids = {}
        for gateway_id, owner_id, profiles in (
            (alice[1], alice[0], ["default", "control-dev"]),
            (destination_id, alice[0], ["default"]),
            (bob[1], bob[0], ["personal"]),
        ):
            connector = Connector(
                owner_id=owner_id, gateway_id=gateway_id, name=f"Device {gateway_id}",
                token_hash=gateway_id.replace("-", "").ljust(64, "0"), profiles=profiles,
                version="test-transfer-v1", last_seen_at=utc_now(),
            )
            db.add(connector)
            db.add(GatewayCredential(
                gateway_id=gateway_id,
                trusted_source_sha_ciphertext=app.state.services.vault.encrypt(
                    HERMES_SHA, aad=f"gateway:{gateway_id}:source-sha",
                ),
            ))
            db.flush()
            connector_ids[gateway_id] = connector.id
        db.commit()

    async def no_socket(*args):
        pass

    for gateway_id, profile_names in (
        (alice[1], ["default", "control-dev"]), (destination_id, ["default"]), (bob[1], ["personal"]),
    ):
        app.state.connector_registry.links[gateway_id] = ConnectorLink(
            gateway_id, frozenset(profile_names), no_socket, no_socket,
        )
    source_provider = factory(ProviderConnection(
        gateway_id=alice[1], profile_name="control-dev", rest_url=f"connector://{alice[1]}",
        ws_url=f"connector://{alice[1]}", trusted_source_sha=HERMES_SHA,
    ))
    source_manager = factory(replace(source_provider.connection, profile_name="default"))
    source_manager._created_profiles["control-dev"] = HermesProfile(name="control-dev", display_name="Control Dev")
    source_provider._sessions[source_stored_session_id] = HermesSession(
        stored_session_id=source_stored_session_id, runtime_session_id="old-runtime", title="Preserved conversation",
    )
    source_provider._messages[source_stored_session_id] = [{"id": "history-1", "role": "user", "content": "Preserve this history"}]
    yield SimpleNamespace(
        app=app, client=client, settings=settings, alice=alice, bob=bob,
        destination_id=destination_id, connector_ids=connector_ids, native_calls=native_calls,
        older_connectors=older_connectors, source_provider=source_provider,
        transfer_fault=transfer_fault, providers=providers,
    )


def move(fixture, *, profile_id=None, destination_id=None, confirmation="control-dev", key="cloud-move", csrf=None):
    return fixture.client.post(
        f"/api/v1/profiles/{profile_id or fixture.alice[2]}/move",
        headers=mutation_headers(csrf or fixture.alice[5], key),
        json={"destinationGatewayId": destination_id or fixture.destination_id, "confirmation": confirmation},
    )


def assert_source_unchanged(fixture):
    assert fixture.native_calls == []
    with fixture.app.state.session_factory() as db:
        assert db.get(ProfileRef, fixture.alice[2]).gateway_id == fixture.alice[1]
        assert db.get(SessionLink, fixture.alice[3]).gateway_id == fixture.alice[1]


def test_cloud_non_admin_can_move_own_agent_and_replay_preserves_route_and_ownership(cloud_transfer):
    fixture = cloud_transfer
    assert fixture.client.get("/api/v1/auth/me").json()["isAdmin"] is False
    moved = move(fixture)
    assert moved.status_code == 200, moved.text
    assert moved.json()["profileId"] == fixture.alice[2]
    assert moved.json()["sourceGatewayId"] == fixture.alice[1]
    assert moved.json()["destinationGatewayId"] == fixture.destination_id
    assert moved.json()["status"] == "moved"
    calls = list(fixture.native_calls)
    replay = move(fixture)
    assert replay.status_code == 200, replay.text
    assert replay.json() == moved.json()
    assert fixture.native_calls == calls
    assert [call[0] for call in calls] == ["transfer", "delete"]
    with fixture.app.state.session_factory() as db:
        profile = db.get(ProfileRef, fixture.alice[2])
        session = db.get(SessionLink, fixture.alice[3])
        assert profile.gateway_id == session.gateway_id == fixture.destination_id
        assert profile.profile_name == session.profile_name == "control-dev"
        assert profile.avatar_data == b"preserved-avatar"
        assert db.get(Connector, fixture.connector_ids[fixture.alice[1]]).profiles == ["default"]
        assert fixture.app.state.connector_registry.get(fixture.alice[1]).profiles == frozenset({"default"})
        assert session.owner_id == fixture.alice[0]
        assert session.stored_session_id == "alice"
        assert session.runtime_session_id is None
        assert db.scalar(select(ProfileRef.id).where(
            ProfileRef.gateway_id == fixture.alice[1], ProfileRef.profile_name == "control-dev",
        )) is None
        assert db.get(ProfileRef, fixture.bob[2]).gateway_id == fixture.bob[1]
        assert db.get(SessionLink, fixture.bob[3]).owner_id == fixture.bob[0]

    # A different account cannot replay the original user's mutation receipt or
    # address the moved agent even when its stable ID and key are known.
    fixture.client.cookies.set("hc_session", fixture.bob[4])
    foreign = move(fixture, csrf=fixture.bob[5])
    assert foreign.status_code == 404, foreign.text
    assert fixture.native_calls == calls


@pytest.mark.parametrize("foreign", ["source", "destination"])
def test_cloud_move_rejects_known_foreign_ids_before_native_calls(cloud_transfer, foreign):
    fixture = cloud_transfer
    response = move(
        fixture,
        profile_id=fixture.bob[2] if foreign == "source" else fixture.alice[2],
        destination_id=fixture.bob[1] if foreign == "destination" else fixture.destination_id,
        confirmation="personal" if foreign == "source" else "control-dev",
    )
    assert response.status_code == 404, response.text
    assert fixture.bob[1] not in response.text and fixture.bob[2] not in response.text
    assert_source_unchanged(fixture)


@pytest.mark.parametrize("side", ["source", "destination"])
@pytest.mark.parametrize("condition", ["revoked", "offline", "old_connector"])
def test_cloud_move_requires_two_active_updated_connectors(cloud_transfer, side, condition):
    fixture = cloud_transfer
    gateway_id = fixture.alice[1] if side == "source" else fixture.destination_id
    if condition == "revoked":
        # Leave cached link/capabilities alive: the DB revocation must remain
        # authoritative even during the disconnect race.
        with fixture.app.state.session_factory() as db:
            db.get(Connector, fixture.connector_ids[gateway_id]).revoked_at = utc_now()
            db.commit()
    elif condition == "offline":
        fixture.app.state.connector_registry.links[gateway_id].online = False
    else:
        fixture.older_connectors.add(gateway_id)

    response = move(fixture)
    expected_status = 404 if condition == "revoked" else 409
    assert response.status_code == expected_status, response.text
    if condition == "offline":
        assert "Connect both computers" in response.json()["message"]
    elif condition == "old_connector":
        assert "Update both connectors" in response.json()["message"]
    assert_source_unchanged(fixture)


def test_cloud_move_keeps_csrf_and_exact_confirmation_requirements(cloud_transfer):
    fixture = cloud_transfer
    assert move(fixture, csrf="invalid", key="bad-csrf").status_code == 403
    rejected = move(fixture, confirmation="Control Dev", key="wrong-name")
    assert rejected.status_code == 409, rejected.text
    assert_source_unchanged(fixture)


def test_cloud_move_protects_default_profile(cloud_transfer):
    fixture = cloud_transfer
    with fixture.app.state.session_factory() as db:
        manager = db.scalar(select(ProfileRef).where(ProfileRef.gateway_id == fixture.alice[1], ProfileRef.profile_name == "default"))
        db.delete(manager)
        db.flush()
        db.get(ProfileRef, fixture.alice[2]).profile_name = "default"
        db.commit()
    response = move(fixture, confirmation="default")
    assert response.status_code == 409, response.text
    assert "default" in response.json()["message"]
    assert_source_unchanged(fixture)


def test_cloud_move_requires_authentication(cloud_transfer):
    fixture = cloud_transfer
    fixture.client.cookies.clear()
    response = move(fixture)
    assert response.status_code == 401, response.text
    assert_source_unchanged(fixture)


@pytest.mark.parametrize("failure", ["collision", "unknown"])
def test_cloud_transfer_refusal_or_uncertainty_never_deletes_unconfirmed_destination(cloud_transfer, failure):
    fixture = cloud_transfer
    fixture.transfer_fault["kind"] = failure
    fixture.source_provider._automations["cron-preserved"] = HermesAutomation(
        automation_id="cron-preserved", name="Preserved schedule", schedule="0 8 * * *",
        timezone="UTC", enabled=True, prompt="Do scheduled work",
    )
    response = move(fixture)
    assert response.status_code == 409, response.text
    expected_code = "CONFLICT" if failure == "collision" else "MUTATION_DELIVERY_UNKNOWN"
    assert response.json()["code"] == expected_code
    calls = list(fixture.native_calls)
    assert [call[0] for call in calls] == ["transfer"]
    assert not fixture.source_provider.__dict__.get("_own_profile_deleted", False)
    destination_manager = fixture.providers[(fixture.destination_id, "default")]
    assert "control-dev" in destination_manager._created_profiles
    if failure == "collision":
        assert destination_manager._created_profiles["control-dev"].display_name == "Unrelated native profile"
        assert fixture.source_provider._automations["cron-preserved"].enabled is True
    else:
        target = fixture.providers[(fixture.destination_id, "control-dev")]
        assert target._automations["cron-preserved"].enabled is False
        assert fixture.source_provider._automations["cron-preserved"].enabled is False
    with fixture.app.state.session_factory() as db:
        assert db.get(ProfileRef, fixture.alice[2]).gateway_id == fixture.alice[1]
        assert db.get(SessionLink, fixture.alice[3]).gateway_id == fixture.alice[1]
    replay = move(fixture)
    assert replay.status_code == 409 and replay.json()["code"] == expected_code
    assert fixture.native_calls == calls


def test_cloud_move_preserves_image_urls_and_blobs_without_exposing_another_owners_media(cloud_transfer):
    fixture = cloud_transfer
    store = FakeStore()
    media_service = VisualMediaService(fixture.settings, store)
    fixture.app.state.services.visual_media = media_service
    own_media_id, other_media_id = "a" * 32, "b" * 32
    with fixture.app.state.session_factory() as db:
        # Native import authorizes the new profile in the real connector
        # protocol. This transport fixture preapproves that profile to isolate
        # durable media-route migration and HTTP authorization here.
        destination = db.get(Connector, fixture.connector_ids[fixture.destination_id])
        destination.profiles = [*destination.profiles, "control-dev"]
        db.commit()
        for tenant, media_id, profile_name in (
            (fixture.alice, own_media_id, "control-dev"), (fixture.bob, other_media_id, "personal"),
        ):
            connector = db.get(Connector, fixture.connector_ids[tenant[1]])
            session = db.get(SessionLink, tenant[3])
            assert media_service.ingest(
                db, connector, media_id=media_id, profile_name=profile_name,
                stored_session_id=session.stored_session_id,
                metadata={"alt": "Preserved figure", "provenance": "generated", "width": 80, "height": 60, "mediaType": "image/png"},
                content=picture(),
            ) == {"id": media_id, "status": "ready"}
    own_url = f"/api/v1/sessions/{fixture.alice[3]}/media/{own_media_id}"
    foreign_url = f"/api/v1/sessions/{fixture.bob[3]}/media/{other_media_id}"
    original = fixture.client.get(own_url)
    assert original.status_code == 200, original.text
    metadata = fixture.client.get(own_url + "/metadata").json()
    assert fixture.client.get(foreign_url).status_code == 404
    objects_before = deepcopy(store.objects)
    puts_before = store.puts

    moved = move(fixture)
    assert moved.status_code == 200, moved.text
    preserved = fixture.client.get(own_url)
    assert preserved.status_code == 200, preserved.text
    assert preserved.content == original.content
    assert fixture.client.get(own_url + "/metadata").json() == metadata
    assert fixture.client.get(own_url + "?variant=thumbnail").status_code == 200
    assert fixture.client.get(foreign_url).status_code == 404
    assert store.objects == objects_before and store.puts == puts_before
    with fixture.app.state.session_factory() as db:
        assert db.get(VisualMedia, own_media_id).gateway_id == fixture.destination_id
        assert db.get(VisualMedia, own_media_id).owner_id == fixture.alice[0]
        assert db.get(VisualMedia, other_media_id).gateway_id == fixture.bob[1]
        assert db.get(VisualMedia, other_media_id).owner_id == fixture.bob[0]

    fixture.client.cookies.set("hc_session", fixture.bob[4])
    assert fixture.client.get(own_url).status_code == 404
    assert fixture.client.get(own_url + "/metadata").status_code == 404
    assert fixture.client.get(foreign_url).status_code == 200


def test_cloud_config_failure_has_known_rollback_receipt_and_retires_only_import_grant(cloud_transfer, monkeypatch):
    fixture = cloud_transfer
    original = InMemoryHermesProvider.replace_config

    async def fail_destination(self, config):
        if self.connection.gateway_id == fixture.destination_id:
            raise RuntimeError("private upstream detail must not appear in receipt")
        return await original(self, config)

    monkeypatch.setattr(InMemoryHermesProvider, "replace_config", fail_destination)
    # Real transport grants this future profile before dispatching its import.
    with fixture.app.state.session_factory() as db:
        db.get(Connector, fixture.connector_ids[fixture.destination_id]).profiles = ["default", "control-dev"]
        db.commit()
    link = fixture.app.state.connector_registry.get(fixture.destination_id)
    link.profiles = frozenset({"default", "control-dev"})
    response = move(fixture)
    assert response.status_code == 409 and response.json()["code"] == "CONFLICT"
    assert "config_restore" in response.json()["message"]
    assert "private upstream" not in response.text
    calls = list(fixture.native_calls)
    assert calls[-1] == ("delete", fixture.destination_id, "control-dev")
    with fixture.app.state.session_factory() as db:
        assert db.get(ProfileRef, fixture.alice[2]).gateway_id == fixture.alice[1]
        assert db.get(Connector, fixture.connector_ids[fixture.destination_id]).profiles == ["default"]
        assert db.get(Connector, fixture.connector_ids[fixture.alice[1]]).profiles == ["default", "control-dev"]
        receipt = db.scalar(select(AuditEvent).where(AuditEvent.action == "profile.move"))
        assert receipt.outcome == "rolled_back"
        assert receipt.details["failureStage"] == "config_restore"
    assert link.profiles == frozenset({"default"})
    replay = move(fixture)
    assert replay.json() == response.json()
    assert fixture.native_calls == calls


def test_cloud_last_shared_agent_is_rejected_before_native_transfer(cloud_transfer):
    fixture = cloud_transfer
    with fixture.app.state.session_factory() as db:
        manager = db.scalar(select(ProfileRef).where(ProfileRef.gateway_id == fixture.alice[1], ProfileRef.profile_name == "default"))
        db.delete(manager)
        db.commit()
    response = move(fixture)
    assert response.status_code == 409 and "Share another agent" in response.json()["message"]
    assert_source_unchanged(fixture)


def test_cloud_delete_requires_retirement_aware_connector_before_native_mutation(cloud_transfer):
    fixture = cloud_transfer
    fixture.older_connectors.add(fixture.alice[1])
    response = fixture.client.request(
        "DELETE", f"/api/v1/profiles/{fixture.alice[2]}",
        headers=mutation_headers(fixture.alice[5], "old-cloud-delete"),
        json={"confirmation": "control-dev"},
    )
    assert response.status_code == 409, response.text
    assert_source_unchanged(fixture)
