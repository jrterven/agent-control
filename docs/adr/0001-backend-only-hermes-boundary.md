# ADR 0001: Backend-only Hermes boundary

- Status: accepted
- Date: 2026-08-28

## Decision

Only FastAPI may communicate with Hermes. The React application uses same-origin
Control REST and a ticket-authenticated Control WebSocket. Hermes URLs,
credentials and raw protocol frames are server-side data.

## Consequences

Control must proxy streams and normalize events, but browser compromise cannot
directly call Hermes or extract its long-lived keys. CORS is unnecessary on
Hermes, and production can keep both Hermes ports on loopback.
