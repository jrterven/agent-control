from __future__ import annotations

import gzip
import json
import logging
from datetime import datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from hermes_control_api.integrations import (
    ElevenLabsScribeClient,
    IntegrationError,
    TranscriptionTokenLimiter,
)
from hermes_control_api.models import (
    AuditEvent,
    IdempotencyOperation,
    User,
    UserIntegration,
)
from hermes_control_api.security import hash_password


class FakeScribeClient:
    def __init__(self) -> None:
        self.api_keys: list[str] = []
        self.tokens: list[str] = []

    async def issue_realtime_token(self, api_key: str) -> str:
        self.api_keys.append(api_key)
        token = f"sutkn_test_{len(self.tokens) + 1}"
        self.tokens.append(token)
        return token


class CountingAsyncStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.iterations = 0

    async def __aiter__(self):
        for chunk in self.chunks:
            self.iterations += 1
            yield chunk


def _put_key(
    client: TestClient,
    csrf: str,
    api_key: str,
    *,
    idempotency_key: str | None = None,
):
    headers = {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": idempotency_key or uuid4().hex,
    }
    return client.put(
        "/api/v1/integrations/elevenlabs/key",
        headers=headers,
        json={"apiKey": api_key},
    )


def _issue_token(
    client: TestClient,
    csrf: str,
    *,
    idempotency_key: str | None = None,
):
    headers = {"X-CSRF-Token": csrf}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return client.post(
        "/api/v1/realtime/transcription-token",
        headers=headers,
        json={},
    )


def test_owner_can_store_write_only_key_and_bootstrap_exposes_neutral_capability(
    authenticated, app
):
    client, csrf = authenticated
    secret = "sk_owner_1234567890_private"

    before = client.get("/api/v1/integrations/elevenlabs")
    assert before.status_code == 200
    assert before.json() == {
        "configured": False,
        "provider": "elevenlabs",
        "modelId": "scribe_v2_realtime",
    }

    stored = _put_key(client, csrf, secret)
    assert stored.status_code == 200, stored.text
    assert stored.json() == {
        "configured": True,
        "provider": "elevenlabs",
        "modelId": "scribe_v2_realtime",
    }
    assert secret not in stored.text

    with app.state.session_factory() as db:
        row = db.scalar(select(UserIntegration))
        assert row is not None
        assert row.provider == "elevenlabs"
        assert row.api_key_ciphertext.startswith("v1.")
        assert secret not in row.api_key_ciphertext
        assert (
            app.state.services.vault.decrypt(
                row.api_key_ciphertext,
                aad=f"user-integration:{row.owner_id}:elevenlabs:api-key",
            )
            == secret
        )
        with pytest.raises(ValueError):
            app.state.services.vault.decrypt(
                row.api_key_ciphertext,
                aad="user-integration:another-user:elevenlabs:api-key",
            )

    bootstrap = client.get("/api/v1/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["features"]["dictation"] == {
        "available": True,
        "provider": "elevenlabs",
        "modelId": "scribe_v2_realtime",
    }
    assert secret not in bootstrap.text


def test_non_admin_owners_are_isolated_and_token_uses_only_current_owner_key(
    authenticated, app
):
    client, admin_csrf = authenticated
    admin_key = "sk_admin_1234567890_private"
    reader_key = "sk_reader_1234567890_private"
    assert _put_key(client, admin_csrf, admin_key).status_code == 200

    with app.state.session_factory() as db:
        db.add(
            User(
                username="reader",
                password_hash=hash_password("reader password long enough"),
                is_admin=False,
            )
        )
        db.commit()

    reader_login = client.post(
        "/api/v1/auth/login",
        json={"username": "reader", "password": "reader password long enough"},
    )
    assert reader_login.status_code == 200
    reader_csrf = reader_login.json()["csrfToken"]
    assert client.get("/api/v1/integrations/elevenlabs").json()["configured"] is False
    assert _put_key(client, reader_csrf, reader_key).status_code == 200

    fake = FakeScribeClient()
    app.state.elevenlabs_scribe_client = fake
    reader_token = _issue_token(client, reader_csrf)
    assert reader_token.status_code == 200, reader_token.text
    assert fake.api_keys == [reader_key]

    admin_login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
    )
    assert admin_login.status_code == 200
    admin_token = _issue_token(client, admin_login.json()["csrfToken"])
    assert admin_token.status_code == 200, admin_token.text
    assert fake.api_keys == [reader_key, admin_key]

    with app.state.session_factory() as db:
        rows = db.scalars(
            select(UserIntegration).order_by(UserIntegration.owner_id)
        ).all()
        assert len(rows) == 2
        assert len({row.owner_id for row in rows}) == 2


