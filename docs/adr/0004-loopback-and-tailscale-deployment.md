# ADR 0004: Same-host loopback deployment with Tailscale Serve

- Status: accepted
- Date: 2026-08-28

## Decision

The first production deployment runs Hermes Control and Hermes as separate
services on `gx10-58f9`. Hermes binds only to loopback. Tailscale Serve terminates
HTTPS and proxies only Hermes Control at `127.0.0.1:8000`.

## Consequences

No Hermes protocol port is visible on the tailnet or Internet. Control must use
one Uvicorn worker in the first release because realtime tickets and the event
hub are process-local. Horizontal scaling requires an external shared event and
ticket store.
