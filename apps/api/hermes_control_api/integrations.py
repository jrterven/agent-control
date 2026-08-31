from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import time
import zlib
from collections import defaultdict, deque
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import (
    ProfileRef,
    ProfileVoicePreference,
    SessionLink,
    User,
    UserIntegration,
)
from .security import SecretVault


ELEVENLABS_PROVIDER = "elevenlabs"
SCRIBE_REALTIME_MODEL_ID = "scribe_v2_realtime"
ElevenLabsTtsModelId = Literal["eleven_flash_v2_5", "eleven_multilingual_v2"]
ELEVENLABS_TTS_MODEL_ID: ElevenLabsTtsModelId = "eleven_flash_v2_5"
ELEVENLABS_TTS_MODEL_IDS = frozenset(
    {"eleven_flash_v2_5", "eleven_multilingual_v2"}
)
ELEVENLABS_TTS_OUTPUT_FORMAT = "mp3_44100_128"
ELEVENLABS_REALTIME_TOKEN_URL = (
    "https://api.elevenlabs.io/v1/single-use-token/realtime_scribe"
)
ELEVENLABS_TTS_TOKEN_URL = (
    "https://api.elevenlabs.io/v1/single-use-token/tts_websocket"
)
ELEVENLABS_VOICES_URL = "https://api.elevenlabs.io/v2/voices"
ELEVENLABS_TTS_STREAM_URL = (
    "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
)
ELEVENLABS_TOKEN_TTL = timedelta(minutes=15)
ELEVENLABS_MAX_TOKEN_RESPONSE_BYTES = 16_384
ELEVENLABS_MAX_VOICE_RESPONSE_BYTES = 2 * 1024 * 1024
ELEVENLABS_MAX_AUDIO_RESPONSE_BYTES = 50 * 1024 * 1024
ELEVENLABS_MAX_PREVIEW_RESPONSE_BYTES = 10 * 1024 * 1024
ELEVENLABS_PREVIEW_HOST = "storage.googleapis.com"
ELEVENLABS_PREVIEW_PATH_PREFIX = "/eleven-public-prod/"


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
            message="ElevenLabs is not configured for this user",
        )


class IntegrationSecretUnavailable(IntegrationError):
    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            code="INTEGRATION_SECRET_UNAVAILABLE",
            message="The ElevenLabs credential is unavailable",
            retryable=False,
        )


class SpeechVoiceNotConfigured(IntegrationError):
    def __init__(self) -> None:
        super().__init__(
            status_code=409,
            code="SPEECH_VOICE_NOT_CONFIGURED",
            message="Choose an ElevenLabs voice before enabling speech playback",
        )


class SpeechVoiceUnavailable(IntegrationError):
    def __init__(self) -> None:
        super().__init__(
            status_code=422,
            code="SPEECH_VOICE_UNAVAILABLE",
            message="The selected ElevenLabs voice is not available to this account",
        )


class SpeechVoicePreviewUnavailable(IntegrationError):
    def __init__(self) -> None:
        super().__init__(
            status_code=422,
            code="SPEECH_VOICE_PREVIEW_UNAVAILABLE",
            message="The selected ElevenLabs voice does not provide a safe preview",
        )


class ProfileVoiceTargetNotFound(IntegrationError):
    def __init__(self) -> None:
        super().__init__(
            status_code=404,
            code="PROFILE_VOICE_TARGET_NOT_FOUND",
            message="The selected profile does not exist",
        )


class SpeechSessionNotFound(IntegrationError):
    def __init__(self) -> None:
        super().__init__(
            status_code=404,
            code="SPEECH_SESSION_NOT_FOUND",
            message="The selected chat does not exist",
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


class SpeechRateLimited(IntegrationError):
    def __init__(self, retry_after: int) -> None:
        super().__init__(
            status_code=429,
            code="SPEECH_RATE_LIMITED",
            message="Too many speech requests",
            retryable=True,
            retry_after=retry_after,
        )


def _safe_retry_after(value: str | None) -> int | None:
    try:
        parsed = math.ceil(float(value or ""))
    except (TypeError, ValueError):
        return None
    return parsed if 1 <= parsed <= 3_600 else None


def _safe_voice_preview_url(value: object) -> str | None:
    """Accept only ElevenLabs' documented public preview bucket.

    Voice metadata is authenticated but remains upstream-controlled input. The
    URL is never exposed to the browser and is still constrained here so the
    preview proxy cannot become an SSRF primitive.
    """

    if not isinstance(value, str) or not 1 <= len(value) <= 2_048:
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme != "https"
        or (parts.hostname or "").lower() != ELEVENLABS_PREVIEW_HOST
        or port not in {None, 443}
        or parts.username is not None
        or parts.password is not None
        or not parts.path.startswith(ELEVENLABS_PREVIEW_PATH_PREFIX)
        or not parts.path.lower().endswith(".mp3")
        or parts.fragment
    ):
        return None
    return value


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