def test_auth_csrf_and_cors_boundaries_cover_integration_mutations(client):
    assert client.get("/api/v1/integrations/elevenlabs").status_code == 401
    assert client.put(
        "/api/v1/integrations/elevenlabs/key",
        json={"apiKey": "sk_unauthed_1234567890"},
    ).status_code == 401

    login = client.post(
        "/api/v1/auth/login",
        json={"username": "admin", "password": "correct horse battery staple"},
    )
    assert login.status_code == 200
    csrf = login.json()["csrfToken"]
    assert client.put(
        "/api/v1/integrations/elevenlabs/key",
        json={"apiKey": "sk_no_csrf_1234567890"},
    ).status_code == 403
    assert client.post(
        "/api/v1/integrations/elevenlabs/test"
    ).status_code == 403
    assert client.delete(
        "/api/v1/integrations/elevenlabs/key"
    ).status_code == 403
    assert client.post(
        "/api/v1/realtime/transcription-token", json={}
    ).status_code == 403
    missing_idempotency = client.put(
        "/api/v1/integrations/elevenlabs/key",
        headers={"X-CSRF-Token": csrf},
        json={"apiKey": "sk_missing_idempotency_1234567890"},
    )
    assert missing_idempotency.status_code == 400
    assert "Idempotency-Key" in missing_idempotency.text

    preflight = client.options(
        "/api/v1/integrations/elevenlabs/key",
        headers={
            "Origin": "http://testserver",
            "Access-Control-Request-Method": "PUT",
            "Access-Control-Request-Headers": "X-CSRF-Token,Idempotency-Key",
        },
    )
    assert preflight.status_code == 200
    assert "PUT" in preflight.headers["access-control-allow-methods"]


def test_invalid_key_and_pydantic_errors_never_echo_raw_secret(
    authenticated, caplog
):
    client, csrf = authenticated
    invalid = "LEAK-ME-" + ("x" * 600)
    caplog.set_level(logging.DEBUG)

    rejected = _put_key(client, csrf, invalid)
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "INVALID_INTEGRATION_KEY"
    assert invalid not in rejected.text
    assert invalid not in caplog.text

    nested_secret = "NESTED-SECRET-MUST-NOT-LEAK"
    pydantic_rejected = client.put(
        "/api/v1/integrations/elevenlabs/key",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
        json={"apiKey": {"value": nested_secret}},
    )
    assert pydantic_rejected.status_code == 422
    assert pydantic_rejected.json()["code"] == "VALIDATION_ERROR"
    assert nested_secret not in pydantic_rejected.text
    assert nested_secret not in caplog.text


def test_put_replay_stores_only_digest_and_neutral_response(authenticated, app):
    client, csrf = authenticated
    secret = "sk_idempotent_1234567890_private"
    headers_key = "elevenlabs-key-set"

    first = _put_key(
        client,
        csrf,
        secret,
        idempotency_key=headers_key,
    )
    replay = _put_key(
        client,
        csrf,
        secret,
        idempotency_key=headers_key,
    )
    assert first.status_code == replay.status_code == 200
    assert replay.headers["X-Idempotent-Replay"] == "true"
    assert replay.json() == first.json()

    with app.state.session_factory() as db:
        rows = db.scalars(select(IdempotencyOperation)).all()
        assert len(rows) == 1
        serialized = str(rows[0].response_json)
        assert secret not in serialized
        assert "apiKey" not in serialized
        assert len(rows[0].response_json["requestHash"]) == 64


