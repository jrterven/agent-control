"""Cloud relay bounds and, crucially, ownership after an uncertain import."""
import asyncio
import hashlib
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from hermes_client import ProviderConnection
from hermes_client.provider import ProfileManagementServerRequired
from hermes_client.types import CapabilitySet, HermesProfile
from hermes_control_api.remote_provider import (
    ConnectorRegistry, ConnectorLink, ProfileTransferNotImported, ProfileTransferOutcomeUnknown,
    ProfileTransferManagementServerRequired,
    RemoteProvider,
)


class TransferPeer:
    def __init__(self, payload):
        self.online = True
        self.payload = payload
        self.calls = []
        self.received = bytearray()
        self.failure = None
        self.tamper = None
        self.caps = CapabilitySet(
            methods=frozenset({"profiles.export", "profiles.import", "profiles.transfer"}),
            features=frozenset({"connector.profileTransferV3", "profiles.transfer"}),
        )

    async def call(self, profile, operation, args, kwargs, *, operation_id=None):
        self.calls.append((operation, kwargs, operation_id))
        if self.failure and operation == self.failure[0]:
            raise self.failure[1]
        if operation == "capabilities":
            return self.caps
        receipt = {"transferId": kwargs.get("transfer_id"), "size": len(self.payload),
                   "sha256": hashlib.sha256(self.payload).hexdigest(), "offset": 0}
        if operation == "profile_archive_read":
            result = self.payload[kwargs["offset"]:kwargs["offset"] + kwargs["length"]]
        elif operation == "profile_archive_write":
            assert kwargs["offset"] == len(self.received)
            self.received.extend(kwargs["chunk"])
            result = {**receipt, "offset": len(self.received)}
        elif operation == "profile_import_finish":
            result = HermesProfile(name="Control.dev", display_name="Test")
        elif operation == "profile_archive_cleanup":
            result = None
        else:
            assert operation in {"profile_export", "profile_import_begin"}
            result = receipt
        if self.tamper and operation == self.tamper[0]:
            return self.tamper[1](result)
        return result


@pytest.fixture
def relay():
    payload = b"x" * (2 * 1024 * 1024 + 7)
    registry = ConnectorRegistry(AsyncMock())
    source, destination = TransferPeer(payload), TransferPeer(payload)
    registry.links.update(source=source, destination=destination)
    def remote(gateway):
        return RemoteProvider(ProviderConnection(gateway_id=gateway, profile_name="manager",
            rest_url="connector://local", ws_url="connector://local"), registry)
    return remote("source"), remote("destination"), source, destination


@pytest.mark.asyncio
async def test_relay_streams_bounded_chunks_with_stable_phase_ids_and_no_native_paths(relay):
    source, destination, source_peer, destination_peer = relay
    result = await source.transfer_profile_to(destination, name="Control.dev")
    assert result.name == "Control.dev"
    assert bytes(destination_peer.received) == source_peer.payload
    writes = [call for call in destination_peer.calls if call[0] == "profile_archive_write"]
    assert [len(call[1]["chunk"]) for call in writes] == [1024 * 1024, 1024 * 1024, 7]
    transfer_ids = set()
    for operation, kwargs, identity in source_peer.calls + destination_peer.calls:
        if operation == "capabilities":
            continue
        transfer_ids.add(kwargs["transfer_id"])
        assert identity.startswith(kwargs["transfer_id"] + ":")
        assert not {"path", "url", "archive_path"}.intersection(kwargs)
    assert len(transfer_ids) == 1
    for peer in (source_peer, destination_peer):
        assert peer.calls[-1][0] == "profile_archive_cleanup"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["profile_export", "profile_import_begin", "profile_archive_read", "profile_archive_write"])
