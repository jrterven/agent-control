from __future__ import annotations

import logging

import pytest

from hermes_client import EndpointPolicy, EventNormalizer, UnsafeEndpointError, resolve_endpoint, validate_endpoint
from hermes_client.provider import _PinnedNetworkBackend
from hermes_control_api.security import SecretVault, hash_password, verify_password
from hermes_control_api.main import RedactingLogFilter


@pytest.mark.asyncio
async def test_ssrf_blocks_metadata_and_requires_explicit_private_mode():
    async def public_resolver(host: str, port: int):
        return ["93.184.216.34"]

    async def private_resolver(host: str, port: int):
        return ["10.10.0.5"]

    assert (
        await validate_endpoint(
            "https://example.test:443/path", EndpointPolicy(), resolver=public_resolver
        )
        == "https://example.test:443/path"
    )
    with pytest.raises(UnsafeEndpointError):
        await validate_endpoint("http://169.254.169.254/latest/meta-data", EndpointPolicy())
    with pytest.raises(UnsafeEndpointError):
        await validate_endpoint(
            "http://100.100.100.200/latest/meta-data",
            EndpointPolicy(allow_private=True, allow_loopback=True),
        )
    with pytest.raises(UnsafeEndpointError):
        await validate_endpoint(
            "http://internal.test:9119", EndpointPolicy(), resolver=private_resolver
        )
    await validate_endpoint(
        "http://internal.test:9119",
        EndpointPolicy(allow_private=True),
        resolver=private_resolver,
    )


@pytest.mark.asyncio
async def test_ssrf_blocks_public_cgnat_and_cross_protocol_urls():
    async def tailscale_resolver(host: str, port: int):
        return ["100.75.1.2"]

    with pytest.raises(UnsafeEndpointError):
        await validate_endpoint(
            "https://tailnet-host.test",
            EndpointPolicy(),
            resolver=tailscale_resolver,
        )
    async def mapped_tailscale_resolver(host: str, port: int):
        return ["::ffff:100.75.1.2"]

    with pytest.raises(UnsafeEndpointError):
        await validate_endpoint(
            "https://mapped-tailnet-host.test",
            EndpointPolicy(),
            resolver=mapped_tailscale_resolver,
        )
    async def mapped_metadata_resolver(host: str, port: int):
        return ["::ffff:100.100.100.200"]

    with pytest.raises(UnsafeEndpointError):
        await validate_endpoint(
            "http://mapped-metadata.test",
            EndpointPolicy(allow_private=True, allow_loopback=True),
            resolver=mapped_metadata_resolver,
        )
    resolved = await resolve_endpoint(
        "https://tailnet-host.test",
        EndpointPolicy(allow_private=True),
        resolver=tailscale_resolver,
    )
    assert resolved.addresses == ("100.75.1.2",)

    async def public_resolver(host: str, port: int):
        return ["93.184.216.34"]

    with pytest.raises(UnsafeEndpointError):
        await validate_endpoint(
            "ws://example.test/socket",
            EndpointPolicy(allowed_schemes=frozenset({"http", "https"})),
            resolver=public_resolver,
        )


@pytest.mark.asyncio
async def test_pinned_http_backend_never_redoes_dns_resolution():
    calls: list[tuple[str, int]] = []

    class Delegate:
        async def connect_tcp(self, host: str, port: int, **kwargs):
            calls.append((host, port))
            return object()

        async def connect_unix_socket(self, path: str, **kwargs):
            raise AssertionError("unix sockets are not used")

        async def sleep(self, seconds: float) -> None:
            return None

    backend = _PinnedNetworkBackend(
        Delegate(),
        expected_host="gateway.example",
        connect_host="93.184.216.34",
    )
    await backend.connect_tcp("gateway.example", 443)
    assert calls == [("93.184.216.34", 443)]
    with pytest.raises(OSError, match="Unvalidated"):
        await backend.connect_tcp("metadata.internal", 80)


def test_vault_is_authenticated_and_bound_to_gateway_field():
    vault = SecretVault(b"x" * 32)
    envelope = vault.encrypt("super-secret-token", aad="gateway:one:api")
    assert "super-secret-token" not in envelope
    assert vault.decrypt(envelope, aad="gateway:one:api") == "super-secret-token"
    with pytest.raises(ValueError):
        vault.decrypt(envelope, aad="gateway:two:api")


