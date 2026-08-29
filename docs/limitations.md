# Known limitations

- The first release supports one administrator. Database ownership fields exist
  for future multi-user authorization, but sharing is not implemented.
- Uvicorn runs one worker because realtime tickets and event subscriptions are
  process-local. Horizontal scaling needs a shared ticket/event backend.
- SQLite is appropriate for one always-on node. It is not a multi-writer cluster
  database and its volume must not be placed on an unsafe network filesystem.
- `9119` is required for full interactive behavior. `8642` is a fallback and is
  initially enabled only for `control-dev`.
- Capability availability is probed; version numbers alone do not enable UI.
  New or removed Hermes methods may temporarily appear unavailable.
- Real Hermes mutations require an operator allowlist, an operator-verified
  full SHA, and the exact audited provider capability. Newton (`default`),
  Jarvis and `control-dev` are in the default mutable allowlist so the UI can
  expose their complete verified contract, including cron and administration.
  Operators can still narrow `HERMES_CONTROL_MUTABLE_PROFILES` when deploying
  a shared or less-trusted installation.
- The audited Hermes memory endpoints are not profile-aware, so memory reads
  and writes are hidden for real 0.20.5/0.20.6 providers.
- An event replay buffer can truncate. Control rehydrates durable history, but
  transient tool progress between the gap may be unavailable.
- A disconnect after Hermes accepts a prompt can remain in a reconciling state.
  Control favors avoiding duplicate side effects over automatic resend.
- Approval and clarification responses are available only for audited
  0.20.5/0.20.6 dashboard contracts on an operator-allowlisted profile. A lost or ambiguous
  response is never retried; the user must wait for Hermes to replay the
  still-pending request after reconnection.
- Control never exposes raw reasoning/chain-of-thought. It may show only a safe
  status that reasoning occurred.
- Files and notes initially store references/metadata. Offline snapshots exclude
  attachments and require explicit opt-in.
- Cron executions remain separate Hermes sessions and are not ordinary chats.
- Hermes 0.20.5/0.20.6 cron schedules use the profile's configured timezone or,
  when it is empty, the Hermes host's local timezone. The official cron route
  does not support a different timezone per job.
- The container option relies on Linux host networking; native systemd is the
  recommended deployment for the first host.
- Tailscale ACLs, host compromise and Hermes tool behavior remain outside the
  application's full control.
