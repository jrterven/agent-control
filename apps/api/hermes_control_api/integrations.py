from __future__ import annotations

import hmac
import json
import math
import re
import time
import zlib
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import User, UserIntegration
from .security import SecretVault


ELEVENLABS_PROVIDER = "elevenlabs"
SCRIBE_REALTIME_MODEL_ID = "scribe_v2_realtime"
ELEVENLABS_REALTIME_TOKEN_URL = (
    "https://api.elevenlabs.io/v1/single-use-token/realtime_scribe"
)
ELEVENLABS_TOKEN_TTL = timedelta(minutes=15)
ELEVENLABS_MAX_TOKEN_RESPONSE_BYTES = 16_384


class IntegrationError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.public_message = message
        self.retryable = retryable
        self.retry_after = retry_after


class InvalidIntegrationKey(IntegrationError):
    def __init__(self) -> None:
        super().__init__(
            status_code=422,
            code="INVALID_INTEGRATION_KEY",
            message="The integration credential has an invalid format",
        )


class IntegrationNotConfigured(IntegrationError):
    def __init__(self) -> None:
        super().__init__(
            status_code=409,
            code="INTEGRATION_NOT_CONFIGURED",
            message="Speech transcription is not configured for this user",
        )


class IntegrationSecretUnavailable(IntegrationError):
    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            code="INTEGRATION_SECRET_UNAVAILABLE",
            message="The speech transcription credential is unavailable",
            retryable=False,
        )


