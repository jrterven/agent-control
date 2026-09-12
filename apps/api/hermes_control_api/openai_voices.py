from __future__ import annotations

from typing import Literal, get_args


# Built-in voices from the official Live session creation contract. Custom
# voice IDs require a separate consent/ownership flow and are not selectable.
# https://developers.openai.com/api/reference/resources/live/methods/create
OpenAILiveVoiceId = Literal[
    "alloy", "ash", "ballad", "beacon", "bossa", "cedar", "cinder", "coral",
    "delta", "echo", "gleam", "marin", "meridian", "quartz", "ripple", "sage",
    "shimmer", "stone", "tempo", "verse", "vesper", "willow",
]
OPENAI_LIVE_VOICE_IDS: frozenset[str] = frozenset(get_args(OpenAILiveVoiceId))
OPENAI_LIVE_DEFAULT_VOICE_ID: OpenAILiveVoiceId = "marin"
OpenAILivePreviewLanguage = Literal["es", "en", "fr", "de", "pt"]
OPENAI_LIVE_PREVIEW_PHRASES: dict[OpenAILivePreviewLanguage, str] = {
    "es": "Hola, soy una voz de tu asistente. Puedo escucharte y ayudarte en tiempo real.",
    "en": "Hello, I am a voice for your assistant. I can listen and help you in real time.",
    "fr": "Bonjour, je suis une voix de votre assistant. Je peux vous écouter et vous aider en temps réel.",
    "de": "Hallo, ich bin eine Stimme für deinen Assistenten. Ich kann dir zuhören und in Echtzeit helfen.",
    "pt": "Olá, sou uma voz do seu assistente. Posso ouvir você e ajudar em tempo real.",
}
OPENAI_LIVE_VOICE_CHECK = "openai_voice_id IN (" + ", ".join(
    repr(voice) for voice in sorted(OPENAI_LIVE_VOICE_IDS)
) + ")"
