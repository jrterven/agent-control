# Visual media activation

The connector bundles the stdlib-only native Hermes plugin `agent-control-media`.
It requires the audited Hermes `939e45c91d751fadd94dcd1b873ac3cb44846213`
contract. An older runtime reports `unsupportedRuntime`; it must be upgraded
through the existing managed lifecycle before enabling images.

Deploy the cloud with `welcome.capabilities.visualMediaV1` enabled, then update
connectors. A connected connector installs the plugin only after a complete idle
scan. Newly created profiles are installed before their creation receipt returns.
This process does not restart Hermes. For an explicit, supervised installation:

```sh
agent-control-connector install-media --data-dir /path/to/connector-state
agent-control-connector media-status --data-dir /path/to/connector-state
```

`install-media` requires a fresh maintenance acknowledgement from the running
connector and refuses active or uncertain work. It installs only approved shared
profiles and preserves SOUL, prompts, schedules, personal configuration and
explicit plugin/toolset disables. Configuration and cron backups are retained
inside each plugin directory. Cron changes use Hermes' `.jobs.lock`.

`media-status` is read-only. `pendingActivation` means the files are installed but
the running Hermes process has not loaded this source hash. If the next idle
runtime discovery does not activate it, use the existing managed Hermes restart
procedure with its fresh idle checks. Never restart active work to activate media.
Verify each enabled profile reports `ready` before announcing the capability.
`disabled` is an operator preference, not a rollout failure.

The plugin uses actual runtime session/turn identity and returns private
`ac-media:` references. Its outbox is
`HERMES_PROFILE_HOME/.agent-control/media/outbox.sqlite3`; no credentials are
stored there. Pending content is bounded to 256 MiB and 512 images per profile.
The tool accepts at most six images per call and 24 per turn. Failed/acknowledged
rows release their local blobs; bounded receipts survive restart.

The cloud can reduce these defaults through the authenticated welcome message's
`visualMediaLimits` (`maxBytes`, `maxPixels`, `maxImagesPerGallery`,
`maxImagesPerResponse`). The connector stores this bounded policy per profile in
`policy.json`; both plugin admission and image processing read it for each call.
Policies cannot exceed the safety caps of 10 MiB, 25 megapixels, six per gallery
and 24 per response. Normalized-image expansion is checked against the aggregate
outbox quota under its SQLite write lock.

Publications only flow after the cloud capability handshake. A lost ACK replays
the same ID, bytes and metadata. `storage_unavailable`, `draining`, and reserved
`session_pending` errors retry with bounded backoff; all other ACK failures are
terminal. The cloud continues serving acknowledged images independently of the
connector. Cloud object storage, quota, retention and recovery are covered by the
cloud release/backup runbooks.

`agent-control-connector check-media` performs a no-user-data smoke of the
PNG/JPEG/WebP native codecs and the bundled plugin source. Native and managed
release builds run it before producing an artifact.

Smoke test with a temporary session: publish a local chart, reopen its history,
disconnect the connector, and verify the image still loads. Also run one scheduled
briefing with a job-specific toolset list and no browser open. Do not create or
modify user briefings as a deployment health check.
