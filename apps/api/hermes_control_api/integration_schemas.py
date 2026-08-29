from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, SecretStr

from .schemas import ApiModel


class ElevenLabsIntegrationView(ApiModel):
    configured: bool
    provider: Literal["elevenlabs"] = "elevenlabs"
    model_id: Literal["scribe_v2_realtime"] = "scribe_v2_realtime"


class ElevenLabsKeyMutation(ApiModel):
    # Deliberately unconstrained here: Pydantic must wrap the raw value before
    # validation so a validation error can never echo a long-lived credential.
    api_key: SecretStr


class ElevenLabsIntegrationTestView(ApiModel):
    ok: Literal[True] = True
    provider: Literal["elevenlabs"] = "elevenlabs"
    model_id: Literal["scribe_v2_realtime"] = "scribe_v2_realtime"


class TranscriptionTokenRequest(ApiModel):
    # A chat need not exist before dictation starts. These optional hints are
    # intentionally not sent to the token endpoint; language is applied by the
    # browser during the subsequent WebSocket handshake.
    session_id: str | None = Field(default=None, min_length=1, max_length=255)
    language_code: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$",
    )


class TranscriptionTokenView(ApiModel):
    token: str
    expires_at: datetime
    model_id: Literal["scribe_v2_realtime"] = "scribe_v2_realtime"
