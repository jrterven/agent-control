# ADR 0005: Control stores references, not Hermes internals

- Status: accepted
- Date: 2026-08-28

## Decision

SQLite stores users, workspaces, routing references, encrypted credentials,
audit and presentation state. Hermes remains authoritative for transcripts,
profiles, configuration, memory and cron execution sessions.

An owner may assign a `display_title` to a session link. This is a presentation
label only: synchronization continues refreshing the canonical Hermes `title`
without overwriting the local label, and clearing/losing the Control database
reveals the Hermes title again.

Cron synchronization is authoritative in both directions: Control upserts jobs
returned by Hermes and removes local references that no longer exist upstream.
It never recreates a missing job implicitly.

## Consequences

Control neither edits Hermes databases/files nor needs to synchronize full
message histories. Backup and deletion semantics stay explicit: archiving a
Control link is reversible and is distinct from deleting a Hermes session.
