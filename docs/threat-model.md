# Modelo de amenazas

## Activos y actores

Protected assets are Hermes/API credentials, each user's ElevenLabs API key,
single-use transcription/TTS tokens, microphone and synthesized audio, the Control vault key,
admin session, transcripts, agent routing, remote mutation authority and audit
data. Relevant actors include a legitimate admin, an attacker with browser
access, a malicious page capable of CSRF, a compromised gateway URL, an
external transcription provider, untrusted tool/model output and an operator
with host access.

## Threats and controls

| Threat | Impact | Required control |
|---|---|---|
| Browser learns Hermes key/URL | Direct agent control | backend-only boundary; no `VITE_*`, localStorage or read response secrets |
| Browser learns ElevenLabs API key | Reusable credential theft and quota spend | owner-scoped write-only record, AES-GCM with bound AAD; browser receives presence plus single-use token only |
| Transcription token is cached, replayed or captured from the provider WSS query | Unauthorized audio session or token leakage | memory only, `Cache-Control: no-store`, no service-worker/browser persistence, no idempotency ledger, no complete provider-URL logging/telemetry and a fresh token per capture |
| Token mint abuse | Exhausted provider quota | authenticated owner, CSRF and Origin validation, per-owner rate limit, provider-side key scopes and quota |
| TTS synthesis abuse | Character quota exhaustion or unintended text disclosure | explicit live opt-in or per-message tap, authenticated CSRF-bound proxy, 20,000-character ceiling, owner rate limit, fixed provider origin and no automatic history replay |
| Microphone starts without informed action | Private speech leaves device unexpectedly | destination/retention notice visible before the enabled mic action, secure-context permission, explicit user gesture, visible active state and deterministic resource cleanup |
| Broad CSP exception | Arbitrary external exfiltration | exact `wss://api.elevenlabs.io` `connect-src`, bounded `media-src 'self' blob:`, `microphone=(self)`, no wildcard scheme or remote script/media relaxation |
| Malicious transcription event | Draft corruption, transcript disclosure or UI/resource exhaustion | reproducibly patched/pinned SDK, 65,536-UTF-16-unit textual limit before JSON parsing, no raw SDK console output, unknown-event rejection, partial/committed separation and no automatic agent submission |
| CSRF/session theft | Unauthorized mutations | opaque HttpOnly Secure SameSite cookies, synchronizer token, Origin checks, rotation and expiry |
| Gateway SSRF/DNS rebinding | Cloud metadata or LAN access | scheme/port policy, DNS resolution before connect and redirect, block metadata/link-local/multicast/unspecified; private destinations require explicit private/tunnel mode |
| Cross-profile session confusion | Prompt reaches wrong agent | stable ordinary route tuple, profile-scoped pool, stored/runtime separation, per-command assertion and an exclusive verified route rewrite during agent move |
| Partial or concurrent profile move | Lost history, duplicate cron execution or work sent to the old computer | shared profile lifecycle lock, active-work preflight, paused cron inventory, streamed native export/import, destination verification, atomic local cutover and fail-closed rollback |
| Deleted Hermes profile reappears | Supposedly removed agent resumes cron work | exact revision allowlist; Hermes 0.20.5 delete/transfer is disabled because its stale multiplex heartbeat can recreate the directory |
| Duplicate accepted prompt | Duplicate external side effects | idempotency ledger plus no blind Hermes mutation retry; durable reconciliation |
| Replay gap/restart | Missing or reordered UI state | `(epoch, seq)` dedupe, truncation detection, history rehydrate |
| Secret in logs/errors | Credential disclosure | structured allowlist logs, recursive redaction, safe upstream error mapping |
| Untrusted Markdown/tool output | XSS or data exfiltration | sanitize HTML/URLs, CSP, no arbitrary iframes, safe download proxy |
| Hostile or fabricated email reference | Phishing, cross-session disclosure or remote tracking | owner/session-bound HMAC id, encrypted AAD-bound cache with live-history fallback, strict provider/URL allowlist, plain-text preview, no remote images and no claim that agent metadata is verified |
| WebSocket abuse | memory/CPU exhaustion | one-use tickets, origin/auth checks, frame/rate/subscription limits and bounded queues |
| Vault/database theft | gateway credential disclosure | AES-256-GCM, random nonce, AAD, external master key, filesystem mode 0600 |
| Malicious gateway response | parser/resource exhaustion | response size/depth limits, strict envelopes with forward-compatible unknown fields |
| Unsafe remote integration test | alters Newton/Jarvis | hard guard: mutating tests require exact `control-dev`; other profiles are read-only |
| Public service exposure | Internet reaches Control/Hermes | loopback binds, Tailscale Serve rather than Funnel, ACLs, no Docker port publication |