def test_passwords_use_argon2_and_invalid_password_fails():
    digest = hash_password("this is a long admin password")
    assert digest.startswith("$argon2id$")
    assert verify_password(digest, "this is a long admin password")
    assert not verify_password(digest, "wrong password")


def test_normalizer_removes_credentials_reasoning_and_home_paths():
    event = EventNormalizer(gateway_id="g1", profile_name="default").normalize(
        {
            "event": "tool.completed",
            "seq": 9,
            "payload": {
                "session_id": "run-1",
                "api_key": "do-not-leak",
                "reasoning_content": "private chain",
                "output": "/home/hermes/.hermes/config.yaml",
            },
        }
    )
    assert "api_key" not in event.data
    assert "reasoning_content" not in event.data
    assert "output" not in event.data


def test_normalizer_preserves_only_bounded_public_session_usage_counters():
    event = EventNormalizer(gateway_id="g1", profile_name="control-dev").normalize(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "session.usage",
                "session_id": "runtime-usage",
                "stored_session_id": "stored-usage",
                "seq": 22,
                "payload": {
                    "usage": {
                        "input": 12_000,
                        "output": 800,
                        "total": 12_800,
                        "calls": 4,
                        "context_used": 53_248,
                        "context_max": 128_000,
                        "context_percent": 142,
                        "compressions": 1,
                        "active_subagents": 2,
                        "prompt": 10**10_000,
                        "completion": -1,
                        "reasoning": 999,
                        "reasoning_content": "PRIVATE-COT",
                        "model": "private-provider-model",
                        "dev_credits_spent_micros": 123_456,
                        "api_key": "secret-key",
                        "negative": -1,
                        "not_finite": float("inf"),
                    }
                },
            },
        }
    )

    assert event.type == "session.usage"
    assert event.runtime_session_id == "runtime-usage"
    assert event.stored_session_id == "stored-usage"
    assert event.sequence == 22
    assert event.data == {
        "usage": {
            "input": 12_000,
            "output": 800,
            "total": 12_800,
            "calls": 4,
            "context_used": 53_248,
            "context_max": 128_000,
            "context_percent": 100,
            "compressions": 1,
            "active_subagents": 2,
        }
    }
    rendered = str(event.to_dict())
    assert "PRIVATE-COT" not in rendered
    assert "private-provider-model" not in rendered
    assert "secret-key" not in rendered
    assert "reasoning" not in rendered
    assert "credits" not in rendered


def test_message_completion_usage_uses_the_same_public_counter_projection():
    event = EventNormalizer(gateway_id="g1", profile_name="default").normalize(
        {
            "event": "message.complete",
            "payload": {
                "text": "Respuesta pública",
                "usage": {
                    "total": 900,
                    "context_used": 700,
                    "context_max": 4_000,
                    "reasoning_content": "PRIVATE-COT",
                    "unexpected": {"token": "secret"},
                },
            },
        }
    )

    assert event.data == {
        "text": "Respuesta pública",
        "usage": {"total": 900, "context_used": 700, "context_max": 4_000},
    }


@pytest.mark.parametrize("key", ["thinking", "analysis", "thoughts", "deliberation"])
def test_normalizer_never_exposes_reasoning_aliases(key: str):
    event = EventNormalizer(gateway_id="g1", profile_name="default").normalize(
        {
            "event": "message.delta",
            "payload": {"session_id": "runtime-1", "delta": "safe", key: "PRIVATE-COT"},
        }
    )
    assert event.data == {"delta": "safe"}
    assert "PRIVATE-COT" not in str(event.data)


