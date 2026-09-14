import struct
from datetime import datetime, timezone
import pytest
from hermes_client.connector_protocol import FrameReader, MAX_FRAME_BYTES, ProtocolError, frames, encode_message, decode_message
from hermes_client.types import PromptAttachment, SessionRoute, NormalizedEvent


def test_binary_attachment_and_typed_routes_roundtrip_across_bounded_frames():
    attachment = PromptAttachment(kind="image", name="photo.png", media_type="image/png", content=b"123456" * 200_000)
    message = {"v": 1, "type": "request", "args": (SessionRoute("gateway", "alice", "stored", "runtime"), attachment)}
    reader = FrameReader()
    chunks = list(frames(message))
    assert len(chunks) > 1
    assert all(len(chunk) <= MAX_FRAME_BYTES for chunk in chunks)
    values = [reader.feed(chunk) for chunk in chunks]
    assert values[-1] == message
    assert values[:-1] == [None] * (len(chunks) - 1)


def test_wire_tags_in_untrusted_json_do_not_construct_objects():
    value = {"v": 1, "data": {"$type": "SessionRoute", "fields": {"gateway_id": "foreign"}}}
    assert decode_message(encode_message(value)) == value


def test_reordered_chunks_and_oversized_frames_are_rejected():
    chunks = list(frames({"v": 1, "body": b"x" * 400_000}))
    with pytest.raises(ProtocolError):
        FrameReader().feed(chunks[1])
    with pytest.raises(ProtocolError):
        FrameReader().feed(b"x" * (MAX_FRAME_BYTES + 1))


def test_version_mismatch_is_rejected():
    with pytest.raises(ProtocolError):
        decode_message(encode_message({"v": 2}))


def test_same_normalized_event_including_private_metadata_roundtrips():
    event = NormalizedEvent.create(type="message.complete", gateway_id="g", profile_name="p", runtime_generation="generation", private_data={"authorized": True})
    decoded = decode_message(encode_message({"v": 1, "event": event}))
    assert decoded["event"] == event


def test_repeated_binary_references_cannot_amplify_memory(monkeypatch):
    import json
    import hermes_client.connector_protocol as protocol
    monkeypatch.setattr(protocol, "MAX_BLOB_BYTES", 64)
    metadata = json.dumps({"$dict": {"v": 1, "items": [{"$bytes": [0, 64]}] * 3}}).encode()
    payload = struct.pack("!I", len(metadata)) + metadata + b"x" * 64
    with pytest.raises(ProtocolError, match="Decoded binary"):
        protocol.decode_message(payload)


def test_dataclass_fields_and_operation_arguments_are_type_checked():
    import json
    from hermes_client.connector_protocol import validate_arguments
    with pytest.raises(ProtocolError):
        validate_arguments("submit_prompt", ("not-a-route", "prompt"), {"operation_id":"id"})
    forged = {"$dict":{"v":1,"route":{"$type":"SessionRoute","fields":{"gateway_id":42,"profile_name":"selected","stored_session_id":"stored"}}}}
    metadata = json.dumps(forged).encode()
    with pytest.raises(ProtocolError, match="typed"):
        decode_message(struct.pack("!I",len(metadata))+metadata)
