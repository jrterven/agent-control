# Conversation modes

New Chat opens a preparation screen. The selected mode becomes immutable when
the first message, voice session or camera session creates the conversation.
Existing conversations default to `memory_read_write` (Alembic revision 0029).

| Mode | Shared memory | History | Header |
| --- | --- | --- | --- |
| `memory_read_write` | Reads and saves memories | Retained | Brain |
| `memory_read_only` | Reads; refuses new memories | Retained | Book |
| `temporary` | Reads; refuses new memories | Only while open | Hidden eye |

The icon exposes its meaning on focus, hover or tap. Closing a temporary chat,
opening another chat, leaving the chat screen, switching profile/workspace,
logging out or closing/reloading the tab ends it. Opening menus or backgrounding
the app leaves it open. Backgrounded/disconnected clients renew every 30 seconds;
API and native runtime leases independently expire after five minutes without a
successful renewal. Expired sessions cannot be resumed or recreated implicitly.

Temporary conversations use a tab-only access secret, an isolated RAM database,
RAM event replay and RAM image storage. They are omitted from bootstrap, sidebar,
search, notifications, offline snapshots, drafts, update-return context and
durable idempotency receipts. Content is never copied into the primary database.
Native transcripts and background delegation records also use bounded RAM
databases. Closing interrupts native work and releases these stores; browser
cleanup uses keepalive with the leases as a fallback.

Explicit file/image uploads use a session-owned OS temporary directory because
native tools require file paths. It is removed on close/lease expiration and
normal runtime exit. An abrupt OS/process crash can leave staged upload files
until OS temporary-file cleanup. Files or external actions the user explicitly
asks tools to create are separate from retained conversation history. Model and
external tool providers retain their own applicable data policies.

## Native activation

The connector installs `agent-control-chat-modes` only on a completely idle,
audited Hermes 0.21.2 source revision
`939e45c91d751fadd94dcd1b873ac3cb44846213`. The copied adapter validates hashes of
the native modules it wraps. Removing the plugin from the enabled list or
explicitly disabling it is respected. Installations preserve configuration
backups and other plugins. The existing supervised command
`agent-control-connector install-background` installs this adapter too.

Use the normal managed runtime restart procedure with fresh idle checks to load
the installed adapter. A cloud/web update alone cannot activate native policy.
Only a successful `control.chat_modes` attestation enables the two restricted
options. If media is enabled, its RAM-aware adapter must be loaded too. External
memory providers and isolated compute-host processes currently have no audited
policy adapter, so restricted options are disabled for those configurations.
Temporary file staging currently requires the local terminal backend.
Existing restricted conversations remain readable but cannot send another turn
without their attested policy. Modes never silently fall back to ordinary chat.

The adapter constrains each agent instance and delegated child, blocks the
native memory mutation path, disables memory review/commit and transcript/debug
logging for temporary sessions, and binds every RPC/worker to the same policy.
Ordinary conversations retain their existing memory behavior.

## Release verification

Run the regular tests/build plus the browser chat-mode flow. On an installed
audited Hermes runtime, run its Python interpreter with:

```sh
python scripts/verify_native_chat_modes.py --hermes-root /path/to/hermes
```

This smoke test uses an isolated home, does not call a model, and checks shared
recall, blocked memory mutation, volatile transcript/delegation databases,
uploaded-file cleanup and lease revocation. Publish updated signed connectors
with the cloud release. Active temporary chats count as active work for cloud
and connector restart preflights, even when their foreground turn is idle.