class ElevenLabsSpeechClient:
    """Bounded client for voice discovery, TTS tickets and audio streaming."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._http_client = http_client

    @staticmethod
    def _headers(api_key: str, accept: str = "application/json") -> dict[str, str]:
        return {
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
            "Content-Type": "application/json",
            "User-Agent": "Agent-Control/0.1",
            "xi-api-key": api_key,
        }

    @staticmethod
    def _provider_error(response: httpx.Response, *, operation: str) -> IntegrationError:
        if response.status_code in {401, 403}:
            return IntegrationError(
                status_code=409,
                code="INTEGRATION_CREDENTIAL_REJECTED",
                message="ElevenLabs rejected the configured credential",
            )
        if response.status_code == 402:
            return IntegrationError(
                status_code=409,
                code="SPEECH_QUOTA_EXCEEDED",
                message="ElevenLabs speech quota is unavailable",
            )
        if response.status_code == 429:
            return IntegrationError(
                status_code=429,
                code="SPEECH_PROVIDER_RATE_LIMITED",
                message="ElevenLabs rate limited the speech request",
                retryable=True,
                retry_after=_safe_retry_after(response.headers.get("retry-after")),
            )
        if response.status_code >= 500:
            return IntegrationError(
                status_code=503,
                code="SPEECH_PROVIDER_UNAVAILABLE",
                message="ElevenLabs speech is unavailable",
                retryable=True,
            )
        return IntegrationError(
            status_code=502,
            code=f"SPEECH_PROVIDER_{operation.upper()}_REJECTED",
            message=f"ElevenLabs rejected the speech {operation} request",
        )

    @staticmethod
    async def _bounded_json(response: httpx.Response, *, maximum: int) -> object:
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                if not 0 <= int(declared) <= maximum:
                    raise ValueError
            except ValueError:
                raise IntegrationError(
                    status_code=502,
                    code="SPEECH_PROVIDER_INVALID_RESPONSE",
                    message="ElevenLabs returned an invalid response",
                ) from None
        body = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65_536):
            if len(body) + len(chunk) > maximum:
                raise IntegrationError(
                    status_code=502,
                    code="SPEECH_PROVIDER_INVALID_RESPONSE",
                    message="ElevenLabs returned an invalid response",
                )
            body.extend(chunk)
        try:
            return json.loads(body)
        except (TypeError, ValueError):
            raise IntegrationError(
                status_code=502,
                code="SPEECH_PROVIDER_INVALID_RESPONSE",
                message="ElevenLabs returned an invalid response",
            ) from None

    async def _with_json_response(
        self,
        method: str,
        url: str,
        *,
        api_key: str,
        params: dict[str, str | int] | None = None,
        maximum: int = ELEVENLABS_MAX_VOICE_RESPONSE_BYTES,
    ) -> object:
        async def execute(client: httpx.AsyncClient) -> object:
            async with client.stream(
                method,
                url,
                params=params,
                headers=self._headers(api_key),
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    raise self._provider_error(response, operation="configuration")
                return await self._bounded_json(
                    response,
                    maximum=maximum,
                )

        try:
            if self._http_client is not None:
                return await execute(self._http_client)
            timeout = httpx.Timeout(12.0, connect=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                return await execute(client)
        except IntegrationError:
            raise
        except (httpx.TimeoutException, httpx.NetworkError):
            raise IntegrationError(
                status_code=503,
                code="SPEECH_PROVIDER_UNAVAILABLE",
                message="ElevenLabs speech is unavailable",
                retryable=True,
            ) from None
        except httpx.RequestError:
            raise IntegrationError(
                status_code=502,
                code="SPEECH_PROVIDER_ERROR",
                message="The ElevenLabs speech request failed",
                retryable=True,
            ) from None

    async def list_voices(self, api_key: str) -> list[dict[str, object]]:
        voices: list[dict[str, object]] = []
        next_page_token: str | None = None
        for _ in range(5):
            params: dict[str, str | int] = {"page_size": 100}
            if next_page_token:
                params["next_page_token"] = next_page_token
            payload = await self._with_json_response(
                "GET",
                ELEVENLABS_VOICES_URL,
                api_key=api_key,
                params=params,
            )
            rows = payload.get("voices") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                raise IntegrationError(
                    status_code=502,
                    code="SPEECH_PROVIDER_INVALID_RESPONSE",
                    message="ElevenLabs returned an invalid voice catalog",
                )
            for row in rows[:100]:
                if not isinstance(row, dict):
                    continue
                voice_id = row.get("voice_id")
                name = row.get("name")
                if (
                    not isinstance(voice_id, str)
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", voice_id) is None
                    or not isinstance(name, str)
                    or not name.strip()
                ):
                    continue
                labels = row.get("labels")
                safe_labels = {
                    str(key)[:40]: str(value)[:80]
                    for key, value in (labels.items() if isinstance(labels, dict) else [])
                    if isinstance(key, str) and isinstance(value, str)
                }
                preview_url = _safe_voice_preview_url(row.get("preview_url"))
                voices.append(
                    {
                        "id": voice_id,
                        "name": name.strip()[:200],
                        "category": str(row.get("category"))[:80]
                        if isinstance(row.get("category"), str)
                        else None,
                        "labels": safe_labels,
                        "preview_available": preview_url is not None,
                        "preview_url": preview_url,
                    }
                )
            has_more = payload.get("has_more") is True if isinstance(payload, dict) else False
            candidate = payload.get("next_page_token") if isinstance(payload, dict) else None
            if (
                not has_more
                or not isinstance(candidate, str)
                or not 1 <= len(candidate) <= 512
            ):
                break
            next_page_token = candidate
        return sorted(voices, key=lambda item: str(item["name"]).casefold())

    async def issue_realtime_token(self, api_key: str) -> str:
        payload = await self._with_json_response(
            "POST",
            ELEVENLABS_TTS_TOKEN_URL,
            api_key=api_key,
            maximum=ELEVENLABS_MAX_TOKEN_RESPONSE_BYTES,
        )
        token = payload.get("token") if isinstance(payload, dict) else None
        if (
            not isinstance(token, str)
            or not 8 <= len(token) <= 4_096
            or re.fullmatch(r"[A-Za-z0-9._~-]+", token) is None
            or hmac.compare_digest(token, api_key)
        ):
            raise IntegrationError(
                status_code=502,
                code="SPEECH_PROVIDER_INVALID_RESPONSE",
                message="ElevenLabs returned an invalid speech token",
            )
        return token

    async def open_audio_stream(
        self,
        api_key: str,
        *,
        voice_id: str,
        model_id: ElevenLabsTtsModelId,
        text: str,
    ) -> tuple[httpx.Response, httpx.AsyncClient | None]:
        own_client: httpx.AsyncClient | None = None
        client = self._http_client
        if client is None:
            own_client = httpx.AsyncClient(
                timeout=httpx.Timeout(90.0, connect=5.0),
                follow_redirects=False,
            )
            client = own_client
        request = client.build_request(
            "POST",
            ELEVENLABS_TTS_STREAM_URL.format(voice_id=voice_id),
            params={"output_format": ELEVENLABS_TTS_OUTPUT_FORMAT},
            headers=self._headers(api_key, "audio/mpeg"),
            json={"text": text, "model_id": model_id},
        )
        try:
            response = await client.send(request, stream=True, follow_redirects=False)
        except (httpx.TimeoutException, httpx.NetworkError):
            if own_client is not None:
                await own_client.aclose()
            raise IntegrationError(
                status_code=503,
                code="SPEECH_PROVIDER_UNAVAILABLE",
                message="ElevenLabs speech is unavailable",
                retryable=True,
            ) from None
        except httpx.RequestError:
            if own_client is not None:
                await own_client.aclose()
            raise IntegrationError(
                status_code=502,
                code="SPEECH_PROVIDER_ERROR",
                message="The ElevenLabs speech request failed",
                retryable=True,
            ) from None
        if response.status_code != 200:
            error = self._provider_error(response, operation="generation")
            await response.aclose()
            if own_client is not None:
                await own_client.aclose()
            raise error
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        if content_type not in {"audio/mpeg", "audio/mp3", "application/octet-stream"}:
            await response.aclose()
            if own_client is not None:
                await own_client.aclose()
            raise IntegrationError(
                status_code=502,
                code="SPEECH_PROVIDER_INVALID_RESPONSE",
                message="ElevenLabs returned an invalid audio response",
            )
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                valid_length = 0 <= int(declared) <= ELEVENLABS_MAX_AUDIO_RESPONSE_BYTES
            except ValueError:
                valid_length = False
            if not valid_length:
                await response.aclose()
                if own_client is not None:
                    await own_client.aclose()
                raise IntegrationError(
                    status_code=502,
                    code="SPEECH_PROVIDER_INVALID_RESPONSE",
                    message="ElevenLabs returned an invalid audio response",
                )
        return response, own_client

    async def open_preview_stream(
        self,
        preview_url: str,
    ) -> tuple[httpx.Response, httpx.AsyncClient | None]:
        safe_url = _safe_voice_preview_url(preview_url)
        if safe_url is None:
            raise SpeechVoicePreviewUnavailable()

        own_client: httpx.AsyncClient | None = None
        client = self._http_client
        if client is None:
            own_client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=5.0),
                follow_redirects=False,
            )
            client = own_client
        request = client.build_request(
            "GET",
            safe_url,
            headers={
                "Accept": "audio/mpeg",
                "Accept-Encoding": "identity",
                "User-Agent": "Agent-Control/0.1",
            },
        )
        try:
            response = await client.send(request, stream=True, follow_redirects=False)
        except (httpx.TimeoutException, httpx.NetworkError):
            if own_client is not None:
                await own_client.aclose()
            raise IntegrationError(
                status_code=503,
                code="SPEECH_PROVIDER_UNAVAILABLE",
                message="The ElevenLabs voice preview is unavailable",
                retryable=True,
            ) from None
        except httpx.RequestError:
            if own_client is not None:
                await own_client.aclose()
            raise IntegrationError(
                status_code=502,
                code="SPEECH_PROVIDER_ERROR",
                message="The ElevenLabs voice preview request failed",
                retryable=True,
            ) from None
        if response.status_code != 200:
            await response.aclose()
            if own_client is not None:
                await own_client.aclose()
            raise SpeechVoicePreviewUnavailable()
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        # A small set of ElevenLabs' documented public GCS previews are valid
        # MPEG audio but carry the legacy ``text/plain`` object metadata. The
        # URL has already been constrained to ElevenLabs' fixed public bucket
        # and an ``.mp3`` path, so accepting that one compatibility MIME here
        # does not turn the proxy into a general content fetcher.
        if content_type not in {
            "audio/mpeg",
            "audio/mp3",
            "application/octet-stream",
            "text/plain",
        }:
            await response.aclose()
            if own_client is not None:
                await own_client.aclose()
            raise SpeechVoicePreviewUnavailable()
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                valid_length = 0 <= int(declared) <= ELEVENLABS_MAX_PREVIEW_RESPONSE_BYTES
            except ValueError:
                valid_length = False
            if not valid_length:
                await response.aclose()
                if own_client is not None:
                    await own_client.aclose()
                raise SpeechVoicePreviewUnavailable()
        return response, own_client

    @staticmethod
    async def audio_chunks(
        response: httpx.Response,
        own_client: httpx.AsyncClient | None,
        maximum: int = ELEVENLABS_MAX_AUDIO_RESPONSE_BYTES,
    ) -> AsyncIterator[bytes]:
        total = 0
        try:
            async for chunk in response.aiter_bytes(chunk_size=65_536):
                total += len(chunk)
                if total > maximum:
                    return
                if chunk:
                    yield chunk
        finally:
            await response.aclose()
            if own_client is not None:
                await own_client.aclose()


class UserIntegrationService:
    def __init__(self, vault: SecretVault) -> None:
        self._vault = vault

    @staticmethod
    def _aad(owner_id: str) -> str:
        return f"user-integration:{owner_id}:{ELEVENLABS_PROVIDER}:api-key"

    @staticmethod
    def _key_fingerprint(row: UserIntegration) -> str:
        return hashlib.sha256(row.api_key_ciphertext.encode("utf-8")).hexdigest()

    @classmethod
    def _current_preference(
        cls,
        row: UserIntegration | None,
        preference: ProfileVoicePreference | None,
    ) -> ProfileVoicePreference | None:
        if row is None or preference is None:
            return None
        if not hmac.compare_digest(
            preference.api_key_fingerprint,
            cls._key_fingerprint(row),
        ):
            return None
        return preference

    @staticmethod
    def _row(db: Session, owner_id: str) -> UserIntegration | None:
        return db.scalar(
            select(UserIntegration).where(
                UserIntegration.owner_id == owner_id,
                UserIntegration.provider == ELEVENLABS_PROVIDER,
            )
        )

    @staticmethod
    def _profile(db: Session, profile_id: str) -> ProfileRef:
        profile = db.get(ProfileRef, profile_id)
        if profile is None:
            raise ProfileVoiceTargetNotFound()
        return profile

    @staticmethod
    def _preference(
        db: Session,
        *,
        integration_id: str,
        profile_id: str,
    ) -> ProfileVoicePreference | None:
        return db.scalar(
            select(ProfileVoicePreference).where(
                ProfileVoicePreference.integration_id == integration_id,
                ProfileVoicePreference.profile_id == profile_id,
            )
        )

    def is_configured(self, db: Session, owner: User) -> bool:
        row = self._row(db, owner.id)
        return bool(row and row.api_key_ciphertext)

    def configuration(self, db: Session, owner: User) -> dict[str, object]:
        row = self._row(db, owner.id)
        configured = bool(row and row.api_key_ciphertext)
        return {
            "configured": configured,
            "tts_model_id": (
                row.tts_model_id
                if row and row.tts_model_id in ELEVENLABS_TTS_MODEL_IDS
                else ELEVENLABS_TTS_MODEL_ID
            ),
            "voice_id": row.tts_voice_id if row else None,
            "voice_name": row.tts_voice_name if row else None,
            "speech_available": bool(configured and row and row.tts_voice_id),
        }

    def profile_configuration(
        self,
        db: Session,
        owner: User,
        profile_id: str,
    ) -> dict[str, object]:
        profile = self._profile(db, profile_id)
        row = self._row(db, owner.id)
        preference = (
            self._preference(
                db,
                integration_id=row.id,
                profile_id=profile.id,
            )
            if row is not None
            else None
        )
        preference = self._current_preference(row, preference)
        return self._effective_profile_configuration(profile, row, preference)

    @staticmethod
    def _effective_profile_configuration(
        profile: ProfileRef,
        row: UserIntegration | None,
        preference: ProfileVoicePreference | None,
    ) -> dict[str, object]:
        inherited = preference is None
        voice_id = preference.tts_voice_id if preference else (row.tts_voice_id if row else None)
        voice_name = (
            preference.tts_voice_name if preference else (row.tts_voice_name if row else None)
        )
        raw_model_id = (
            preference.tts_model_id
            if preference is not None
            else (row.tts_model_id if row is not None else ELEVENLABS_TTS_MODEL_ID)
        )
        model_id = (
            raw_model_id
            if raw_model_id in ELEVENLABS_TTS_MODEL_IDS
            else ELEVENLABS_TTS_MODEL_ID
        )
        configured = bool(row and row.api_key_ciphertext)
        return {
            "profile_id": profile.id,
            "gateway_id": profile.gateway_id,
            "profile_name": profile.profile_name,
            "tts_model_id": model_id,
            "voice_id": voice_id,
            "voice_name": voice_name,
            "speech_available": bool(configured and voice_id),
            "inherited": inherited,
        }

    def profile_configurations(
        self,
        db: Session,
        owner: User,
        profiles: list[ProfileRef],
    ) -> dict[str, dict[str, object]]:
        row = self._row(db, owner.id)
        preferences: dict[str, ProfileVoicePreference] = {}
        if row is not None and profiles:
            preferences = {
                preference.profile_id: current
                for preference in db.scalars(
                    select(ProfileVoicePreference).where(
                        ProfileVoicePreference.integration_id == row.id,
                        ProfileVoicePreference.profile_id.in_(
                            [profile.id for profile in profiles]
                        ),
                    )
                ).all()
                if (current := self._current_preference(row, preference)) is not None
            }
        return {
            profile.id: self._effective_profile_configuration(
                profile,
                row,
                preferences.get(profile.id),
            )
            for profile in profiles
        }

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
            db.execute(
                delete(ProfileVoicePreference).where(
                    ProfileVoicePreference.integration_id == row.id
                )
            )
            row.api_key_ciphertext = ciphertext
            # A replacement credential can point at another workspace. Never
            # assume that the old voice remains authorized by the new key.
            row.tts_voice_id = None
            row.tts_voice_name = None
        db.flush()
        return row

    def set_profile_voice(
        self,
        db: Session,
        owner: User,
        *,
        profile_id: str,
        voice_id: str,
        voice_name: str,
        model_id: ElevenLabsTtsModelId | None,
    ) -> ProfileVoicePreference:
        profile = self._profile(db, profile_id)
        row = self._row(db, owner.id)
        if row is None:
            raise IntegrationNotConfigured()
        preference = self._preference(
            db,
            integration_id=row.id,
            profile_id=profile.id,
        )
        if preference is None:
            preference = ProfileVoicePreference(
                integration_id=row.id,
                profile_id=profile.id,
                api_key_fingerprint=self._key_fingerprint(row),
                tts_voice_id=voice_id,
                tts_voice_name=voice_name[:200],
                tts_model_id=(
                    model_id
                    or (
                        row.tts_model_id
                        if row.tts_model_id in ELEVENLABS_TTS_MODEL_IDS
                        else ELEVENLABS_TTS_MODEL_ID
                    )
                ),
            )
            db.add(preference)
        else:
            preference.api_key_fingerprint = self._key_fingerprint(row)
            preference.tts_voice_id = voice_id
            preference.tts_voice_name = voice_name[:200]
            if model_id is not None:
                preference.tts_model_id = model_id
        db.flush()
        return preference

    def delete_profile_voice(
        self,
        db: Session,
        owner: User,
        *,
        profile_id: str,
    ) -> bool:
        profile = self._profile(db, profile_id)
        row = self._row(db, owner.id)
        if row is None:
            return False
        preference = self._preference(
            db,
            integration_id=row.id,
            profile_id=profile.id,
        )
        if preference is None:
            return False
        db.delete(preference)
        db.flush()
        return True

    def set_voice(
        self,
        db: Session,
        owner: User,
        *,
        voice_id: str,
        voice_name: str,
        model_id: ElevenLabsTtsModelId | None,
    ) -> UserIntegration:
        row = self._row(db, owner.id)
        if row is None:
            raise IntegrationNotConfigured()
        row.tts_voice_id = voice_id
        row.tts_voice_name = voice_name[:200]
        if model_id is not None:
            row.tts_model_id = model_id
        db.flush()
        return row

    def speech_configuration(
        self,
        db: Session,
        owner: User,
        *,
        session_id: str | None = None,
    ) -> tuple[str, str, ElevenLabsTtsModelId]:
        row = self._row(db, owner.id)
        if row is None:
            raise IntegrationNotConfigured()
        preference: ProfileVoicePreference | None = None
        if session_id is not None:
            session_link = db.scalar(
                select(SessionLink).where(
                    SessionLink.id == session_id,
                    SessionLink.owner_id == owner.id,
                )
            )
            if session_link is None:
                raise SpeechSessionNotFound()
            profile = db.scalar(
                select(ProfileRef).where(
                    ProfileRef.gateway_id == session_link.gateway_id,
                    ProfileRef.profile_name == session_link.profile_name,
                )
            )
            if profile is None:
                raise SpeechSessionNotFound()
            preference = self._preference(
                db,
                integration_id=row.id,
                profile_id=profile.id,
            )
            preference = self._current_preference(row, preference)
        voice_id = preference.tts_voice_id if preference else row.tts_voice_id
        voice_name = preference.tts_voice_name if preference else row.tts_voice_name
        raw_model_id = preference.tts_model_id if preference else row.tts_model_id
        if not voice_id:
            raise SpeechVoiceNotConfigured()
        model_id: ElevenLabsTtsModelId = (
            raw_model_id
            if raw_model_id in ELEVENLABS_TTS_MODEL_IDS
            else ELEVENLABS_TTS_MODEL_ID
        )
        return voice_id, voice_name or voice_id, model_id

    def delete_api_key(self, db: Session, owner: User) -> bool:
        row = self._row(db, owner.id)
        if row is None:
            return False
        db.execute(
            delete(ProfileVoicePreference).where(
                ProfileVoicePreference.integration_id == row.id
            )
        )
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


class SpeechRateLimiter(TranscriptionTokenLimiter):
    def consume(self, owner_id: str, *, now: float | None = None) -> None:
        try:
            super().consume(owner_id, now=now)
        except TranscriptionTokenRateLimited as exc:
            raise SpeechRateLimited(exc.retry_after or 1) from None


def token_expiration(*, now: datetime | None = None) -> datetime:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current + ELEVENLABS_TOKEN_TTL