@pytest.mark.parametrize(
    ("discriminator", "value"),
    [
        ("role", "analysis"),
        ("type", "thinking"),
        ("kind", "reasoning"),
        ("role", "assistant_analysis"),
        ("type", "reasoning_content"),
        ("kind", "chain-of-thought"),
        ("role", "internal_monologue"),
        ("type", "scratchpad"),
        ("kind", "thoughts"),
        ("role", "deliberation"),
    ],
)
def test_message_payload_reasoning_discriminator_omits_all_private_text(
    discriminator: str, value: str
):
    private = f"PRIVATE-COT-{discriminator}-{value}"
    event = EventNormalizer(gateway_id="g1", profile_name="control-dev").normalize(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "message.delta",
                "session_id": "runtime-private",
                "seq": 21,
                "payload": {
                    discriminator: value,
                    "content": private,
                    "text": private,
                    "delta": private,
                    "status": "streaming",
                },
            },
        }
    )

    assert event.type == "reasoning.omitted"
    assert event.data == {"omitted": True}
    assert private not in str(event.to_dict())
    assert event.runtime_session_id == "runtime-private"
    assert event.sequence == 21


def test_any_top_level_discriminator_can_mark_message_as_private():
    event = EventNormalizer(gateway_id="g1", profile_name="default").normalize(
        {
            "event": "message.completed",
            "payload": {
                "type": "assistant_message",
                "role": "assistant",
                "kind": "THINKING",
                "content": "PRIVATE-COT-MULTIPLE-DISCRIMINATORS",
            },
        }
    )

    assert event.type == "reasoning.omitted"
    assert event.data == {"omitted": True}
    assert "PRIVATE-COT" not in str(event.to_dict())


def test_reasoning_in_message_event_type_is_omitted_before_projection():
    event = EventNormalizer(gateway_id="g1", profile_name="default").normalize(
        {
            "event": "message.reasoning.delta",
            "payload": {"role": "assistant", "delta": "PRIVATE-COT-EVENT-TYPE"},
        }
    )

    assert event.type == "reasoning.omitted"
    assert event.data == {"omitted": True}
    assert "PRIVATE-COT-EVENT-TYPE" not in str(event.to_dict())


def test_regular_assistant_message_still_projects_public_content():
    event = EventNormalizer(gateway_id="g1", profile_name="default").normalize(
        {
            "event": "message.delta",
            "payload": {
                "role": "assistant",
                "kind": "message",
                "delta": "Public answer",
            },
        }
    )

    assert event.type == "message.delta"
    assert event.data == {"role": "assistant", "delta": "Public answer"}


def test_normalizer_unwraps_official_event_envelope():
    event = EventNormalizer(gateway_id="g1", profile_name="jarvis").normalize(
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "message.delta",
                "session_id": "runtime-1",
                "stored_session_id": "stored-1",
                "seq": 17,
                "payload": {"delta": "hola"},
            },
        }
    )
    assert event.type == "message.delta"
    assert event.runtime_session_id == "runtime-1"
    assert event.stored_session_id == "stored-1"
    assert event.sequence == 17
    assert event.data == {"delta": "hola"}
    public = event.to_dict()
    assert public["eventId"] == public["id"]
    assert public["occurredAt"] == public["timestamp"]
    assert public["seq"] == public["sequence"] == 17
    assert public["data"] == public["payload"] == {"delta": "hola"}


def test_access_log_filter_redacts_one_use_realtime_ticket():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '127.0.0.1 - "WebSocket /api/v1/realtime?ticket=one-use-secret&cursors=%7B%7D"',
        (),
        None,
    )
    assert RedactingLogFilter().filter(record)
    rendered = record.getMessage()
    assert "one-use-secret" not in rendered
    assert "[REDACTED]" in rendered


def test_access_log_filter_preserves_uvicorn_structured_arguments():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "GET", "/api/v1/realtime?ticket=one-use-secret", "1.1", 101),
        None,
    )
    assert RedactingLogFilter().filter(record)
    assert isinstance(record.args, tuple)
    assert len(record.args) == 5
    assert "one-use-secret" not in str(record.args)


def test_log_filter_redacts_entire_authorization_bearer_value():
    record = logging.LogRecord(
        "hermes_control",
        logging.ERROR,
        __file__,
        1,
        "upstream Authorization: Bearer supersecretvalue failed",
        (),
        None,
    )
    assert RedactingLogFilter().filter(record)
    rendered = record.getMessage()
    assert "supersecretvalue" not in rendered
    assert "[REDACTED]" in rendered
