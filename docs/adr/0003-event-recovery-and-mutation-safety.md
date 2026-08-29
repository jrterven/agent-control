# ADR 0003: Event recovery and mutation safety

- Status: accepted
- Date: 2026-08-28

## Decision

Control tracks Hermes event sequence and epoch, replays after reconnect and
rehydrates history on gaps. REST mutations require idempotency keys, but an
accepted Hermes prompt is never automatically retried.

Hermes `0.20.5` and `0.20.6` do not echo the submitted `request_id` in prompt
events. Control therefore allows one unresolved prompt per session and binds a
fresh, sequenced terminal `message.*` event to that sole operation. Before
dispatch it records only the authoritative history length and a SHA-256 digest
of the prompt—not the prompt text. If the terminal event is lost, a later
history read can confirm completion only when the matching new user turn and a
following assistant turn both exist beyond that boundary.

Event sequence is scoped to the ephemeral Hermes runtime session id. Replacing
that id (or its Control connection generation) resets the durable sequence and
epoch cursor before the first resumed event is evaluated.

Manual cron triggers are also at-most-once. Control commits a local queued row,
marks it running before the long Hermes call, and never re-dispatches it after a
process restart. Any queued/running row without an authoritative Hermes run id
is closed as `unknown`; Hermes `/runs` is synchronized independently.

## Consequences

Temporary network loss can be healed without duplicate UI events. An ambiguous
prompt remains reconciling until durable history establishes its outcome; the
user receives an explicit state instead of a duplicated agent turn. Duplicate
or stale event sequences cannot complete a newer operation, and an ambiguous
history never causes a resend or a guessed success.
