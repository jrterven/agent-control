# Public cloud beta

Agent Control supports a separate cloud deployment with capped Google registration.
The public beta uses open registration without invitations, with 20 total accounts.
Other cloud deployments default to invitation-only registration. The existing
private installation keeps its own users, database, secrets and Tailscale origin.
The cloud does not seed an environment gateway or initiate direct connections to
user-supplied URLs. See [ADR 0009](../adr/0009-public-cloud-personal-connectors.md).

## First deployment

Prerequisites: a confirmed Linux server with Docker Compose, util-linux (`flock`), a DNS hostname,
inbound 80/443 for Caddy, outbound HTTPS, persistent storage, off-host backups,
and a Google OAuth web application. Only Caddy publishes host ports; PostgreSQL
and the API stay on private container networks. Start with no more than 20 beta
accounts and measure capacity before increasing scope. One API worker is required.

1. Build and test the committed revision. The manually triggered **Cloud and
   connector release** GitHub Actions workflow runs the test/build suite,
   builds native Intel/ARM Linux/macOS packages and records the cloud image digest.
2. Configure Google with the exact redirect URI
   `https://YOUR_DOMAIN/api/v1/auth/google/callback`; request only identity scopes.
   No external Google mailbox or Drive access is requested.
3. Place `deploy/cloud/cloud.env.example` outside the checkout as
   `/etc/agent-control/cloud.env` (mode 0600). Fill in the public origin, matching
   allowed origin, Google client credentials, independent random 32-byte vault
   key and PostgreSQL URL. The PostgreSQL password must match the separate
   `/etc/agent-control/postgres-password` file, also mode 0600. Percent-encode
   URL password characters. Keep vault-key recovery separate from DB backups.
4. Place `compose.env.example` as `/etc/agent-control/compose.env`. Set the domain,
   file paths and the tested image **digest**, never `latest`. Prepare the
   downloads and private backup directories. Check existing services and disk
   capacity; do not reuse a port or overwrite another application's deployment.
5. From this release's `deploy/cloud` directory, run:

   ```bash
   docker compose --env-file /etc/agent-control/compose.env -f compose.yml up -d --wait
   python3 verify.py https://YOUR_DOMAIN
   ```

   The API entrypoint applies Alembic before serving. Cloud readiness describes
   database and API background supervision; sleeping users' computers do not
   make the service globally unready. Verification checks health/readiness and
   the actual PWA HTML, manifest, icons, JS/CSS and service worker over HTTPS.
