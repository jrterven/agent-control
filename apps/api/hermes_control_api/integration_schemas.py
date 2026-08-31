from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, SecretStr, field_validator

from .schemas import ApiModel
from .integrations import ELEVENLABS_TTS_MODEL_ID, ElevenLabsTtsModelId


class ElevenLabsIntegrationView(ApiModel):
    configured: bool
    provider: Literal["elevenlabs"] = "elevenlabs"
    model_id: Literal["scribe_v2_realtime"] = "scribe_v2_realtime"
    tts_model_id: ElevenLabsTtsModelId = ELEVENLABS_TTS_MODEL_ID
    voice_id: str | None = None
    voice_name: str | None = None
    speech_available: bool = False


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


class ElevenLabsVoiceView(ApiModel):
    id: str
    name: str
    category: str | None = None
    labels: dict[str, str] = Field(default_factory=dict)
    preview_available: bool = False


class ElevenLabsVoiceListView(ApiModel):
    items: list[ElevenLabsVoiceView]


class ElevenLabsVoiceMutation(ApiModel):
    voice_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    # Optional for compatibility with an older cached PWA. Omitting it keeps
    # the current model instead of silently resetting the owner's preference.
    tts_model_id: ElevenLabsTtsModelId | None = None


class ElevenLabsProfileVoiceView(ApiModel):
    profile_id: str
    gateway_id: str
    profile_name: str
    tts_model_id: ElevenLabsTtsModelId = ELEVENLABS_TTS_MODEL_ID
    voice_id: str | None = None
    voice_name: str | None = None
    speech_available: bool = False
    inherited: bool = True


class SpeechTokenRequest(ApiModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=255)


class SpeechTokenView(ApiModel):
    token: str
    expires_at: datetime
    model_id: ElevenLabsTtsModelId = ELEVENLABS_TTS_MODEL_ID
    voice_id: str
    voice_name: str


class SpeechRequest(ApiModel):
    # The response is already visible to the authenticated user. It is sent
    # only after an explicit playback action and is never persisted or audited.
    text: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("text")
    @classmethod
    def meaningful_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Speech text cannot be empty")
        return normalized
