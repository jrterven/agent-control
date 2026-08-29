# Mock Hermes

Deterministic, in-memory test double for the two Hermes surfaces used by
Hermes Control. It binds to loopback by default and deliberately has no model
or tool side effects.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e 'apps/mock-hermes[test]'
mock-hermes
```

Default endpoints:

- dashboard REST and JSON-RPC WebSocket: `http://127.0.0.1:19119`,
  `/api/ws?token=mock-dashboard-token`
- OpenAI-compatible fallback: `http://127.0.0.1:18642`, bearer key
  `mock-api-server-key-change-me`

Override those development-only credentials with
`MOCK_HERMES_DASHBOARD_TOKEN` and `MOCK_HERMES_API_KEY`. They are intentionally
mock values and must never be copied into a real Hermes installation.

The REST control plane under `/__mock` supports `reset`, `state`, and these
scenarios:

- `disconnect`: close the WebSocket after accepting a prompt;
- `epoch`: change replay epoch and reset sequence counters;
- `replay-truncated`: force replay to report a gap;
- `unknown-event`: insert a future event type;
- `missing-endpoint`: return 404 for one configured path.

Scenario endpoints require the same mock token/key as their owning server.
Prompts may also contain `[disconnect]`, `[unknown-event]`, `[tool]`, or
`[approval]` for a per-turn behavior.

To run it in Docker, use `docker compose -f deploy/docker/compose.mock.yml up`.
The container listens on all interfaces inside its isolated namespace, but
Compose publishes both **mock** ports on host loopback only.
