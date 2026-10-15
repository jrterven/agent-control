# Chat file downloads

Updated connectors instruct agents to deliver requested documents using
`[Download filename](ac-file:URL_ENCODED_FILENAME)` after writing a regular copy
to the conversation's staging directory. The instructions include the exact
directory, so the agent does not need to infer its profile or session. Original
user-requested output locations are preserved. A user can ask the agent to
provide a download for an older document; old absolute paths alone never grant
filesystem access.

Copies live under the native profile's `exports/<sha256(session)[:32]>/` directory.
The connector exposes only referenced, existing files from that exact directory.
Symlinks in any component, hard links, subdirectories, hidden names, special
files and files over 50 MiB are rejected. The read repeats these checks with
directory descriptors, so a link swap after history loading cannot redirect a
download. The browser supplies an opaque identity, never a filesystem path.

Control checks ownership, active gateway access and conversation history, then
returns the bytes as an attachment with the original UTF-8 filename, no-store
and nosniff headers. Document content is never served inline. The PWA downloads
on a user click and reports missing-file/offline errors in the chat without
navigating away. A download does not resume a session or send another prompt.

Files remain on the agent's computer; it must be connected for a download.
There is no cloud archive or automatic expiry of these staged copies. Deleting
the conversation withdraws access through Control but does not remove native
exports. Temporary chats do not receive persistent file staging instructions
and cannot use this endpoint. Private installations support files only for the
environment-managed gateway with a locally accessible configured Hermes home.

Release the web/API and native connectors together. The capability
`connector.fileDownloadsV1` identifies updated connectors; older connectors
continue existing chats without downloadable file projection. No database
migration is added. Follow the ordinary backup, migration rehearsal, immutable
release, native update, health/readiness and public PWA checks; do not restart an
active or uncertain native runtime to activate this feature.
