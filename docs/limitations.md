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
- Native profile deletion and transfer are enabled only for exact revisions
  whose deletion remains durable while the multiplex cron scheduler is
  running. Hermes 0.20.5 (`791e2ae…`) is excluded because a stale heartbeat can
  recreate a deleted profile directory; it is not accepted as either side of
  a move because destination rollback also requires safe deletion. Native
  delete is currently audited for Hermes 0.20.6 revisions `9978706e…` and
  `4209d371…`; transfer is narrower and the only audited move pair is
  `4209d371… → 4209d371…`. Profile archives are limited to 100 MiB and exclude
  credentials; local files and tools are not made portable automatically.
- Hermes 0.20.6 can keep a same-name import hidden when that technical profile
  name was previously deleted on the destination because the native import
  does not clear its `.deleted-profiles` tombstone. Control fails verification,
  keeps the source agent, and does not publish a cutover. A hidden imported
  directory may still require native operator cleanup before retrying with a
  different destination or technical name.
- After a native delete, a just-finished runtime inside multiplexed `hermes
  serve` can briefly recreate a tombstoned shell containing only an empty
  `state.db` and its lock. Hermes does not list or serve that shell and the
  audited live check retained no sessions, messages, routing, configuration,
  SOUL or cron data. Agent Control deliberately does not bypass Hermes to
  remove provider files; an operator who requires physical forensic cleanup
  must stop the owning serve process, verify the exact shell is empty, and use
  the Hermes host's maintenance procedure.
- Agents created in Control start with empty memory and history and share the
  gateway-managed inference authentication pool. They do not automatically
  receive a Telegram bot/channel; messaging-channel setup remains a separate
  Hermes administration task.
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
- Chat attachments require the dashboard WebSocket attachment methods; they are
  unavailable through the `8642` API fallback. A message accepts at most five
  supported files, 8 MB each and 12 MB combined. Offline snapshots and drafts
  exclude attachment bytes.
- BYOK dictation currently uses ElevenLabs Scribe and needs public Internet
  access from both the Control backend (HTTPS token mint) and the browser
  (`wss://api.elevenlabs.io`). It may be unavailable while the tailnet-hosted
  agent interface and native keyboard dictation still work.
- Realtime microphone capture depends on secure-context browser support,
  permission and mobile lifecycle behavior. Native operating-system keyboard
  dictation is the supported fallback when capture is unavailable or the user
  has not configured ElevenLabs.
- Audio travels directly from the device to ElevenLabs. Control cannot audit
  the stream, revoke an already-issued single-use token, guarantee zero
  retention or enforce stream duration after issuance. It rate-limits token
  minting and closes local resources, while provider quotas and retention remain
  governed by the owner's ElevenLabs account and terms.
- The official Scribe handshake includes the single-use token in the provider
  WSS query. The app does not persist or log that URL, but browser extensions,
  device diagnostics or external telemetry capable of recording complete
  WebSocket URLs remain outside Control's enforcement boundary.
- Dictation only edits the unsent composer draft. It never submits a prompt,
  chooses an agent or creates a Hermes/OpenClaw session automatically.
- Spoken responses require a selected ElevenLabs voice, use the owner's choice
  of Flash v2.5 or Multilingual v2, and consume the owner's character quota.
  Eleven v3 is intentionally unavailable. Live playback depends on MediaSource-compatible MP3
  streaming and mobile autoplay policy; when incremental playback is not
  supported, the response remains readable and on-demand playback can be
  retried after an explicit tap.
- The voice selector reads at most the first 500 voices exposed by the account.
  Replacing a key intentionally clears the selected voice until it is verified
  against the replacement account. The selected model remains owner-scoped.
- Cron executions remain separate Hermes sessions and are not ordinary chats.
- Hermes 0.20.5/0.20.6 cron schedules use the profile's configured timezone or,
  when it is empty, the Hermes host's local timezone. The official cron route
  does not support a different timezone per job.
- The container option relies on Linux host networking; native systemd is the
  recommended deployment for the first host.
- Tailscale ACLs, host compromise and Hermes tool behavior remain outside the
  application's full control.
