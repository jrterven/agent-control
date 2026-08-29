# Matriz de compatibilidad Hermes

Verified on 2026-08-28 against the installed checkout and official upstream
sources. This is a protocol allowlist, not a promise that every future Hermes
build exposes the same surface.

| Target | Version / revision | Status |
|---|---|---|
| Remote installation | `0.20.5`, `791e2ae3257e211d14ca77e654dfe10ee1976a1c` | Primary production target; probe at startup |
| Official line | `0.20.6`, `9978706e9303dbf990d90e744b131361449d73b9` | Development compatibility target |

Primary references: [programmatic integration](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/developer-guide/programmatic-integration.md),
[WebSocket transport](https://github.com/NousResearch/hermes-agent/blob/main/tui_gateway/ws.py),
[session handlers](https://github.com/NousResearch/hermes-agent/blob/main/tui_gateway/methods_session.py),
[prompt handlers](https://github.com/NousResearch/hermes-agent/blob/main/tui_gateway/methods_prompt.py),
and [API server](https://github.com/NousResearch/hermes-agent/blob/main/gateway/platforms/api_server.py).

Legend: **required** blocks the corresponding Control feature; **optional** is
used only after a successful capability probe; **fallback** may replace a
preferred path but is never exposed to the browser.

## Dashboard/server surface (`9119`)

| Surface | 0.20.5 | 0.20.6 | Control policy |
|---|---:|---:|---|
| `WS /api/ws` JSON-RPC 2.0 | yes | yes | required for interactive sessions |
| `HERMES_DASHBOARD_SESSION_TOKEN` | yes | yes | required; header for REST, `?token=` for WS |
| `GET /api/profiles` | yes | yes | optional REST discovery; RPC probe can supplement |
| session list/search/messages/export routes | yes | yes | optional per route; never infer from version alone |
| cron jobs/runs/pause/resume/trigger | yes | yes | optional; hidden when probes fail |
| config/schema/env/models, skills/toolsets/MCP/memory | yes | yes | optional administration modules |

The audited `/api/memory` routes are process-global in both target revisions:
they accept a profile parameter but do not use it to select profile state.
Hermes Control therefore does not advertise `memory.*` through the real
provider for 0.20.5 or 0.20.6. The deterministic mock remains available for
frontend development of that future, profile-aware contract.

Headless `hermes serve` is preferred over `hermes dashboard`; it intentionally
does not serve Hermes' SPA. A 404 at `/` is not a liveness failure.

## JSON-RPC methods

| Group | Confirmed methods | Requirement |
|---|---|---|
| Lifecycle | `session.create`, `session.list`, `session.resume`, `session.active_list`, `session.status`, `session.history`, `session.usage`, `session.branch`, `session.compress`, `session.close` | create/list/resume/history/status required for MVP |
| Turns | `prompt.submit`, `prompt.background`, `session.steer`, `session.interrupt` | submit/interrupt required for MVP |
| Recovery | `session.events.since`, `session.events.stats` | replay preferred; history rehydrate is fallback |
| User gates | `approval.respond`, `clarify.respond`, `sudo.respond`, `secret.respond` | optional, surfaced only after matching request event |
| Discovery/admin | `profiles.list`, `profiles.create`, `commands.catalog`, `models.list`, `config.get`, `config.set`, `reload.mcp`, `reload.env` | profile creation is mutation-gated; other methods optional |
| Delegation | `delegation.status`, `subagent.interrupt`, spawn-tree operations | optional |

Important response contracts:

- `session.create` returns an eight-character runtime `session_id` and a durable
  `stored_session_id`; it does not persist a database row before the first turn.
- `session.resume` receives the durable identifier and may return a different
  runtime ID.
- `prompt.submit` returns `{"status":"streaming"}` before the terminal event.
  Control must not impose a short request timeout or retry this mutation blindly.
- The audited prompt handler ignores `request_id`, and official event frames do
  not carry it. Control correlates the sole active prompt by session plus fresh
  event sequence; durable-history recovery uses a pre-dispatch message-count
  boundary and one-way prompt digest.
- `profiles.create` is enabled only after a successful `profiles.list` probe on
  an exact audited version/SHA. Control creates a fresh profile without
  `clone_from`, `description`, or `soul`, and requests Hermes' shared
  authentication mode so OAuth refresh state is not forked. The operator's
  long setup brief is then submitted in a visible session to the new profile so
  Hermes can analyze it and decide what to persist; Control never copies the
  brief directly into `SOUL.md`. It discards upstream filesystem/credential
  diagnostics, serializes creation per gateway, and reconciles an ambiguous
  response by listing profiles rather than resending the mutation.
- `session.events.since` returns `events`, `latest_seq`, `truncated`, `count`
  and `epoch`. Upstream buffers 512 events per session for at most 64 sessions.
- `approval.request` supplies `request_id`, a redacted `command`, description,
  policy flags and legal `choices`. Control answers only through
  `approval.respond({session_id, request_id, choice})`, where `choice` is
  `once`, `session`, `always` or `deny`; the result is `{resolved: int}`.
- `clarify.request` is either one `{question, choices, multi_select?}` or a
  batch of `{qid, question, choices, multi_select}` records. Control answers
  through `clarify.respond({request_id, answer, question_id?})`; batches return
  `remaining` question IDs and `clarify.expire` closes a timed-out request.
  Choice questions retain Hermes' final free-text “Other” row; for a
  multi-select question its answer is sent as the selected strings plus the
  custom string in the same array.
- A `session.resume` snapshot may include `pending_approval` and
  `pending_clarify`. Control replays those through the same normalized event
  path after binding the new runtime generation. A response is rejected unless
  its request event is pending for the same owned Control session; this is
  mandatory because upstream clarification IDs are process-global.

## Event allowlist

Required MVP events are `gateway.ready`, `message.start`, `message.delta`,
`message.complete`, `tool.start`, `tool.progress`, `tool.complete`,
`approval.request`, `clarify.request`, status/session events and `error`.
Unknown event types are retained as safe opaque telemetry and ignored by the UI
unless a registered normalizer supports them. Raw reasoning events and secret,
sudo or host-path payloads are never forwarded verbatim.

Every replayable event uses this envelope:

```json
{
  "jsonrpc": "2.0",
  "method": "event",
  "params": {
    "type": "message.delta",
    "session_id": "runtime-id",
    "payload": {"text": "…"},
    "seq": 12
  }
}
```

`gateway.ready.payload` advertises heartbeat support and `replay_epoch` in the
current line. Clients should use 15-second connect/heartbeat checks, a 45-second
inbound deadline and a 120-second default RPC deadline, except that accepted
long-running turns finish by events.

## OpenAI-compatible API server (`8642`)

| Endpoint | Role | Control policy |
|---|---|---|
| `GET /health`, `/health/detailed` | liveness/capabilities | probe only |
| `GET /v1/capabilities`, `/v1/models` | discovery | optional |
| `POST /v1/chat/completions`, `/v1/responses` | stateless/stateful generation | fallback only |
| `/api/sessions` and messages/fork/chat routes | durable sessions | optional fallback |
| `/v1/runs`, run status/events/approval/stop | async run + SSE | optional fallback |
| `/p/<profile>/…` | multiplexed secondary profile | use only when advertised and independently authenticated |

The server binds `127.0.0.1:8642`, has browser CORS disabled and requires a
strong `API_SERVER_KEY`. For the initial deployment it runs only under
`control-dev`; `9119` remains the authoritative interactive route.

## Negotiation rules

1. Record the reported Hermes version for diagnostics. Set
   `HERMES_CONTROL_HERMES_SOURCE_SHA` to an independently verified exact
   40-hex commit for contract selection; upstream status fields are never a
   trust anchor. Leaving it empty keeps audited writes disabled. For a gateway
   added through Control, an administrator may enter or revoke the same
   backend-only value through its write-only trust field; Control stores it
   encrypted and exposes only whether one is configured.
2. Probe the smallest harmless operation for each readable capability.
3. Enable a write capability only when both version and full SHA match an audited matrix entry and its related harmless route probe succeeds; a same-version fork remains read-only. The operator anchor may supply a revision omitted by Hermes, but any explicit upstream SHA that differs or is malformed forces read-only mode.
4. Cache the result by `(gateway, profile, version, revision)` for at most the configured short TTL (60 seconds by default).
5. Hide unsupported UI; never synthesize a write route from a version number alone.
6. Reject payloads with an unexpected profile/session route before sending.
7. On incompatible schema, mark only that capability unavailable and preserve
   chat when its required subset still passes.
8. Strip every write capability and reject every upstream mutation unless the
   technical profile is in the backend-only mutable allowlist, its gateway has
   a valid operator trust anchor, and the exact capability passed the audited
   probe. The default allowlist contains `default`, `jarvis` and `control-dev`;
   an operator may narrow it explicitly. The `8642` fallback and destructive
   remote mutation test guard remain restricted to exactly `control-dev`
   regardless of this runtime allowlist.

## Integración de transcripción fuera de la matriz Hermes

ElevenLabs Scribe no es una capacidad de Hermes ni de OpenClaw y no participa
en la negociación por gateway, perfil, versión o SHA. Es una integración BYOK
propia del usuario autenticado. Su contrato Control es deliberadamente pequeño:

| Operación Control | Contrato | Seguridad y persistencia |
|---|---|---|
| `GET /api/v1/integrations/elevenlabs` | devuelve `{configured, provider: "elevenlabs", modelId: "scribe_v2_realtime"}` | autenticado y owner-scoped; nunca devuelve la API key |
| `PUT /api/v1/integrations/elevenlabs/key` | recibe `{apiKey}` y devuelve la vista neutral | autenticación, CSRF e `Idempotency-Key`; cifra antes de persistir |
| `POST /api/v1/integrations/elevenlabs/test` | devuelve `{ok: true, provider, modelId}` cuando la credencial es aceptada | autenticación, CSRF e `Idempotency-Key`; no devuelve credenciales ni tokens |
| `DELETE /api/v1/integrations/elevenlabs/key` | elimina la credencial del owner y devuelve `204` | autenticación, CSRF e `Idempotency-Key` |
| `POST /api/v1/realtime/transcription-token` | recibe opcionalmente `{sessionId?, languageCode?}` y devuelve `{token, expiresAt, modelId}` | autenticado, owner-scoped, CSRF y rate limit; ambos campos son hints validados no reenviados, persistidos ni auditados; sin `Idempotency-Key`, caché ni persistencia de respuesta |

Control usa la API key únicamente desde el backend para solicitar el token
oficial single-use. El token se consume desde memoria en una conexión directa a
`wss://api.elevenlabs.io/v1/speech-to-text/realtime`; el protocolo oficial lo
incluye como query de esa URL. Control no captura, persiste ni registra la URL
completa. El token no se convierte en ticket de Control, no entra en
`NormalizedEvent` y no se reintenta/reproduce. Un cierre o fallo exige otra
acción explícita del usuario y un token nuevo.

La transcripción confirmada puede insertarse en el borrador activo, pero nunca
se envía automáticamente ni crea una sesión. Cambiar de Hermes a OpenClaw no
cambia, comparte ni migra esta integración owner-scoped.