def test_transcription_tokens_are_never_persisted_or_idempotently_replayed(
    authenticated, app
):
    client, csrf = authenticated
    secret = "sk_token_owner_1234567890_private"
    assert _put_key(client, csrf, secret).status_code == 200
    fake = FakeScribeClient()
    app.state.elevenlabs_scribe_client = fake

    with app.state.session_factory() as db:
        before_count = len(db.scalars(select(IdempotencyOperation)).all())

    first = _issue_token(client, csrf, idempotency_key="must-not-be-recorded")
    second = _issue_token(client, csrf, idempotency_key="must-not-be-recorded")
    assert first.status_code == second.status_code == 200
    assert first.json()["token"] != second.json()["token"]
    assert "X-Idempotent-Replay" not in second.headers
    assert first.headers["Cache-Control"] == "no-store"
    assert first.json()["modelId"] == "scribe_v2_realtime"
    assert datetime.fromisoformat(first.json()["expiresAt"]).tzinfo is not None

    with app.state.session_factory() as db:
        operations = db.scalars(select(IdempotencyOperation)).all()
        assert len(operations) == before_count
        audit_rows = db.scalars(select(AuditEvent)).all()
        serialized = str(
            [
                {
                    "action": row.action,
                    "targetType": row.target_type,
                    "targetId": row.target_id,
                    "details": row.details,
                }
                for row in audit_rows
            ]
        )
        assert secret not in serialized
        assert first.json()["token"] not in serialized
        assert second.json()["token"] not in serialized
        token_audits = [
            row for row in audit_rows if row.action == "integration.elevenlabs.token.issue"
        ]
        assert len(token_audits) == 2
        assert all(row.target_id == "elevenlabs" and row.details == {} for row in token_audits)


def test_transcription_token_trailing_slash_redirect_never_creates_idempotency_row(
    authenticated, app
):
    client, csrf = authenticated
    assert _put_key(client, csrf, "sk_trailing_slash_1234567890").status_code == 200
    app.state.elevenlabs_scribe_client = FakeScribeClient()
    with app.state.session_factory() as db:
        before_count = len(db.scalars(select(IdempotencyOperation)).all())

    response = client.post(
        "/api/v1/realtime/transcription-token/",
        headers={
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "trailing-slash-must-not-persist",
        },
        json={},
        follow_redirects=True,
    )
    assert response.status_code == 200, response.text
    with app.state.session_factory() as db:
        assert len(db.scalars(select(IdempotencyOperation)).all()) == before_count


def test_test_endpoint_discards_single_use_token(authenticated, app):
    client, csrf = authenticated
    secret = "sk_test_owner_1234567890_private"
    assert _put_key(client, csrf, secret).status_code == 200
    fake = FakeScribeClient()
    app.state.elevenlabs_scribe_client = fake

    tested = client.post(
        "/api/v1/integrations/elevenlabs/test",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
    )
    assert tested.status_code == 200, tested.text
    assert tested.json() == {
        "ok": True,
        "provider": "elevenlabs",
        "modelId": "scribe_v2_realtime",
    }
    assert fake.api_keys == [secret]
    assert fake.tokens[0] not in tested.text


def test_delete_is_owner_scoped_and_disables_dictation(authenticated, app):
    client, csrf = authenticated
    assert _put_key(client, csrf, "sk_delete_1234567890_private").status_code == 200
    fake = FakeScribeClient()
    app.state.elevenlabs_scribe_client = fake

    deleted = client.delete(
        "/api/v1/integrations/elevenlabs/key",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
    )
    assert deleted.status_code == 204
    assert deleted.content == b""
    assert client.get("/api/v1/integrations/elevenlabs").json()["configured"] is False
    assert client.get("/api/v1/bootstrap").json()["features"]["dictation"]["available"] is False

    token = _issue_token(client, csrf)
    assert token.status_code == 409
    assert token.json()["code"] == "INTEGRATION_NOT_CONFIGURED"
    assert fake.api_keys == []