class TranscriptionTokenRateLimited(IntegrationError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(
            status_code=429,
            code="TRANSCRIPTION_TOKEN_RATE_LIMITED",
            message="Too many transcription token requests",
            retryable=True,
            retry_after=retry_after,
        )


def _safe_retry_after(value: str | None) -> int | None:
    try:
        parsed = math.ceil(float(value or ""))
    except (TypeError, ValueError):
        return None
    return parsed if 1 <= parsed <= 3_600 else None


class ElevenLabsScribeClient:
    """Minimal client for the official single-use Scribe token contract.

    An ``httpx.AsyncClient`` may be injected for deterministic tests. The
    default path creates a short-lived client with redirects disabled so the
    long-lived API key is sent only to the fixed official origin.
    """

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http_client = http_client

    @staticmethod
    def _provider_rejected() -> IntegrationError:
        return IntegrationError(
            status_code=502,
            code="TRANSCRIPTION_PROVIDER_REJECTED",
            message="The transcription provider rejected the token request",
        )

    @staticmethod
    async def _bounded_response_body(response: httpx.Response) -> bytes:
        """Read at most 16 KiB on the wire and after content decoding.

        ``httpx.Response.content`` materializes an unbounded decoded response.
        Reading raw chunks first lets us stop the transport as soon as its
        compressed representation exceeds the contract. Decompression is then
        performed with an output ceiling so a small compression bomb cannot
        bypass the same limit.
        """

        # A preloaded response is useful for an injected MockTransport in
        # deterministic tests. HTTPX has already decoded it, so only the
        # decoded ceiling remains meaningful; the production network path is
        # always unconsumed because ``AsyncClient.stream`` is used below.
        if response.is_stream_consumed:
            if len(response.content) > ELEVENLABS_MAX_TOKEN_RESPONSE_BYTES:
                raise ElevenLabsScribeClient._provider_rejected()
            return response.content

        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                raise ElevenLabsScribeClient._provider_rejected() from None
            if (
                declared_length < 0
                or declared_length > ELEVENLABS_MAX_TOKEN_RESPONSE_BYTES
            ):
                raise ElevenLabsScribeClient._provider_rejected()

        raw_body = bytearray()
        async for chunk in response.aiter_raw():
            if len(raw_body) + len(chunk) > ELEVENLABS_MAX_TOKEN_RESPONSE_BYTES:
                raise ElevenLabsScribeClient._provider_rejected()
            raw_body.extend(chunk)

        content_encoding = response.headers.get("content-encoding", "").strip().lower()
        if content_encoding in {"", "identity"}:
            return bytes(raw_body)
        if content_encoding == "gzip":
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif content_encoding == "deflate":
            decoder = zlib.decompressobj()
        else:
            raise ElevenLabsScribeClient._provider_rejected()

        try:
            decoded = decoder.decompress(
                bytes(raw_body),
                ELEVENLABS_MAX_TOKEN_RESPONSE_BYTES + 1,
            )
            if (
                len(decoded) > ELEVENLABS_MAX_TOKEN_RESPONSE_BYTES
                or decoder.unconsumed_tail
            ):
                raise ElevenLabsScribeClient._provider_rejected()
            decoded += decoder.flush(
                ELEVENLABS_MAX_TOKEN_RESPONSE_BYTES + 1 - len(decoded)
            )
        except zlib.error:
            raise ElevenLabsScribeClient._provider_rejected() from None
        if (
            len(decoded) > ELEVENLABS_MAX_TOKEN_RESPONSE_BYTES
            or not decoder.eof
            or decoder.unused_data
        ):
            raise ElevenLabsScribeClient._provider_rejected()
        return decoded

    async def _issue_with_client(
        self,
        client: httpx.AsyncClient,
        *,
        headers: dict[str, str],
        api_key: str,
    ) -> str:
        async with client.stream(
            "POST",
            ELEVENLABS_REALTIME_TOKEN_URL,
            headers=headers,
            follow_redirects=False,
        ) as response:
            if response.status_code in {401, 403}:
                raise IntegrationError(
                    status_code=409,
                    code="INTEGRATION_CREDENTIAL_REJECTED",
                    message="The transcription provider rejected the configured credential",
                )
            if response.status_code == 402:
                raise IntegrationError(
                    status_code=409,
                    code="TRANSCRIPTION_QUOTA_EXCEEDED",
                    message="The transcription provider quota is unavailable",
                )
            if response.status_code == 429:
                raise IntegrationError(
                    status_code=429,
                    code="TRANSCRIPTION_PROVIDER_RATE_LIMITED",
                    message="The transcription provider rate limit was reached",
                    retryable=True,
                    retry_after=_safe_retry_after(response.headers.get("retry-after")),
                )
            if response.status_code >= 500:
                raise IntegrationError(
                    status_code=503,
                    code="TRANSCRIPTION_PROVIDER_UNAVAILABLE",
                    message="The transcription provider is unavailable",
                    retryable=True,
                )
            if response.status_code != 200:
                raise self._provider_rejected()

            body = await self._bounded_response_body(response)
            try:
                payload = json.loads(body)
            except (TypeError, ValueError):
                payload = None
            token = payload.get("token") if isinstance(payload, dict) else None
            if (
                not isinstance(token, str)
                or not 8 <= len(token) <= 4_096
                or re.fullmatch(r"[A-Za-z0-9._~-]+", token) is None
                or hmac.compare_digest(token, api_key)
            ):
                raise IntegrationError(
                    status_code=502,
                    code="TRANSCRIPTION_PROVIDER_INVALID_RESPONSE",
                    message="The transcription provider returned an invalid response",
                )
            return token

    async def issue_realtime_token(self, api_key: str) -> str:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": "Agent-Control/0.1",
            "xi-api-key": api_key,
        }
        try:
            if self._http_client is not None:
                return await self._issue_with_client(
                    self._http_client,
                    headers=headers,
                    api_key=api_key,
                )
            timeout = httpx.Timeout(10.0, connect=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await self._issue_with_client(
                    client,
                    headers=headers,
                    api_key=api_key,
                )
        except (httpx.TimeoutException, httpx.NetworkError):
            raise IntegrationError(
                status_code=503,
                code="TRANSCRIPTION_PROVIDER_UNAVAILABLE",
                message="The transcription provider is unavailable",
                retryable=True,
            ) from None
        except httpx.RequestError:
            raise IntegrationError(
                status_code=502,
                code="TRANSCRIPTION_PROVIDER_ERROR",
                message="The transcription provider request failed",
                retryable=True,
            ) from None


class UserIntegrationService:
    def __init__(self, vault: SecretVault) -> None:
        self._vault = vault

    @staticmethod
    def _aad(owner_id: str) -> str:
        return f"user-integration:{owner_id}:{ELEVENLABS_PROVIDER}:api-key"

    @staticmethod
    def _row(db: Session, owner_id: str) -> UserIntegration | None:
        return db.scalar(
            select(UserIntegration).where(
                UserIntegration.owner_id == owner_id,
                UserIntegration.provider == ELEVENLABS_PROVIDER,
            )
        )

    def is_configured(self, db: Session, owner: User) -> bool:
        row = self._row(db, owner.id)
        return bool(row and row.api_key_ciphertext)

    def set_api_key(self, db: Session, owner: User, api_key: str) -> UserIntegration:
        if (
            not 16 <= len(api_key) <= 512
            or any(ord(character) < 33 or ord(character) > 126 for character in api_key)
        ):
            raise InvalidIntegrationKey()
        ciphertext = self._vault.encrypt(api_key, aad=self._aad(owner.id))
        if ciphertext is None:
            raise InvalidIntegrationKey()
        row = self._row(db, owner.id)
        if row is None:
            row = UserIntegration(
                owner_id=owner.id,
                provider=ELEVENLABS_PROVIDER,
                api_key_ciphertext=ciphertext,
            )
            db.add(row)
        else:
            row.api_key_ciphertext = ciphertext
        db.flush()
        return row

    def delete_api_key(self, db: Session, owner: User) -> bool:
        row = self._row(db, owner.id)
        if row is None:
            return False
        db.delete(row)
        db.flush()
        return True

    def api_key(self, db: Session, owner: User) -> str:
        row = self._row(db, owner.id)
        if row is None:
            raise IntegrationNotConfigured()
        try:
            value = self._vault.decrypt(
                row.api_key_ciphertext,
                aad=self._aad(owner.id),
            )
        except ValueError:
            raise IntegrationSecretUnavailable() from None
        if value is None:
            raise IntegrationSecretUnavailable()
        return value


class TranscriptionTokenLimiter:
    def __init__(self, *, limit: int, window_seconds: int) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._buckets: dict[str, deque[float]] = defaultdict(deque)

    def consume(self, owner_id: str, *, now: float | None = None) -> None:
        current = time.monotonic() if now is None else now
        cutoff = current - self._window_seconds
        bucket = self._buckets[owner_id]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self._limit:
            retry_after = max(1, math.ceil(bucket[0] + self._window_seconds - current))
            raise TranscriptionTokenRateLimited(retry_after)
        bucket.append(current)
        if len(self._buckets) > 4_096:
            for key in list(self._buckets):
                candidate = self._buckets[key]
                while candidate and candidate[0] <= cutoff:
                    candidate.popleft()
                if not candidate:
                    del self._buckets[key]


def token_expiration(*, now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current + ELEVENLABS_TOKEN_TTL
