# Production deployment

The canonical first deployment is native systemd on `gx10-58f9`. Commands below
are an operator runbook; repository automation must not execute them implicitly.
Hermes Control is added in front of the existing Hermes installation. It does
not install, upgrade, vendor or replace Hermes, and Hermes remains the source of
truth for profiles, conversations, sessions and cron.

## Network result

| Service | Bind | Exposure |
|---|---|---|
| Hermes Control | `127.0.0.1:8000` | Tailscale Serve only |
| Hermes headless protocol | `127.0.0.1:9119` | local Control only |
| Hermes `control-dev` API | `127.0.0.1:8642` | local Control only |

Do not add firewall openings, Docker port publishing, Tailscale Funnel, or
Tailscale Serve entries for `9119`/`8642`.

BYOK voice features add outbound traffic only. FastAPI needs HTTPS egress to the
fixed `https://api.elevenlabs.io` origin to mint single-use tokens, list voices
and proxy historical TTS audio. Voice-catalog samples additionally require
backend HTTPS egress to `storage.googleapis.com`; redirects and all paths
outside ElevenLabs' public preview bucket remain blocked. Each participating browser needs WSS egress to
the official speech-to-text and text-to-speech paths for microphone capture and
live answer playback. No new inbound listener or Serve route is permitted.

The official provider handshake carries the single-use token in the WSS query.
Do not enable browser/proxy telemetry that records complete WebSocket URLs, and
redact that URL from support captures. This narrow exception does not permit the
token in a Control URL, application log, audit event or persistent browser store.

## Prepare Control

1. Create a dedicated `hermes-control` system user with no login shell.
2. Install immutable releases under `/opt/hermes-control/releases/<revision>`
   and point `/opt/hermes-control/current` to the selected release.
3. Create `/var/lib/hermes-control` and `/var/backups/hermes-control`, both
   owned by `hermes-control` and mode 0700. The backup service is unprivileged
   and will not create its directory below root-owned `/var/backups`.
4. Build the React bundle on the Mac/CI and copy `apps/web/dist/` into
   `apps/api/static/` in the immutable release; the production host does not
   require Node. Create a Python 3.12 virtualenv and install the API plus Hermes client.
5. Copy `deploy/systemd/control.env.example` to
   `/etc/hermes-control/control.env`, replace every placeholder outside Git and
   set `root:hermes-control 0640`.
6. Run `alembic -c apps/api/alembic.ini upgrade head`, then invoke
   `hermes-control-admin create-admin --username admin` as `hermes-control`
   with the production environment file. The command reads the password twice
   from the terminal; never pass it as an argument or env var.
7. Install the reviewed `hermes-control.service`,
   `hermes-control-backup.service` and `hermes-control-backup.timer`, then run
   `systemctl daemon-reload`. Enable both `hermes-control.service` and
   `hermes-control-backup.timer`; verify the timer with
   `systemctl list-timers hermes-control-backup.timer` and run the oneshot once
   before relying on it.
8. Verify `/api/v1/health` (liveness) and `/api/v1/ready` (database readiness,
   automation-route watcher health and TTL-bounded cached upstream status) on
   loopback. `upstream=stale` means no recent Hermes observation; it must not be
   interpreted as proof that Hermes is online or offline.

Do not put an ElevenLabs API key in `control.env`, a `VITE_*` variable, the
release bundle or a service unit. Each authenticated user configures their own
key through the write-only integration setting; Control stores only its
AES-GCM ciphertext in SQLite. Restrict the provider key to the required Scribe
scope and an appropriate account quota before saving it.

The unit runs the same idempotent Alembic upgrade before every start. It invokes
Uvicorn with `--ws-max-size 4096`, one worker and `--no-proxy-headers`.
Tailscale Serve is the only ingress and Control deliberately ignores forwarded
client headers; the login limiter also keeps a global single-admin bucket so a
loopback process cannot evade Argon2 throttling by rotating `X-Forwarded-For`.
Keep one worker until realtime tickets/event fanout move to shared
infrastructure.

## Prepare Hermes protocol services

The files `hermes-serve.service` and `hermes-control-dev-gateway.service` are
templates matching the audited source-install layout. Use them only when the
existing installation does not already provide the required loopback endpoint;
do not overwrite a working Hermes unit. Before installation, confirm
`hermes serve --help`, the virtualenv interpreter path and profile list.

- Generate a unique dashboard session token and store it only in
  `/home/hermes/.hermes/control-services/hermes-serve.env` (0600).
- Generate a different strong API key for `control-dev`; set
  `API_SERVER_HOST=127.0.0.1`, `API_SERVER_PORT=8642` and leave CORS empty.
- The reviewed systemd template runs
  `hermes -p control-dev gateway run --replace --external-supervisor`; do not
  substitute the active profile or remove the explicit profile selector.
- Start each service independently and verify loopback listeners with `ss`.
- Never clone Newton/Jarvis into `control-dev` and never change their existing
  gateway services as part of Control deployment.

`hermes serve` is inherently headless. Its `/` route can legitimately return
404; validate its authenticated API/WebSocket instead.

The Hermes unit templates keep `/usr` and boot paths read-only but do not make
the user's workspaces read-only or disable Hermes' approval-mediated command
execution. Review workspace/tool permissions separately for each profile.

## Expose Control with Tailscale Serve

After Control succeeds on loopback, inspect current Serve state and add only the
Control reverse proxy:

```bash
sudo tailscale serve status --json
sudo tailscale serve --bg http://127.0.0.1:8000
sudo tailscale serve status --json
```