def test_unconfigured_test_and_token_failures_are_audited(authenticated, app):
    client, csrf = authenticated

    tested = client.post(
        "/api/v1/integrations/elevenlabs/test",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
    )
    token = _issue_token(client, csrf)
    assert tested.status_code == token.status_code == 409

    with app.state.session_factory() as db:
        rows = db.scalars(
            select(AuditEvent).where(
                AuditEvent.action.in_(
                    (
                        "integration.elevenlabs.test",
                        "integration.elevenlabs.token.issue",
                    )
                )
            )
        ).all()
        assert {row.action for row in rows} == {
            "integration.elevenlabs.test",
            "integration.elevenlabs.token.issue",
        }
        assert all(row.outcome == "failure" and row.details == {} for row in rows)


def test_decrypt_failures_for_test_and_token_are_audited(authenticated, app):
    client, csrf = authenticated
    assert _put_key(client, csrf, "sk_tampered_1234567890_private").status_code == 200
    with app.state.session_factory() as db:
        row = db.scalar(select(UserIntegration))
        assert row is not None
        row.api_key_ciphertext = "v1.invalid.invalid"
        db.commit()

    token = _issue_token(client, csrf)
    tested = client.post(
        "/api/v1/integrations/elevenlabs/test",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
    )
    assert token.status_code == 503
    # The idempotent mutation boundary converts an upstream-style 5xx into a
    # delivery-unknown response, but the endpoint failure remains audited.
    assert tested.status_code == 409

    with app.state.session_factory() as db:
        rows = db.scalars(
            select(AuditEvent).where(
                AuditEvent.action.in_(
                    (
                        "integration.elevenlabs.test",
                        "integration.elevenlabs.token.issue",
                    )
                )
            )
        ).all()
        assert len(rows) == 2
        assert all(row.outcome == "failure" and row.details == {} for row in rows)


def test_local_token_rate_limit_is_owner_scoped(authenticated, app):
    client, csrf = authenticated
    assert _put_key(client, csrf, "sk_rate_limit_1234567890_private").status_code == 200
    app.state.elevenlabs_scribe_client = FakeScribeClient()
    app.state.transcription_token_limiter = TranscriptionTokenLimiter(
        limit=2,
        window_seconds=60,
    )

    assert _issue_token(client, csrf).status_code == 200
    assert _issue_token(client, csrf).status_code == 200
    limited = _issue_token(client, csrf)
    assert limited.status_code == 429
    assert limited.json()["code"] == "TRANSCRIPTION_TOKEN_RATE_LIMITED"
    assert limited.json()["retryable"] is True
    assert int(limited.headers["Retry-After"]) >= 1
    with app.state.session_factory() as db:
        rows = db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "integration.elevenlabs.token.issue"
            )
        ).all()
        assert [row.outcome for row in rows] == ["success", "success", "failure"]


