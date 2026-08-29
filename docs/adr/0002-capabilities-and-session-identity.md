# ADR 0002: Capability negotiation and dual session identity

- Status: accepted
- Date: 2026-08-28

## Decision

Compatibility is capability-based, with a matrix keyed by reported version and
revision. A session route contains immutable gateway/profile/stored identifiers
and a replaceable runtime identifier. One connection pool entry is maintained
per gateway/profile pair.

## Consequences

Version skew degrades individual modules instead of the whole product. Every
operation performs route validation. Resume is an atomic identity transition,
which prevents cross-profile messages and stale runtime reuse.