6. Choose the registration policy in the API environment file:

   ```dotenv
   HERMES_CONTROL_CLOUD_REGISTRATION_MODE=open
   HERMES_CONTROL_BETA_MAX_USERS=20
   ```

   `open` allows new verified Google identities to register without an invitation.
   `invite_only` is the default when the setting is absent. In either mode the
   maximum is 20 registered Google identities, including existing and disabled
   accounts; pending invitations do not reserve places in open registration.
   Revoking a computer does not remove its owner's account or free a place.
   Enrollment is serialized in PostgreSQL, so concurrent sign-ins cannot exceed
   the cap. At capacity new users see a beta-full message; existing active
   identities can still sign in. New accounts never receive platform administration.
   Switching back to `invite_only` restricts new registrations without removing
   access from existing active users.

   After publishing a configuration change, verify the intended policy as well
   as health and public PWA assets:

   ```bash
   python3 deploy/cloud/verify.py https://YOUR_DOMAIN --registration-mode open --beta-max-users 20
   ```

   Confirm Google Auth Platform uses an External audience and review its
   publication/branding status before advertising public access. The app requests
   only `openid email profile`, not Gmail or Drive access. Google's
   [basic identity exception](https://support.google.com/cloud/answer/15549945?hl=en)
   means these sign-ins do not require adding each person to Google's test-user list.

   For an invitation-only deployment, create an invitation using the installed
   CLI, then let that person sign in:

   ```bash
   docker compose --env-file /etc/agent-control/compose.env -f compose.yml exec control \
     /opt/venv/bin/hermes-control-admin invite --email person@example.com
   ```

   Invitations expire after 14 days by default. The CLI and enrollment serialize
   their PostgreSQL transactions to enforce the beta cap. Explicitly grant an
   accepted operator identity platform administration with
   `hermes-control-admin grant-platform-admin --email person@example.com`.
   Invitations do not send email; distribute the public URL through your normal
   invitation process.

## Signed connector downloads

CI builds four unsigned native archives and the cloud image; it does not receive
the RSA release signing key or Apple's private key. Download all four `connector-*` artifacts from the
successful run and sign them on the operator's trusted Mac. Keep the PEM
RSA private key at `~/.config/agent-control-release/connector-signing.pem` with
mode 0600 and retain its offline backup outside the repository and DB backups.
No signing key is generated or committed by the build scripts.

```bash
gh run download RUN_ID --pattern 'connector-*' --dir /ABSOLUTE_RELEASE_DIR/native
python3 deploy/connector/prepare_release.py \
  --artifacts /ABSOLUTE_RELEASE_DIR/native \
  --output /ABSOLUTE_RELEASE_DIR/downloads \
  --revision COMMITTED_REVISION \
  --private-key ~/.config/agent-control-release/connector-signing.pem \
  --apple-identity DEVELOPER_ID_APPLICATION_CERTIFICATE_SHA1 \
  --apple-team-id APPLE_TEAM_ID \
  --notary-profile agent-control-notary
```

Use the exact Git revision from that successful workflow run. The local signing
command requires Python 3.12 or later, OpenSSL, Xcode tools, a Developer ID
Application identity with its private key in Keychain, and a validated
`notarytool store-credentials` Keychain profile. Obtain the certificate SHA-1
with `security find-identity -v -p codesigning`; never export the Apple private
key to CI. It inserts the public key into
the installer and each immutable native archive, verifies all four signed
archives and refuses an incomplete or overwritten release. The private key
stays on the signing computer.

For macOS, the publisher finalizes the regular-file PyInstaller layout, removes
redundant framework copies only after checking their contents and loader
dependencies, and signs every Mach-O with the same Developer ID team, hardened
runtime and timestamp. The main identifier remains
`com.jemailabs.agent-control.connector` across architectures and releases.
Do not change this identity or add entitlement exceptions casually: Keychain
uses the executable's designated requirement to recognize updates.

The exact final signed tree is submitted as ZIP to Apple. Both architectures
must be accepted, their ticket hashes must cover every delivered code file,
and `codesign --check-notarization` must pass before RSA checksums are published.
A pending submission exits 75 without changing `VERSION`. Repeat the **same
command and output directory** to resume; `.apple-signing` retains signed
bytes, upload IDs and logs. Never re-sign or resubmit a pending release. An
interrupted upload with uncertain outcome requires inspecting Apple's history.
Do not publish an unsigned fallback. Keep `.apple-signing` private and backed
up until publication is complete; only `downloads/connector` is public.
Bare CLI executables and ZIP files cannot be stapled, so initial Gatekeeper
verification can require access to Apple's servers; offline first launch is
not guaranteed. Run the frozen lifecycle smoke tests on the final signed
archives on both Mac architectures before uploading them.

For those native checks, create a temporary **draft** GitHub release targeted at
the exact commit, attach the two prepared macOS archives, and dispatch
`signed-connector-smoke.yml` with its `draft_release_tag`, `expected_revision`
and `expected_team_id`. This read-only workflow verifies all notarized code and
runs the frozen CLI/lifecycle tests on Apple Silicon and Intel. Require both
jobs to pass and compare the reports' archive SHA-256 values with the files
being uploaded. Delete the temporary draft after retaining the reports; do not
publish it. This workflow receives no Apple signing or notarization credentials.
GitHub requires push access to list draft releases, so an isolated download-only
job has a repository-scoped, short-lived `contents: write` token. It checks that
the release is still a draft, performs only reads, and passes the two archives
through Actions artifacts. Native test jobs have `contents: read` and receive
no write token; the download job never checks out or executes release code.

Copy only the resulting `downloads/connector` directory into the configured server
downloads directory, preserving `connector/releases/<revision>`.
Publish the immutable release directory first, then atomically replace the
installer and `VERSION` pointer. Never publish the installer template containing
`__CONNECTOR_RELEASE_PUBLIC_KEY__`. The configured reverse proxy serves these at
`/downloads/connector/`; it does not list directories.

The web's **Connect a computer** command downloads and verifies the correct
native package without requiring Node, Python installation or compilation.
The connector detects a clean, audited Hermes checkout and an authenticated
loopback dashboard on port 9119. An existing Hermes token is read from local
configuration or requested through a hidden terminal prompt; it never goes to
cloud. On Linux, detection includes the private installer's
`~/.hermes/control-services/hermes-serve.env` and the earlier private preview's
`~/.config/hermes-control-preview/hermes-serve.env`; macOS also checks its dashboard
Keychain item. The prompt uses the controlling terminal even when installing
with `curl | sh`, and aborts if input cannot be hidden. For unattended pairing,
add `--token-file /PRIVATE/PATH` to the installer command; this mode-0600 file
must contain only the existing dashboard token. An environment variable must
be exported for the `sh` process, not just for `curl`.
After fixing a failed pairing, rerun the same installer command. It reuses the
staged release only if all files still match the freshly verified download.
If a standalone connector is already paired, the installer offers to reconnect
it. Confirm in the terminal to generate a fresh code, then review and approve
the computer and profiles again in the web. With no controlling terminal, pass
`--reconnect` explicitly. The signed setup engine preserves the existing Hermes
home, source, loopback endpoints, local token, conversations and operation ledger;
it does not update the installed release or revoke an old cloud record. Cancelled
or expired authorization preserves the previous local pairing. Active or
uncertain work prevents the reconnection from stopping the service.
Nonstandard installations can use the runtime `connect --help` flags.
The connector does not install Hermes or silently trust a different revision.

After browser review, only selected profiles are shared. Explicit creation of a
new agent may add that new profile; an existing unshared profile is never
adopted by that operation. A revoked computer must be paired again with a new
identity. Revocation blocks future operations; it cannot undo an action Hermes
has already accepted.

Files live under `~/.agent-control-connector`; credentials use Keychain on macOS
and a protected local file on Linux. macOS LaunchAgents run only while the user
is logged in and awake. Linux uses a user systemd service with lingering; setup
must succeed before installation is reported as complete.

```bash
agent-control-connector doctor
agent-control-connector status
agent-control-connector update --release COMMITTED_REVISION
agent-control-connector rollback
agent-control-connector uninstall
```

Lifecycle commands refuse active or uncertain work. Updates verify signed
metadata, retain previous releases and restore the previous binary if the new
connection fails readiness. macOS checks the target executable's Keychain
access in the foreground **before** requesting maintenance or stopping the
working service, allowing five minutes for an OS permission prompt. Denial or
timeout leaves the existing service running. The first migration from an
ad-hoc signed release can require one approval; choose Always Allow only for
the expected connector. Subsequent releases preserve its Developer ID identity.
An already installed legacy executable still runs its old updater. For that
first migration, download and RSA-verify the new release, stage it in its final
`~/.agent-control-connector/releases/<revision>` directory, and run that exact
target's `check-credentials --data-dir ~/.agent-control-connector` before
activating the staged release with `agent-control-connector rollback --release
<revision>` (this command selects an already installed release). The automatic preflight applies once this version
has been installed; never stop the old service to wait for a permission prompt.
An explicit rollback to a legacy release without the preflight command warns
that permission may instead be requested during startup; automatic recovery
still restores the previous binary if startup fails. Installation and service
restoration report success only after observing a fresh connected status.
Uninstall removes the connector service while
preserving configuration and Hermes data; revoke its cloud entry separately.
Run `agent-control-connector install-service` to restore a previously removed
service with its existing pairing. To pair a revoked computer again, repeat the
guided installer and confirm **Reconnect this computer**. This downloads and
verifies the setup engine before changing local state, including when the
installed connector predates the guided reconnect option. The old cloud entry
stays revoked; the new entry receives a different credential and needs fresh
browser approval. Hermes-managed app installations use their own supervisor;
the standalone installer refuses to replace those identities or services.
Revoked computers remain marked in **My computers**, but their gateways, agents,
conversations and automations are omitted from the active shell and search.
Existing metadata is retained, and Hermes data is never deleted by revocation.
The new approved connection discovers its selected profiles and Hermes history
under its new identity. Computers are not merged by hostname or profile name;
Control-only organization attached to the previous identity is not reassigned.
Temporarily offline computers remain visible.
For an intentional local identity removal, `agent-control-connector uninstall --forget`
remains available before repeating the guided installer.
The explicit `--forget` removes the local device credential and pairing after
the runtime has stopped; it preserves Hermes data and operation deduplication.

## Updates, recovery and monitoring

Run `deploy/cloud/backup.sh /etc/agent-control/compose.env /ABSOLUTE_BACKUP_DIR`
daily and before every deployment. It creates a private PostgreSQL custom-format
dump, restores it to an isolated database and verifies the schema before
publishing the backup. Copy verified backups off-host; use a 30-day retention
policy and keep the vault key separately. Configure the scheduler on the
confirmed server, then verify an actual scheduled execution.

For an existing cloud installation, run:

```bash
bash deploy/cloud/release.sh /etc/agent-control/compose.env \
  ghcr.io/jrterven/agent-control@sha256:TESTED_DIGEST /ABSOLUTE_BACKUP_DIR
```

The release command takes an exclusive per-configuration lock, drains HTTP mutations, checks fresh agent inventories,
backs up/restores PostgreSQL, migrates an isolated restored database with the
new image and compares existing row counts. It checks fresh agent inventories
again immediately before changing the active image, because work may start on
the users' computers while the backup and migration rehearsal run.
It preserves the previous Compose file and validates the public PWA afterward.
An active or uncertain operation blocks cutover. A failed preflight resumes
the current API. A post-cutover failure is a failed deployment: inspect it and
use the retained image/backup according to schema compatibility; do not claim
completion or blindly downgrade the live database. Keep the old release checkout
available with its Caddy/Compose files as well as the previous image digest.

Retain the previous PWA's hashed assets while installed clients finish using
their current shell. The API's existing sibling-release fallback requires a
static path shaped as `releases/<revision>/apps/api/static`. For a container
deployment, extract and verify the new static directory from the exact CI image
digest, retain the preceding image's assets in a sibling release directory, and
mount that releases directory read-only. Set `HERMES_CONTROL_STATIC_DIR` to the
new release's static path in the prepared Compose file. Keep the selected image
pinned to its digest and confirm the mounted files are byte-identical to that
image. Verify a preceding hashed asset and a missing-asset 404 as well as the new
PWA; never replace an existing immutable directory or expose deployment secrets
through the asset mount. Preserve the site's existing proxy and download routes.

For rollback, pause incoming requests, stop Control, retain a fresh copy of the
failed database and confirm whether the previous binary supports its schema.
If not, restore the validated pre-release dump into a separate database, select
that database and the preceding image, then verify before resuming. Never run
`pg_restore --clean` against the running production database or issue an
unreviewed Alembic downgrade. Hermes runtimes stay separate.

An operator can inspect aggregate metrics with
`python -m hermes_control_api.cloud_operations status` inside the API container.
The CLI issues a temporary local session and revokes it afterwards. Metrics
contain route-template request counts, errors, cumulative durations, connector
availability and pending operation counts; no transcript, path identifiers,
query values or secrets. Use `drain` and `resume` for explicit maintenance.
Monitor public readiness externally and alert on sustained failure, backup
failure, disk pressure or increasing error rate; an individual sleeping laptop
is normal and does not page the platform operator.

The single cloud API worker admits at most 20 HTTP API requests at once and
queues at most 40 more for up to ten seconds. Additional requests receive a
retryable 503 before authentication or a mutation begins. Health/readiness use
two separate probe slots, and existing WebSocket connections do not consume
HTTP slots. PostgreSQL connections are bounded to 64 with no overflow and no
synchronous pool wait, leaving room for connector replies and background work
while HTTP requests await their results. Cloud event persistence runs outside
the connection loop with at most 16 database workers and preserves each
gateway's event order through completion or cancellation. Keep PostgreSQL's connection budget
above this bound with capacity reserved for backups and operator access. These
limits prevent local pool starvation; validate throughput and latency on the
actual dedicated server before claiming capacity for the pilot.

## Data handling and retention

The cloud processes conversations and files in transit; this beta does not
provide end-to-end encryption. Hermes keeps the main conversation history on
the user's computer and its credentials stay there. Some features also retain
data in Control, as listed below. Encryption of stored payloads uses the cloud
vault key and does not prevent the service from reading them when needed.

| Data | Location and current retention |
| --- | --- |
| Accounts, external identities, invitations, computers, gateways, profiles, session metadata and audit records | PostgreSQL. Retained until explicit deletion or operator cleanup; there is no automatic account-deletion interface or general age-based cleanup policy. Revoking a computer blocks access but does not delete its records. |
| Live voice transcripts (`LiveTranscript`) | Encrypted in PostgreSQL and retained until the associated conversation is deleted. These are persisted transcripts, not just data in transit. |
| Email reference cache | Encrypted in PostgreSQL with a fixed seven-day TTL and at most 512 entries per session. Access does not extend the TTL. Expired records are removed lazily during cache operations, so physical deletion can occur after expiry. |
| Conversation event replay buffers | Memory only, limited to 32 MiB across routes and 2 MiB per route, with an additional cap of at most 512 events per route. Entries are evicted under pressure and disappear on restart. These buffers are not a durable full conversation archive; persisted live voice transcripts remain subject to the separate rule above. |
| Google authorization flows | Valid for ten minutes, consumed once. A subsequent authorization start removes expired flow records. |
| Device pairing requests | Valid for ten minutes and cannot be reused. New device authorization requests prune records that expired more than one hour earlier; expiry blocks authorization even before physical cleanup. |
| Cloud idempotency records | Durable operation deduplication records in PostgreSQL. They have no automatic expiration policy; a client retry must not be treated as a new operation merely because time has passed. |
| Connector operation ledger | Stored locally to preserve operation identity across restarts. Retained receipts are bounded to 64 KiB each and 64 MiB in total, with a 128 MiB database file limit. These size limits do not establish an age-based expiration or permit automatic retry of uncertain operations. |
| Browser drafts and conversation snapshots | Stored locally in that browser until cleared by the application or user. Logging in on another device does not itself remove these local copies. |
| Database backups | Include persisted cloud records. The deployment policy is 30 days, but the operator must configure the backup scheduler, off-host copy and expiry on the actual server, then verify they run. Neither this document nor the backup command installs that schedule. Deleted live data can remain in retained backups until those backups expire. |

Before opening the beta, publish this retention policy to users and
provide an operator contact for deletion requests. An operator must separately
handle the cloud records, browser copies under the user's control and backup
retention; cloud cleanup does not delete Hermes history on the user's computer.

## Validation

`make test build` includes private-mode regressions, cloud authorization and
connector protocol/lifecycle tests. `npm run test:e2e -- cloud-onboarding.spec.ts`
exercises Google redirect, profile review, revocation and the empty state in
mobile/desktop Chromium, WebKit and Firefox configurations. Google is mocked in
automated tests; verify a real Google sign-in against the configured public
origin with the selected registration policy before advertising external access.
Open registration tests cover uninvited accounts, the last available place,
concurrent enrollment and existing-account login at capacity. Use only isolated test agents
for mutation checks; never send test prompts or reset personal agents.
