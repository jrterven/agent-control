# Semantic conversation search

Global search has independent Lexical and Semantic tabs. Lexical remains the
default and retains its existing filters. After explicit per-account activation,
both searches run while typing (250 ms lexical, 600 ms semantic). Semantic results
are grouped by conversation, with the best matching Text or Live excerpt.

The authenticated owner enables or pauses indexing through
`PUT /api/v1/search/semantic/settings` with `{ "enabled": true }` or `false`.
This uses the existing CSRF and idempotency controls. The existing encrypted
OpenAI integration key pays for both historical embeddings and query embeddings.
No platform key is used, and automated tests mock the embedding transport.

`GET /api/v1/search/semantic/status` reports indexed/pending/failed conversations,
an opaque publication revision, and a non-secret error code. Search requests use
`GET /api/v1/search?mode=semantic&q=...&limit=20`; omission of `mode` preserves
lexical behavior. Semantic mode returns conversations irrespective of lexical
kind filters. Model scores are internal, not confidence percentages.

## Data and lifecycle

Alembic `0030_semantic_search` adds preferences, durable indexing checkpoints,
and encrypted fragments without changing historical messages. The model is
`text-embedding-3-small`, 1536 dimensions, index version 1. Fragments contain up
to 700 tokens with 100-token overlap. Plaintext and float32 vectors are encrypted
with the existing vault and bound to the owner and conversation. Both SQLite
and PostgreSQL store the same schema; NumPy ranks authorized vectors in bounded
batches outside the API event loop. Query vectors have a bounded 10-minute
in-memory cache, partitioned by owner/model/dimensions; query text is not stored.

The supervisor discovers changes in persisted session metadata and Live
transcript revisions. It processes one bounded history page at a time, with
durable checkpoints, expiring leases and retry backoff. It also rereads existing
conversations after five minutes to discover upstream changes missed while
disconnected. Fragments unchanged within a conversation reuse their embeddings.
Only complete generations replace the published index; incomplete reads retain
previous results. Indexing pauses during cloud drain. On process termination,
an outstanding lease expires within five minutes before work resumes.

Only persistent, currently accessible conversations are eligible. Temporary
chats, system instructions, reasoning, tool messages, raw attachments and private
camera/email wrappers are excluded. Live uses saved text, never audio. Deleting
a session cascades to its fragments and checkpoint. Revocation withdraws access;
offline connectors retain previously indexed results with partial status after
an unsuccessful refresh. Pausing retains encrypted index data for reactivation.
Existing database backup retention also applies to derived search data.

Credential and quota failures pause the owner. Updating the OpenAI key or using
Retry resumes processing; transient errors use bounded exponential backoff.
Metrics include embedding input count, token usage, duration and error codes,
never prompts, API keys or search text. The cloud deployment disables access
logging and the existing edge proxy does not log query strings.

## Connector and release checks

New connectors advertise `connector.historyPageV1` and allow the read-only
`history_page(stored_session_id, offset, limit)` operation. Pages include messages,
next offset and an explicit completeness flag. No read creates/resumes a runtime;
the complete transcript is not limited to the previous 5000-message window.
Older connectors keep lexical search and report `SEMANTIC_CONNECTOR_UPDATE` for
indexing. Publish the signed connector release alongside the cloud image, using
the normal connector update workflow; never silently update a running agent.

The cloud image preloads the public tiktoken vocabulary at build time. Private
installations should warm `tiktoken.get_encoding('cl100k_base')` in their runtime
before enabling indexing and retain its cache. No OpenAI request is made by this
warmup.

Run backend/connector tests, frontend tests and production build. PostgreSQL
integration tests use `AGENT_CONTROL_TEST_POSTGRES_URL` and disposable schemas.
Follow the cloud/private release runbooks: verify idle state and a complete
backup, rehearse migrations against a restored copy, publish an immutable
release, and verify health/readiness plus current and retained PWA assets.
Rollback uses the previous compatible binary or the validated pre-release
backup. Do not downgrade the running database in place.
