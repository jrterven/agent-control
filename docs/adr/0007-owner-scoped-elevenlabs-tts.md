# ADR 0007: owner-scoped ElevenLabs response playback

## Status

Accepted.

## Context

Users already provide an owner-scoped, encrypted ElevenLabs key for Scribe
dictation. They also need to hear a newly generated answer with low latency and
replay any completed assistant message without exposing that reusable key to
the PWA.

## Decision

- The existing encrypted key is reused; no second credential is created.
- The user selects a voice from the authenticated ElevenLabs v2 voice catalog.
  Only the voice ID and display name are stored as non-secret owner preferences.
- Replacing the key clears the voice until it is revalidated against the new
  account.
- Live answers use `eleven_flash_v2_5` and the official TTS WebSocket. FastAPI
  mints a short-lived `tts_websocket` single-use token; the PWA never receives
  the reusable API key. Incremental normalized answer text is sent as it is
  generated and MP3 chunks are appended to a MediaSource buffer.
- Completed answers use the official HTTP streaming TTS endpoint through an
  authenticated FastAPI proxy. Request text is capped at 20,000 characters and
  audio at 50 MiB. The response is `no-store` and is never written to the local
  database or audit log.
- Live reading defaults off and is stored only as a non-secret device
  preference. Historical playback requires an explicit message-level tap.
- Speech requests use a separate per-owner rate limiter. TTS ticket and audio
  routes bypass the general idempotency body recorder because tokens are
  single-use and binary streaming responses must never enter that ledger.

## Consequences

The provider receives text only after a deliberate playback choice, and usage
is charged to the user's own account. Mobile playback still depends on browser
media/autoplay support. The exact ElevenLabs WSS origin remains the only
external browser connection allowed by CSP.