@pytest.mark.asyncio
async def test_official_client_sanitizes_upstream_rejection_body_and_headers():
    secret = "sk_upstream_1234567890_private"

    def reject(request: httpx.Request) -> httpx.Response:
        assert request.headers["xi-api-key"] == secret
        return httpx.Response(
            401,
            json={"detail": f"rejected {secret}"},
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as http_client:
        client = ElevenLabsScribeClient(http_client)
        with pytest.raises(IntegrationError) as caught:
            await client.issue_realtime_token(secret)

    assert caught.value.code == "INTEGRATION_CREDENTIAL_REJECTED"
    assert secret not in str(caught.value)
    assert secret not in caught.value.public_message


@pytest.mark.asyncio
async def test_official_client_rejects_valid_non_object_json_without_crashing():
    def non_object(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["sutkn_not_an_object"], request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(non_object)) as http_client:
        client = ElevenLabsScribeClient(http_client)
        with pytest.raises(IntegrationError) as caught:
            await client.issue_realtime_token("sk_non_object_1234567890")

    assert caught.value.code == "TRANSCRIPTION_PROVIDER_INVALID_RESPONSE"


@pytest.mark.asyncio
async def test_official_client_accepts_safe_token_without_assuming_prefix():
    token = "futureToken_abc-123"

    def success(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": token}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(success)) as http_client:
        client = ElevenLabsScribeClient(http_client)
        issued = await client.issue_realtime_token("sk_future_1234567890_private")

    assert issued == token


@pytest.mark.asyncio
async def test_official_client_never_accepts_api_key_echoed_as_token():
    secret = "sutkn_malicious_api_key_echo_1234567890"

    def echo(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"token": secret}, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(echo)) as http_client:
        client = ElevenLabsScribeClient(http_client)
        with pytest.raises(IntegrationError) as caught:
            await client.issue_realtime_token(secret)

    assert caught.value.code == "TRANSCRIPTION_PROVIDER_INVALID_RESPONSE"
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_official_client_rejects_declared_oversize_without_reading_body():
    stream = CountingAsyncStream([b"must-not-be-read"])

    def oversize(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Length": "16385"},
            stream=stream,
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(oversize)) as http_client:
        client = ElevenLabsScribeClient(http_client)
        with pytest.raises(IntegrationError) as caught:
            await client.issue_realtime_token("sk_oversize_1234567890_private")

    assert caught.value.code == "TRANSCRIPTION_PROVIDER_REJECTED"
    assert stream.iterations == 0


@pytest.mark.asyncio
async def test_official_client_stops_chunked_wire_body_at_16_kib_ceiling():
    stream = CountingAsyncStream(([b"x" * 4_096] * 4) + [b"x", b"never-read"])

    def oversize(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(oversize)) as http_client:
        client = ElevenLabsScribeClient(http_client)
        with pytest.raises(IntegrationError) as caught:
            await client.issue_realtime_token("sk_chunked_1234567890_private")

    assert caught.value.code == "TRANSCRIPTION_PROVIDER_REJECTED"
    assert stream.iterations == 5


@pytest.mark.asyncio
async def test_official_client_bounds_decompressed_body_and_accepts_safe_gzip():
    oversized = gzip.compress(
        json.dumps({"token": "x" * 20_000}, separators=(",", ":")).encode()
    )

    def compression_bomb(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=CountingAsyncStream([oversized]),
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(compression_bomb)
    ) as http_client:
        client = ElevenLabsScribeClient(http_client)
        with pytest.raises(IntegrationError) as caught:
            await client.issue_realtime_token("sk_compressed_1234567890_private")
    assert caught.value.code == "TRANSCRIPTION_PROVIDER_REJECTED"

    expected = "futureToken_gzip-123"
    compressed = gzip.compress(json.dumps({"token": expected}).encode())

    def safe_gzip(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"Content-Encoding": "gzip"},
            stream=CountingAsyncStream([compressed]),
            request=request,
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(safe_gzip)
    ) as http_client:
        client = ElevenLabsScribeClient(http_client)
        assert (
            await client.issue_realtime_token("sk_gzip_1234567890_private")
            == expected
        )


def test_integration_test_endpoint_shares_the_owner_token_rate_limit(
    authenticated, app
):
    client, csrf = authenticated
    assert _put_key(client, csrf, "sk_test_rate_1234567890_private").status_code == 200
    app.state.elevenlabs_scribe_client = FakeScribeClient()
    app.state.transcription_token_limiter = TranscriptionTokenLimiter(
        limit=1,
        window_seconds=60,
    )

    first = client.post(
        "/api/v1/integrations/elevenlabs/test",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
    )
    second = client.post(
        "/api/v1/integrations/elevenlabs/test",
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex},
    )
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["code"] == "TRANSCRIPTION_TOKEN_RATE_LIMITED"
    with app.state.session_factory() as db:
        rows = db.scalars(
            select(AuditEvent).where(
                AuditEvent.action == "integration.elevenlabs.test"
            )
        ).all()
        assert [row.outcome for row in rows] == ["success", "failure"]


def test_security_headers_allow_only_official_scribe_socket_and_self_microphone(client):
    response = client.get("/api/v1/health")
    assert response.headers["Permissions-Policy"] == (
        "camera=(), microphone=(self), geolocation=()"
    )
    csp = response.headers["Content-Security-Policy"]
    assert "connect-src 'self' wss://api.elevenlabs.io" in csp
    assert "script-src 'self'" in csp
    assert "https://api.elevenlabs.io" not in csp