The command is persistent with `--bg` and terminates HTTPS for the tailnet. Apply
tailnet ACLs that restrict the service to intended users/devices. Do not use
`tailscale funnel`. If existing Serve configuration is non-empty, merge it
deliberately rather than running `tailscale serve reset`.

Set Control's allowed origin to the exact resulting `https://…ts.net` URL. Test
login, CSRF, WebSocket upgrade and logout from a second tailnet device.

Production response headers must keep the existing same-origin policy and add
only the voice exceptions: `microphone=(self)` in Permissions Policy, the exact
`wss://api.elevenlabs.io` source in CSP `connect-src`, and
`media-src 'self' blob:` for same-origin voice notes plus the in-memory audio
objects used by response playback. Do not add a wildcard `wss:`, broad `https:`,
remote media/script source or iframe permission. Test these headers on the built
FastAPI-served PWA, not only in Vite development.

## Container alternative

`deploy/docker/compose.yml` runs only Hermes Control using Linux host networking.
That deliberate choice lets the container reach host loopback without publishing
Hermes ports. The application itself still binds to `127.0.0.1:8000`; Compose
has no `ports` section and runs read-only with all capabilities dropped.
The env file defaults to `/etc/hermes-control/control.env`; override its path for
validation with `HERMES_CONTROL_ENV_FILE`, never with frontend variables. The
container entrypoint applies Alembic before startup, and its immutable default
command enforces the same 4096-byte WebSocket frame limit as systemd.

The image's default identity is numeric UID/GID `10001:10001`, while the bind
mount preserves host ownership and hides the directory created in the image.
Before the first container start, copy `deploy/docker/compose.env.example` to
`deploy/docker/.env`, set `HERMES_CONTROL_UID` and `HERMES_CONTROL_GID` to the
numeric owner of `/var/lib/hermes-control`, and make that directory mode 0700.
For example, after creating the dedicated host user:

```bash
install -d -o hermes-control -g hermes-control -m 0700 /var/lib/hermes-control
id -u hermes-control
id -g hermes-control
docker compose --env-file deploy/docker/.env -f deploy/docker/compose.yml config
docker compose --env-file deploy/docker/.env -f deploy/docker/compose.yml up -d
```

Put the two printed numeric values in `deploy/docker/.env`; do not guess them.
Compose explicitly runs with that identity. The entrypoint checks directory and
database access before Alembic and exits instead of starting against an
unwritable or accidentally different data directory.

Native systemd is preferred initially because it has fewer network-namespace
surprises. Never start the container with `-p 9119`, `-p 8642`, privileged mode,
or a broad mount of `/home/hermes/.hermes`.

## Add a macOS Hermes gateway

A second Hermes installation in the same tailnet can join Control without
exposing its protocol port to the browser or to other tailnet devices. Keep
Hermes on Mac loopback and carry it to a distinct loopback port on the Control
host with a reverse SSH tunnel over Tailscale:

```text
Mac 127.0.0.1:9119
  -> reverse SSH over Tailscale
Control host 127.0.0.1:29119
```

Use the two wrappers in `deploy/bin` with the launchd templates in
`deploy/launchd`. Store the dashboard token in the macOS login Keychain under
service `com.agent-control.hermes-dashboard`; never place it in a plist, shell
history, frontend variable or repository file. The launchd jobs run as the
signed-in macOS user and require no sudo. They keep `hermes serve` and the SSH
tunnel independently restartable and use SSH keepalives to recover after a
Tailscale or network interruption.

Register the gateway in Control with URLs that are local from the backend's
perspective, for example `http://127.0.0.1:29119` and
`ws://127.0.0.1:29119/api/ws`, connection mode `tunnel`, and no API fallback.
Leave the trusted SHA empty until that exact Hermes checkout has passed the
compatibility review. Profile display names are gateway-scoped: a Mac profile
whose canonical id remains `default` may therefore be displayed as Turing while
the production gateway's own `default` profile remains Newton.

## Post-deploy checks

- `9119`, `8642` and `8000` are loopback listeners only.
- Tailscale Serve lists only the Control target.
- Authentication cookies are Secure/HttpOnly/SameSite and CSP is present.
- Automated Newton/Jarvis smoke probes remain read-only; destructive integration
  test mutations still use `control-dev`, independently of runtime permissions.
- Logs and rendered frontend assets contain no token, key or Hermes URL.
- The ElevenLabs read view exposes only configuration presence. Its API key is
  absent from the built bundle, browser storage, network responses, logs and
  audit payloads; a single-use token response is `no-store` and absent from the
  idempotency table.
- The production artifact was built after a clean install that applied
  `patches/@elevenlabs+client+1.23.0.patch`; Scribe text messages over 65,536
  JavaScript UTF-16 code units, malformed messages and unknown events are handled
  without raw console output. This is a pre-JSON textual bound after frame
  receipt, not a network-byte limit.
- On a real mobile device, the ElevenLabs destination/retention notice is visible
  before the enabled mic action; dictation then starts only after the user's
  gesture and browser permission. The UI discloses direct processing/retention,
  previews provisional text directly inside the protected composer, inserts
  only committed text into the editable unsent draft and never sends it
  automatically. Typed and committed text grow the composer from one to six
  visible lines before internal scrolling begins.
- Stop, background, navigation and logout release the microphone and WSS. A
  later start mints a fresh token. With the integration disabled or unavailable,
  native operating-system keyboard dictation still works as the fallback.
- Selecting an ElevenLabs voice enables the live-response switch and the
  per-message speaker. Verify incremental TTS on the installed PWA, then verify
  historical play/pause, stop and speed controls. Neither the reusable key,
  response text nor synthesized MP3 may appear in audit or idempotency records.
- Backup timer is enabled and a restore drill has been completed.
