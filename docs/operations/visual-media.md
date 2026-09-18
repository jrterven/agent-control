# Private images and galleries

Agents publish new images with the native Hermes `agent-control-media` plugin.
`publish_images` accepts local raster files or public HTTPS URLs, alt text,
caption and provenance (`web`, `generated`, `local`). It obtains the profile and
session from Hermes, and returns `![description](ac-media:32_hex_id)` plus a
`pending`, `ready` or `failed` status. Only `ready` confirms cloud publication.
Consecutive references render as galleries in their original Markdown position.
This capability does not add a generator or rewrite earlier responses.

## Storage and limits

Provision a private R2 product bucket, for example `agent-control-media`, separate
from the backup bucket. Keep public access, custom public domains and public
development URLs disabled. Create object read/write credentials scoped only to
this bucket and put them in the API's private environment file. Never put keys
in a frontend environment, image, connector configuration or agent prompt.

| API setting (prefix `HERMES_CONTROL_`) | Default |
| --- | --- |
| `VISUAL_MEDIA_ENDPOINT_URL` | Account's HTTPS R2 S3 endpoint |
| `VISUAL_MEDIA_BUCKET` | Required private product bucket |
| `VISUAL_MEDIA_ACCESS_KEY_ID`, `VISUAL_MEDIA_SECRET_ACCESS_KEY` | Required server credentials |
| `VISUAL_MEDIA_REGION` | `auto` |
| `VISUAL_MEDIA_MAX_BYTES` | 10 MiB; can be lowered |
| `VISUAL_MEDIA_MAX_PIXELS` | 25 million; can be lowered |
| `VISUAL_MEDIA_MAX_IMAGES_PER_GALLERY` | Six; can be lowered |
| `VISUAL_MEDIA_MAX_IMAGES_PER_RESPONSE` | 24; can be lowered |
| `VISUAL_MEDIA_QUOTA_BYTES` | 5 GiB per account, including thumbnails and retained deleted images |
| `VISUAL_MEDIA_RETENTION_DAYS` | 30 days after conversation deletion |

PNG, JPEG and WebP are decoded, orientation normalized, metadata removed and
thumbnails regenerated on the connector and server. Active formats, animation,
oversized files and decompression bombs are rejected. Export SVG charts to PNG
first. A tool call/gallery contains up to six images; a response up to 24.
The connector's private outbox reserves up to 256 MiB and survives restarts.

Image fetching uses no credentials/cookies, validates public DNS results and
connects to a validated address with hostname-verified TLS. Redirects repeat
validation; downloads have time and size limits. Browsers fetch only authorized
API routes, not source URLs or public bucket URLs. The existing restricted CSP
stays in effect. A source link is a separate user-initiated navigation.

## Rollout and runtime verification

1. Run the cloud runbook's tests, build, backup and isolated migration rehearsal.
   Deploy the compatible API/PWA first. Without R2 configuration the welcome
   handshake does not advertise `visualMediaV1` and connectors do not publish.
2. Verify a real authenticated image upload, its full/thumbnail reads and account,
   profile and conversation isolation before advertising the capability.
3. Build, sign, notarize and publish all connector platforms using the existing
   release runbooks. Include the updated connector in managed runtime releases.
   Keep the prior immutable releases for rollback.
4. Update each approved connector with its supported lifecycle command. Updates
   and Hermes restarts require fresh complete inventories with no active work.
   Installation uses the audited Hermes runtime revision, not version text alone.
5. When the cloud advertises support and the connector observes an idle profile,
   it installs the native plugin and merges its toolset into explicit lists for
   existing chats/cron jobs and future jobs. It preserves SOUL, prompts, schedules,
   personal settings and explicit disabling. Config/cron originals are backed up
   next to the plugin; unsupported runtimes are reported instead of patched.
