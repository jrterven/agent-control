# ADR 0006: Owner-scoped BYOK realtime transcription

- Status: accepted
- Date: 2026-08-29

## Context

Mobile users need dictation without exposing a reusable provider credential or
routing microphone audio through an agent runtime. Realtime transcription has a
different trust and latency profile from Hermes/OpenClaw traffic: the browser
must capture the microphone, and ElevenLabs supports a short-lived single-use
token that can authorize a direct WebSocket.

[ADR 0001](0001-backend-only-hermes-boundary.md) keeps all agent-provider URLs,
credentials and protocol frames behind FastAPI. Realtime transcription is not
an agent provider, but direct browser egress still requires an explicit, narrow
exception to the normal same-origin browser boundary.

## Decision

- ElevenLabs Scribe is a user-owned transcription integration. It is never
  attached to Hermes, OpenClaw, a gateway, an agent profile or a conversation.
- Each user's long-lived API key is write-only, encrypted by the Control vault
  with owner/provider/field-bound AAD, and decrypted in backend memory only for
  provider operations. Read responses expose configuration presence only.
- The public Control contract is limited to:
  - `GET /api/v1/integrations/elevenlabs`;
  - `PUT /api/v1/integrations/elevenlabs/key` with `{apiKey}`;
  - `POST /api/v1/integrations/elevenlabs/test`;
  - `DELETE /api/v1/integrations/elevenlabs/key`;
  - `POST /api/v1/realtime/transcription-token` with optional
    `{sessionId?, languageCode?}` and response `{token, expiresAt, modelId}`.
    Both request fields are validated hints only: Control does not forward them
    to token issuance or persist them in SQLite, idempotency state, logs or audit
    payloads.
- The integration mutations are owner-scoped and require authentication, CSRF
  and `Idempotency-Key`. Token issuance is owner-scoped, authenticated,
  CSRF-protected and rate-limited, but it explicitly rejects idempotent replay:
  neither its request result nor its token may enter the idempotency ledger.
- A transcription token is single-use, is kept only in process/browser memory,
  and is returned with `Cache-Control: no-store`. It must not appear in SQLite,
  browser persistence, service-worker caches, logs or audit payloads. The
  official Scribe protocol necessarily places it in the provider WebSocket query
  string. Control must therefore neither persist nor log that complete URL, and
  browser diagnostics, telemetry and support captures must redact or omit it.
  The current provider token lasts up to 15 minutes; clients honor the returned
  `expiresAt` rather than hard-coding that lifetime.
- After a user gesture and microphone permission, the browser may connect only
  to `wss://api.elevenlabs.io/v1/speech-to-text/realtime`. Production CSP adds
  the exact `wss://api.elevenlabs.io` source and Permissions Policy permits
  `microphone=(self)`; no broad `wss:`, `https:`, script or iframe relaxation is
  accepted.
- Before capture, the UI discloses that audio goes directly to ElevenLabs and
  that provider retention depends on the user's ElevenLabs account and terms.
  The microphone action is enabled only after that notice is visible; the
  user's subsequent mic action and browser permission are the explicit consent
  for that capture. Control does not claim zero retention.
  Stopping, backgrounding, navigation, logout and error close microphone tracks,
  audio processing and the WebSocket.
- The reviewed `@elevenlabs/client` version is pinned and receives the
  repository patch under `patches/` during `npm install`. The patch rejects
  inbound Scribe text messages larger than 65,536 JavaScript UTF-16 code units
  before JSON parsing and removes raw payload, close-reason and microphone-error
  console output. This is a conservative textual parse bound after the browser
  has received the frame; it is not a network-frame byte limit. Unknown events
  are ignored without logging their contents. Upgrading the SDK requires
  reviewing and regenerating this patch; silently accepting an unapplied patch
  is not an allowed build state.
- Partial text is presentation-only. Only committed transcript text may be
  inserted into the current unsent draft, without overwriting concurrent user
  edits, and it is never submitted automatically to an agent.
- Native operating-system keyboard dictation remains the fallback and does not
  require this integration.

## Consequences

The direct audio path minimizes latency and avoids turning Control into an audio
relay. The long-lived credential stays inside the existing vault boundary and
each user controls their own provider account.

After issuing a token, Control cannot inspect the audio stream, revoke that
already-issued token, guarantee provider retention, or enforce duration and
concurrency as strongly as a server-side proxy could. It limits issuance and
can close local browser resources, but provider availability, quotas, privacy
terms and compatible microphone support remain external dependencies.

ADR 0001 remains unchanged for Hermes, OpenClaw and every future agent
provider. This decision authorizes only the exact transcription host and
single-use-token flow described above; it does not establish a general browser
egress mechanism.
