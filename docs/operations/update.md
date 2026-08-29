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
