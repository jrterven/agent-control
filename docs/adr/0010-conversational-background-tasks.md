# Conversational background tasks

Control uses the audited Hermes 0.21.2 native async delegation implementation.
The managed `agent-control-background` plugin contributes a system context
section and a next-turn hook for interactive conversations. It enables the
native `delegation` toolset where permitted, preserves personal instructions
and explicit opt-outs, and leaves cron tool selection and execution unchanged.
The agent delegates independent lengthy work, acknowledges dispatch and ends
its foreground turn. The user can then send another request in the same chat.
Native completion delivery wakes the parent between turns and its response
appears in the same conversation, including when no browser is connected.

This is concurrency between independent delegated tasks, not simultaneous
foreground mutations of one Hermes transcript. One human prompt remains in
flight at a time. While the foreground agent answers, the composer retains an
editable draft and explains that it has not been sent. Control does not create
an automatic retry queue for actions such as sending email. Delegation does
not grant additional permissions. Native stopping/resetting a session can
cancel its children, and active workers do not survive exiting Hermes.

An explicit Send can race a native completion wakeup. On the audited revision,
Control passes native `queued=True` with that single request. Hermes checks it
under its history lock: idle dispatch is unchanged; an already-busy session
accepts that request behind the current turn instead of interrupting it. The
UI shows the confirmed queued receipt and preserves its optimistic user row
until authoritative history proves that exact queued prompt was consumed.
This does not change the user's global busy-input preference or resend work.

## Native ownership and presentation

The connector reads only lifecycle columns of `async_delegations` from the
approved profile's SQLite database in read-only mode. It binds every task to
an existing `parent_session_id`. Process-global delegation RPCs and global
subagent counters are not evidence of conversation ownership. Profile-wide
active and pending-delivery counts protect connector update/restart gates;
conversation reads expose only that conversation's counts and tasks.

The read operation and `background.tasks` events carry bounded metadata:
opaque ID, generic title, lifecycle, delivery state and timestamps. They never
carry goals, prompts, child transcripts, raw results, paths or PIDs. Snapshots
have an observation timestamp, completeness and availability. Incomplete or
offline evidence cannot invent completion. Events have no native message
sequence and cannot advance the foreground replay cursor. The API persists
at most 200 recent tasks per conversation; conversation ownership gates both
HTTP reads and realtime delivery. Deleting the conversation deletes the cache.

## Independent turn correlation

Hermes 939e does not echo a request ID or background origin in `message.*`
events. The adapter supplies `controlTurn.correlation=history` and, when a
fresh native start is available, an opaque ID derived from runtime generation,
replay epoch, runtime session and start sequence. Missing starts retain the
history requirement without inventing an ID. Replayed starts cannot replace a
newer turn, and progress is scoped to the current turn. Background completions
can produce two starts; the UI does not create empty duplicate bubbles.

A terminal event with this contract never resolves an arbitrary pending human
prompt. A bounded server reconciler reads authoritative history after terminal
events independently of browser presence. The saved prompt digest and history
boundary identify the human turn, and the next user/system row ends its scope.
No uncertain external action is resent. Explicit native
`display_kind=async_delegation_complete` rows become generic system markers;
their following assistant replies carry `controlTurnOrigin` with the native
delegation ID when present. Internal wakeup prompts are never rendered as user
messages. The assistant's public response is the result shown to the user.

There is no audited ID linking a live turn to a partial history row. On reopening
mid-turn, the UI shows history and a running indicator until a verified terminal
and history refresh, rather than guessing and duplicating partial output.

## Release and recovery

Publish the compatible API/UI first (migration `0026_background_tasks`), then
the signed connector and managed distributions. Install/probe plugins only in
shared profiles while idle; activation requires the correct source hash, a live
runtime receipt and the native delegation toolset. Restart only after a fresh
foreground inventory and native background/pending-delivery proof. Old
connectors remain usable during rollout and do not advertise this capability.
Do not announce activation while profiles are pending, disabled or unsupported.

Follow the cloud/managed runbooks, including paired database/image backup and
restore rehearsal, immutable release pinning, both release drain checks and
public PWA verification. The new metadata is included in ordinary database
backups. A restored snapshot is historical evidence, not an executor or a
request to restart tasks. Rollback must preserve the added columns or use the
verified pre-release backup with its corresponding image assets.