async def test_failure_before_import_proves_destination_was_not_created(relay, operation):
    source, destination, source_peer, destination_peer = relay
    peer = source_peer if operation in {"profile_export", "profile_archive_read"} else destination_peer
    peer.failure = operation, ConnectionError("Disconnected")
    with pytest.raises(ProfileTransferNotImported):
        await source.transfer_profile_to(destination, name="Control.dev")
    assert not any(call[0] == "profile_import_finish" for call in destination_peer.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_import_disconnect_preserves_uncertainty_and_explicit_refusal_proves_no_ownership(relay, refused):
    source, destination, _, peer = relay
    error = ProfileTransferNotImported("PROFILE_TRANSFER_IMPORT_REFUSED") if refused else ConnectionError("Disconnected")
    peer.failure = "profile_import_finish", error
    expected = ProfileTransferNotImported if refused else ProfileTransferOutcomeUnknown
    with pytest.raises(expected):
        await source.transfer_profile_to(destination, name="Control.dev")
    assert sum(call[0] == "profile_import_finish" for call in peer.calls) == 1
    assert not any(call[0] == "delete_profile" for call in peer.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["profile_export", "profile_import_begin", "profile_import_finish"])
async def test_server_root_refusal_keeps_actionable_known_not_imported_outcome(relay, operation):
    source, destination, source_peer, destination_peer = relay
    peer = source_peer if operation == "profile_export" else destination_peer
    refusal = ProfileTransferManagementServerRequired() if operation == "profile_import_finish" else ProfileManagementServerRequired()
    peer.failure = operation, refusal
    with pytest.raises(ProfileTransferManagementServerRequired, match="hermes -p default serve"):
        await source.transfer_profile_to(destination, name="Control.dev")
    assert not any(call[0] == "delete_profile" for call in source_peer.calls + destination_peer.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("code,expected", [
    ("HERMES_DEFAULT_SERVER_REQUIRED", ProfileManagementServerRequired),
    ("PROFILE_TRANSFER_DEFAULT_SERVER_REQUIRED", ProfileTransferManagementServerRequired),
])
async def test_root_refusal_wire_codes_are_typed_and_never_include_native_detail(code, expected):
    link = ConnectorLink("gateway", frozenset({"manager"}), AsyncMock(), AsyncMock())
    future = asyncio.get_running_loop().create_future()
    link.pending["test"] = future
    link.request_profiles["test"] = "manager"
    link.response({"id": "test", "profile": "manager", "error": code})
    with pytest.raises(expected, match="hermes -p default serve"):
        await future


@pytest.mark.asyncio
@pytest.mark.parametrize("operation,tamper", [
    ("profile_export", lambda r: {**r, "size": 100 * 1024 * 1024 + 1}),
    ("profile_export", lambda r: {**r, "size": True}),
    ("profile_export", lambda r: {**r, "transferId": "another-transfer"}),
    ("profile_archive_read", lambda r: r[:-1]),
    ("profile_archive_read", lambda r: b"y" * len(r)),
    ("profile_archive_write", lambda r: {**r, "offset": 0}),
])
async def test_malformed_or_modified_archive_never_reaches_import(relay, operation, tamper):
    source, destination, source_peer, destination_peer = relay
    peer = destination_peer if operation == "profile_archive_write" else source_peer
    peer.tamper = operation, tamper
    with pytest.raises(ProfileTransferNotImported):
        await source.transfer_profile_to(destination, name="Control.dev")
    assert not any(call[0] == "profile_import_finish" for call in destination_peer.calls)


@pytest.mark.asyncio
async def test_import_identity_mismatch_is_uncertain_not_safe_to_delete(relay):
    source, destination, _, peer = relay
    peer.tamper = "profile_import_finish", lambda r: replace(r, name="unrelated")
    with pytest.raises(ProfileTransferOutcomeUnknown):
        await source.transfer_profile_to(destination, name="Control.dev")


@pytest.mark.asyncio
@pytest.mark.parametrize("old_features", [frozenset({"profiles.transfer"}), frozenset({"profiles.transfer", "connector.profileTransferV1"}), frozenset({"profiles.transfer", "connector.profileTransferV2"})])
async def test_old_connector_cannot_advertise_cloud_transfer_or_delete(relay, old_features):
    source, destination, source_peer, peer = relay
    peer.caps = replace(peer.caps, methods=peer.caps.methods | {"profiles.delete"}, features=old_features)
    caps = await destination.capabilities()
    assert not {"profiles.delete", "profiles.transfer", "profiles.import", "profiles.export"}.intersection(caps.methods)
    with pytest.raises(ProfileTransferNotImported, match="Update both connectors"):
        await source.transfer_profile_to(destination, name="Control.dev")
    assert all(call[0] == "capabilities" for call in source_peer.calls + peer.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("features", [frozenset(), frozenset({"connector.profileTransferV1"})])
async def test_direct_delete_refuses_old_connection_before_destructive_dispatch(relay, features):
    source, _, peer, _ = relay
    peer.caps = replace(peer.caps, features=features)
    with pytest.raises(ValueError, match="Update the connector"):
        await source.delete_profile("Control.dev")
    assert [call[0] for call in peer.calls] == ["capabilities"]


@pytest.mark.asyncio
@pytest.mark.parametrize("replace_link", [False, True])
async def test_delete_never_reuses_attestation_after_disconnect_or_connection_replacement(relay, replace_link):
    source, _, peer, _ = relay
    replacement = TransferPeer(b"unused")
    replacement.caps = replace(replacement.caps, features=frozenset({"connector.profileTransferV1"}))

    async def attest_then_change(profile, operation, args, kwargs):
        assert profile == "manager" and operation == "capabilities" and args == () and kwargs == {}
        if replace_link:
            source.registry.links["source"] = replacement
        else:
            peer.online = False
        return peer.caps

    peer.call = AsyncMock(side_effect=attest_then_change)
    with pytest.raises(ConnectionError, match="before agent deletion was sent"):
        await source.delete_profile("Control.dev")
    peer.call.assert_awaited_once_with("manager", "capabilities", (), {})
    assert replacement.calls == []


@pytest.mark.asyncio
async def test_delete_attests_and_dispatches_on_the_same_verified_connection(relay):
    source, _, peer, _ = relay
    peer.call = AsyncMock(side_effect=[peer.caps, None])
    assert await source.delete_profile("Control.dev") is None
    assert peer.call.await_args_list[0].args == ("manager", "capabilities", (), {})
    assert peer.call.await_args_list[1].args == ("manager", "delete_profile", ("Control.dev",), {})


@pytest.mark.asyncio
async def test_invalid_destination_and_default_never_dispatch(relay):
    source, destination, source_peer, peer = relay
    with pytest.raises(ProfileTransferNotImported):
        await source.transfer_profile_to(source, name="Control.dev")
    with pytest.raises(ProfileTransferNotImported):
        await source.transfer_profile_to(destination, name="default")
    destination.registry = ConnectorRegistry(AsyncMock())
    with pytest.raises(ProfileTransferNotImported):
        await source.transfer_profile_to(destination, name="Control.dev")
    assert not source_peer.calls and not peer.calls


@pytest.mark.asyncio
async def test_capability_disconnect_is_proven_preimport(relay):
    source, destination, _, peer = relay
    peer.failure = "capabilities", ConnectionError("Disconnected")
    with pytest.raises(ProfileTransferNotImported):
        await source.transfer_profile_to(destination, name="Control.dev")
    assert [call[0] for call in peer.calls] == ["capabilities"]


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_cleanup_cancellation_never_overwrites_ownership_outcome(relay, uncertain):
    source, destination, source_peer, destination_peer = relay
    if uncertain:
        source_peer.failure = "profile_archive_cleanup", asyncio.CancelledError()
        destination_peer.failure = "profile_import_finish", ConnectionError("Disconnected")
    else:
        source_peer.failure = "profile_export", ConnectionError("Disconnected")
        destination_peer.failure = "profile_archive_cleanup", asyncio.CancelledError()
    expected = ProfileTransferOutcomeUnknown if uncertain else ProfileTransferNotImported
    with pytest.raises(expected):
        await source.transfer_profile_to(destination, name="Control.dev")
