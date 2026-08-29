# Agent Control

Agent Control is a mobile-first React PWA and FastAPI control plane for existing
agent infrastructure. Hermes is the first supported provider and remains the
source of truth for its profiles, sessions, messages and cron; future providers
such as OpenClaw can be added behind the same backend boundary. The browser
talks only to Agent Control, so provider URLs, tokens and API keys remain in the
backend.

The current implementation includes:

- Local administrator authentication, CSRF protection, encrypted gateway credentials and a one-use realtime ticket flow.
- Profile-aware session routing that keeps gateway, profile, persistent session and runtime session identities separate.
- REST/JSON-RPC Hermes clients with heartbeat, replay, epoch handling and a deterministic fallback provider; the mock also exercises Hermes SSE surfaces.
- Workspaces, sessions, gateways, administration surfaces, cron references, diagnostics and audit metadata.
- A responsive PWA based on the selected dark, conversation-first design.
- A standalone mobile fidelity prototype and its passing visual QA evidence.
- A deterministic mock covering both `9119` dashboard/WS and `8642` API-server behavior.
- SSH tunnel supervision, Docker, systemd, Tailscale Serve and backup/restore documentation.

## Local development

Requirements: Node 20+, npm and Python `>=3.12,<3.15`.

```bash
cp .env.example .env
make bootstrap
make migrate
```

Generate a development vault key without committing it:

```bash
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("="))'
```

Set that output as `HERMES_CONTROL_VAULT_KEY_B64` in `.env`. Start either the deterministic mock:

```bash
make mock
```

or the Tailscale-backed SSH tunnel supervisor:

```bash
make tunnels
```

Then start the API and web app in separate terminals:

```bash
make api
make dev
```

The Vite app uses same-origin `/api` and `/realtime` paths through its development proxy. No Hermes secret is needed by the frontend.

Create the first administrator with the backend CLI after the migration:

```bash
.venv/bin/hermes-control-admin create-admin --username admin
```

`make api` also depends on the idempotent Alembic migration target, so every
development API start verifies the schema before serving requests.

## Verification

```bash
make test
make build
```

Remote integration is opt-in. Newton (`default`) and Jarvis (`jarvis`) are read-only targets; all mutations are rejected unless the exact selected profile is `control-dev`. See [remote test safety](docs/operations/remote-test-safety.md).

## Repository map

- `apps/web`: production React PWA.
- `apps/api`: FastAPI backend and security boundary.
- `apps/mock-hermes`: deterministic Hermes protocol simulator.
- `packages/hermes-client`: typed defensive Hermes clients and session routing.
- `packages/ui`: shared presentation primitives.
- `design/prototypes/mobile-option-2`: selected mobile prototype and visual QA.
- `docs`: architecture, API matrix, threat model, ADRs and operations.
- `deploy`: Docker and systemd deployment assets.

Production keeps the existing Hermes installation on loopback and exposes only
Agent Control through Tailscale Serve. Agent Control and Hermes run as
separate services; this repository never vendors, patches or replaces Hermes,
and never edits its internals, profiles, state databases or source code directly.