6. To request installation explicitly, run:

   ```sh
   agent-control-connector install-media --data-dir /ABSOLUTE_CONNECTOR_HOME
   agent-control-connector media-status --data-dir /ABSOLUTE_CONNECTOR_HOME
   ```

   `install-media` uses a fresh idle drain but does not restart Hermes.
   `pendingActivation` requires a supported, idle Hermes restart. `ready` requires
   a matching loaded plugin SHA and a live runtime process. Check every shared
   profile; installation on disk alone is not readiness. Existing conversations
   receive the managed context on their next turn without rewriting prior text.
7. Run a scheduled visual briefing on an isolated test agent with no browser
   open. Wait for confirmed publication, stop only its idle connector, reopen
   the result and verify images, sources, thumbnails, viewer and downloads.
   Repeat responsive/browser acceptance in Chromium and WebKit.

## Backup, restoration and deletion

The database contains owner/route bindings, immutable content hashes, dimensions,
provenance and deletion tombstones. Objects remain private in R2. Conversation
deletion immediately withdraws access and tombstones its route so delayed retries
cannot resurrect it. Daily GC purges blobs after the recovery window. Do not set
a blanket 30-day lifecycle on the **product** bucket: live conversations retain
their images indefinitely. Backup retention is a separate policy.

`backup.sh` restores the PostgreSQL dump into a temporary database, exports every
ready image referenced by that exact snapshot and verifies a complete image
restore into an isolated local store. It publishes a matching
`control-....dump.media.tar` beside the dump. Empty image sets still have a
manifest; a missing image archive is not an empty set. Older pre-image database
schemas remain compatible with the original dump-only backup.

Configure the daily scheduler to invoke this release's `daily-backup.sh`:

```sh
bash deploy/cloud/daily-backup.sh /ABSOLUTE/compose.env /ABSOLUTE/backups \
  /ABSOLUTE/r2-backup.json /ABSOLUTE/backup-venv/bin/python
```

The private JSON uses the existing backup configuration fields `endpoint`,
`bucket`, `prefix`, `access_key_id`, `secret_access_key` and `region` (`auto`).
Use the separate backup bucket, with a verified 30-day lifecycle for the prefix.
The operator Python requires boto3. Use canonical absolute paths: the backup
directory and the directory containing `compose.env` must belong to the operator
and have mode `0700`; `compose.env` and the R2 credentials file must be regular
files with one hard link and mode `0600`. Symlinked paths are rejected. The wrapper
opens a validated deployment lock without truncating it and retains that lock,
uploads both artifacts, downloads them, restores the database and exercises image
restoration in isolation. It publishes a `.complete.json` receipt only after all
checks succeed, then runs image GC and local 30-day retention. A failure prevents
GC/retention and must alert the operator. Never schedule GC outside this lock.

Each receipt records an `expiresAt` UTC timestamp: the earliest effective expiry
of its database and image objects. Retrying an upload reuses immutable objects
and preserves their original expiry; a new `verifiedAt` does **not** restart the
30-day retention window. The completion receipt itself may remain in R2 longer
than its referenced objects, so its existence alone is not evidence of a usable
backup. Local `.verified.json` receipts are written through an atomic replacement.

For recovery, select a verified completion receipt whose `expiresAt` is present
and strictly later than the current UTC time. Reject missing or expired cutoffs
even if the receipt still exists. Download both objects and verify their recorded
hashes. Restore the database into a separate database as
described by the cloud runbook. Extract the flat image archive without links or
path traversal, then use the target release's CLI against that restored database:

```sh
python -m hermes_control_api.visual_media verify-backup /PRIVATE/images \
  --database control_restore_UNIQUE
python -m hermes_control_api.visual_media restore /PRIVATE/images \
  --database control_restore_UNIQUE
```

Use a lowercase unique suffix. `restore` writes only objects whose owner/route,
hash and size agree with that database, and refuses incomplete snapshots. Its
R2 destination comes from the private API environment. Verify authenticated image
reads with the recovered database before switching the production database.
Preserve the existing database, image bucket and prior immutable release until
all checks pass; never overwrite production during a drill.
