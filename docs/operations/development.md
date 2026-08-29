# Desarrollo local

## Prerrequisitos

- MacBook with Node 20 or newer, npm, Python 3.12–3.14 and OpenSSH.
- Existing SSH alias `agent`, reachable over Tailscale and pinned in
  `known_hosts`.
- Hermes credentials only in backend environment files. Do not place any
  credential or Hermes URL in a `VITE_*` variable.

## Mock-first workflow

Prepare the root virtualenv and frontend dependencies once:

```bash
cp .env.example .env
make bootstrap
make migrate
```

Alembic is mandatory in development. The API never creates tables implicitly;
`make api` depends on `make migrate`, so it is safe to use on both a fresh and
an already-current database. Create the first local administrator only after
the migration has completed:

```bash
.venv/bin/hermes-control-admin create-admin --username admin
```

When the remote computer is unavailable, run the deterministic mock in a
separate terminal:

```bash
make mock
```

It listens only on `127.0.0.1:19119` and `127.0.0.1:18642`. Set the backend to
the documented mock token/key; the browser still talks only to Control. Reset
state between tests with the authenticated `POST /__mock/reset` endpoint.
Keep `HERMES_CONTROL_PROVIDER_MODE=real` to exercise the real protocol adapter
against this external mock, or choose `mock` for the in-process provider. A
real session is never failed over silently to a mock identity.

Then start the backend and Vite separately:

```bash
make api
make dev
```

For a fresh database, `make api` applies every Alembic revision before opening
the loopback API listener. Re-running it does not reapply current revisions.

## Remote tunnel workflow

For the remote Hermes Control preview, keep this supervisor running in a
dedicated local terminal:

```bash
scripts/tunnels/control-preview-tunnel.sh run
```

It exposes only `http://127.0.0.1:18000` on the Mac and reconnects after an
SSH or Tailscale interruption. Verify it independently with:

```bash
scripts/tunnels/control-preview-tunnel.sh check
```

The remote `8000` listener remains bound to loopback; closing this local
supervisor makes the browser show `ERR_CONNECTION_REFUSED` without stopping
the remote service.

For local backend development against the Hermes protocols, start the two
protocol forwards below.

Start the foreground supervisor in a dedicated terminal:

```bash
scripts/tunnels/hermes-tunnels.sh run
```

It creates exactly these forwards and reconnects after SSH/Tailscale failure:

- `127.0.0.1:19119` → `agent:127.0.0.1:9119`
- `127.0.0.1:18642` → `agent:127.0.0.1:8642`

Check listener reachability with:

```bash
scripts/tunnels/hermes-tunnels.sh check
```

The check is transport-only: an unavailable `8642` is expected until the
isolated `control-dev` API server is enabled. Control must show that capability
as degraded while preserving the `9119` chat route.

Suggested backend-only local settings:

```dotenv
HERMES_CONTROL_HERMES_DASHBOARD_URL=http://127.0.0.1:19119
HERMES_CONTROL_HERMES_DASHBOARD_WS=ws://127.0.0.1:19119/api/ws
HERMES_CONTROL_HERMES_DASHBOARD_TOKEN=<remote-session-token>
HERMES_CONTROL_HERMES_API_URL=http://127.0.0.1:18642
HERMES_CONTROL_HERMES_API_KEY=<control-dev-api-key>
HERMES_CONTROL_HERMES_SOURCE_SHA=<independently-verified-40-hex-commit>
HERMES_CONTROL_MUTABLE_PROFILES=default,jarvis,control-dev
```

Voice-note playback requires Agent Control to have read-only filesystem access
to the gateway's Hermes profile directory. A backend running on the Mac through
the two HTTP/WebSocket tunnels cannot resolve remote `MEDIA:` files; use the
mock locally or run Control on the Hermes host and set
`HERMES_CONTROL_HERMES_MEDIA_ROOT=/home/hermes/.hermes/profiles`. The public API
never returns that path and only streams audio referenced by an owned session.

Store these in the backend's ignored local env file with mode 0600. Vite must
receive only its ordinary application configuration; it proxies `/api` and the
Control realtime socket to FastAPI. If the exact source SHA is absent or does
not match the probed version, Control deliberately keeps Hermes writes hidden.

## Creating the isolated profile

This is a one-time operator action, not performed by tests or application
startup:

```bash
ssh agent
hermes profile create control-dev --description 'Hermes Control development only'
```

Do **not** add `--clone`: `control-dev` must not inherit Newton/Jarvis `.env`,
memory, history or credentials. Complete the official Hermes setup/auth flow
interactively for that profile. Before installing services, inspect the actual
CLI help and paths; templates under `deploy/systemd` are reviewed inputs, not
remote mutation scripts.

## Safety rules

- `default` is displayed as Newton and `jarvis` as Jarvis.
- Automated capability probes against Newton/Jarvis must remain read-only even
  though the interactive product runtime may expose their verified writes.
- Any create, prompt, cron, config write, secret write, interrupt, archive or
  delete integration test refuses to run unless the profile string is exactly
  `control-dev` and a separate mutation opt-in is set.
- Never run integration tests by relying on the currently active Hermes profile.
- Never edit the Hermes repository, `.env`, `config.yaml` or `state.db` directly
  from Hermes Control.

See [remote test safety](remote-test-safety.md) for the executable test contract.
The read-only and mutating suites have separate Make targets. The latter is
never part of `make test`; it additionally requires
`HERMES_REMOTE_MUTATION_TESTS=1`, the exact sentinel and
`HERMES_TEST_PROFILE=control-dev` before it constructs a remote client.
