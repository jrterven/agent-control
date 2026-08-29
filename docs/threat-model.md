# Modelo de amenazas

## Activos y actores

Protected assets are Hermes/API credentials, the Control vault key, admin
session, transcripts, agent routing, remote mutation authority and audit data.
Relevant actors include a legitimate admin, an attacker with browser access, a
malicious page capable of CSRF, a compromised gateway URL, untrusted tool/model
output and an operator with host access.

## Threats and controls

| Threat | Impact | Required control |
|---|---|---|
| Browser learns Hermes key/URL | Direct agent control | backend-only boundary; no `VITE_*`, localStorage or read response secrets |
| CSRF/session theft | Unauthorized mutations | opaque HttpOnly Secure SameSite cookies, synchronizer token, Origin checks, rotation and expiry |
| Gateway SSRF/DNS rebinding | Cloud metadata or LAN access | scheme/port policy, DNS resolution before connect and redirect, block metadata/link-local/multicast/unspecified; private destinations require explicit private/tunnel mode |
| Cross-profile session confusion | Prompt reaches wrong agent | immutable route tuple, profile-scoped pool, stored/runtime separation and per-command assertion |
| Duplicate accepted prompt | Duplicate external side effects | idempotency ledger plus no blind Hermes mutation retry; durable reconciliation |
| Replay gap/restart | Missing or reordered UI state | `(epoch, seq)` dedupe, truncation detection, history rehydrate |
| Secret in logs/errors | Credential disclosure | structured allowlist logs, recursive redaction, safe upstream error mapping |
| Untrusted Markdown/tool output | XSS or data exfiltration | sanitize HTML/URLs, CSP, no arbitrary iframes, safe download proxy |
| WebSocket abuse | memory/CPU exhaustion | one-use tickets, origin/auth checks, frame/rate/subscription limits and bounded queues |
| Vault/database theft | gateway credential disclosure | AES-256-GCM, random nonce, AAD, external master key, filesystem mode 0600 |
| Malicious gateway response | parser/resource exhaustion | response size/depth limits, strict envelopes with forward-compatible unknown fields |
| Unsafe remote integration test | alters Newton/Jarvis | hard guard: mutating tests require exact `control-dev`; other profiles are read-only |
| Public service exposure | Internet reaches Control/Hermes | loopback binds, Tailscale Serve rather than Funnel, ACLs, no Docker port publication |

## Security invariants to test

- Built frontend and source maps contain no Hermes URL, key or token.
- Read APIs return credential presence/fingerprint only.
- A route mismatch fails before an upstream call.
- Redirects and resolved IP changes are revalidated by SSRF policy.
- Raw `reasoning.*`, secret prompts and host paths are absent from browser events.
- Logout invalidates tickets, clears offline cache and expires the server session.
- CSP and cookies are strict in production; the loopback development relaxation
  is explicit and cannot activate under the production environment flag.

## Residual risks

An administrator of the production host can read process memory and the external
vault key. Tailscale identity and ACL configuration are outside the application
trust boundary. Model/tool side effects remain governed by Hermes; Control adds
approval UX but cannot retroactively undo an approved command.