## Security invariants to test

- Built frontend and source maps contain no Hermes URL, key or token.
- Host bootstrap helpers keep private deployment invariants intact: no
  `tailscale funnel`, no `tailscale serve reset`, no non-loopback Hermes
  listener, and no reusable dashboard token outside the reviewed env file or
  macOS login Keychain.
- Built frontend, source maps, browser persistence and logs contain no
  long-lived transcription key. Single-use transcription tokens never enter
  SQLite, the idempotency ledger, audit payloads or caches. Because the official
  protocol places the token in the provider WSS query, diagnostics and telemetry
  must never retain that complete URL.
- Read APIs return credential presence/fingerprint only.
- A route mismatch fails before an upstream call.
- Redirects and resolved IP changes are revalidated by SSRF policy.
- Raw `reasoning.*`, secret prompts and host paths are absent from browser events.
- Email reference transport instructions, account/mailbox identifiers, UIDs,
  RFC Message-IDs, source URLs and body text are absent from ordinary history
  projections. Preview/open endpoints resolve the opaque id only from that
  authenticated session's AAD-bound cache or, on a miss, its Hermes history;
  they return `no-store` and never render remote email HTML.
- Offline email previews are AES-GCM Vault envelopes bound with AAD to the
  owner, session and opaque reference. Their fixed seven-day TTL is purged at
  startup and hourly; an accessed expired entry is also deleted. Normal reads
  never extend an entry, although a later live-history refresh can validate the
  source again and create a new fixed-TTL envelope. Encrypted
  copies in backups remain subject to the backup retention policy.
- Logout invalidates tickets, clears offline cache and expires the server session.
- Stop, background, navigation, logout and error close every microphone track,
  audio processor and transcription WebSocket. Reconnect always mints a fresh
  token after another explicit user action.
- Live TTS receives only a single-use token; the reusable key remains encrypted
  in Control. Navigation, disabling live reading and logout stop the socket and
  media pipeline. Historical TTS bodies, audio and full provider URLs are not
  stored in SQLite, audit payloads or browser caches. CSP permits only
  same-origin and in-memory `blob:` media; it does not permit remote media
  origins.
- The composer accepts only committed transcript text and never sends it to an
  agent automatically. Native keyboard dictation remains available without the
  BYOK integration.
- A clean `npm install` applies the exact SDK patch. Inbound Scribe messages over
  65,536 JavaScript UTF-16 code units are rejected before JSON parsing; this
  bounds text parsing after receipt, not network-frame bytes. Malformed and
  unknown provider events, close reasons and microphone failures cannot print
  raw data to the console.
- CSP and cookies are strict in production; the loopback development relaxation
  is explicit and cannot activate under the production environment flag.

## Residual risks

An administrator of the production host can read process memory and the external
vault key. Tailscale identity and ACL configuration are outside the application
trust boundary. Model/tool side effects remain governed by Hermes; Control adds
approval UX but cannot retroactively undo an approved command.

For BYOK transcription, microphone audio goes directly from the device to
ElevenLabs and is therefore outside Control's event normalization, audit and
revocation boundary. Control cannot guarantee provider availability, quotas or
zero retention; those depend on the user's ElevenLabs account, settings and
terms. Issuance limits reduce abuse but cannot terminate a token already handed
to a compromised browser.
