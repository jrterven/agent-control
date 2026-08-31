# Update and rollback

Hermes Control and Hermes are independent release streams. Updating Control must
not update, reset, patch or check out the Hermes repository.

## Control release

1. Build and test an immutable revision on Mac/CI, including the mock fault
   suite and frontend secret scan.
2. Back up SQLite and verify the backup before running migrations.
3. Install into a new `/opt/hermes-control/releases/<revision>` directory and
   create its virtualenv/assets without changing the current symlink.
4. Run an offline configuration/preflight command against production settings.
5. Stop Control, switch `/opt/hermes-control/current`, start it and inspect
   migrations, liveness, readiness and realtime reconnection.
6. Smoke-test Newton/Jarvis with read-only probes and use `control-dev` for the
   automated mutation path; runtime permissions are validated from the public
   capability projection without mutating the user's normal profiles.

The production PWA uses a prompt-style service worker. It detects the new
immutable frontend automatically but activates it only after the signed-in user
chooses **Update now** (or **Update when finished**). Verify that the activity
button shows the update indicator, **Settings** reports the installed semantic
version, and activation is deferred while dictation, speech playback, streaming
or an unsent draft is active. After activation, confirm that the same Control conversation
is selected and that no API response entered the service-worker cache.

Keep the immediately preceding immutable release directory installed while
rolling out the new shell. When the configured static directory follows the
canonical `releases/<revision>/apps/api/static` layout, Control serves a missing
`/assets/<hash>` from a sibling release; other missing static files return 404
and are never replaced by cacheable `index.html`. This lets a still-open client
finish loading the bundle referenced by its older shell. Also test the
bundle-failure path: the persistent startup shell must remain visible and offer
**Repair and reload**; that repair may delete service-worker Cache Storage but
must not clear IndexedDB drafts or offline snapshots. Do not remove the previous
release until installed clients have had an opportunity to activate the new
service worker.

For rollback, stop Control and switch back only if the earlier binary supports
the migrated schema. Otherwise restore the pre-update database first. Never run
an Alembic downgrade against production without a migration-specific reviewed
procedure.

## Hermes compatibility change

An operator may update Hermes separately after its own backup/maintenance plan.
Before accepting a new revision for Control:

- record version and source SHA;
- compare relevant protocol sources against the compatibility matrix;
- run mock/adaptor contract tests;
- probe the real gateway read-only;
- run all mutations only under `control-dev`;
- keep unsupported capabilities disabled until verified.

Profile lifecycle support needs a separate destructive-contract review. In
particular, do not infer safe deletion from the presence of
`DELETE /api/profiles/{name}`. Hermes 0.20.5 keeps deleted profile homes in the
multiplex cron ticker snapshot and can recreate them on heartbeat; Control must
not advertise delete or accept that revision on either side of a transfer.
Only add a revision/pair after proving durable deletion beyond a scheduler
heartbeat, safe import rollback, session/history preservation and cleanup of
both temporary archives.

No Control startup, deploy or health check may execute `hermes update`.

## Transcription integration change

ElevenLabs Scribe is independent from the Hermes release stream. Before
updating its browser SDK or token contract:

- review the provider changelog and the single-use realtime Scribe contract;
- pin the reviewed package version in the lockfile and run dependency/license
  checks rather than loading provider code from a CDN;
- regenerate and review `patches/@elevenlabs+client+<version>.patch`, then prove
  a clean `npm install` applies it. The patch must preserve the textual bound of
  65,536 JavaScript UTF-16 code units before JSON parsing and remove raw payload,
  close-reason and microphone-error console output. Do not describe that
  post-receipt text bound as a network-frame byte limit;
- confirm that the backend still sends the owner API key only to the fixed
  HTTPS token origin with redirects disabled;
- exercise token non-persistence, log redaction, rate limiting and provider
  401/403/429/5xx/timeout handling with an injected fake transport;
- run the production frontend secret scan and verify that CSP adds only
  `wss://api.elevenlabs.io` while Permissions Policy remains
  `microphone=(self)`;
- smoke-test real-device start/stop, background, navigation and logout cleanup,
  prior destination/retention consent and the native-keyboard fallback, without
  promoting partial text or auto-submitting a transcript.

Changing the STT vendor must be a new reviewed integration/ADR. It must not be
modeled as a Hermes/OpenClaw capability or inherit an existing user's
ElevenLabs credential implicitly.
