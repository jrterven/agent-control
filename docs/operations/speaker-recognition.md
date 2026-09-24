# pyannoteAI speaker recognition pilot

Settings → Voice → People recognition enables a separate, default-off BYOK integration.
The key is encrypted in the existing vault with owner/provider AAD. It is write-only.
The connection button calls `GET /v1/test`: no audio, inference, balance lookup or
capability guarantee. Replacing/removing keys preserves existing voiceprints and
never regenerates them. Both enrollment and identification explicitly use `precision-3`.

People belong to the signed-in Control account and are available across its agents.
Enrollment requires a name and affirmative consent in settings, then a fresh guided
20-second recording of one speaker. An introduction such as “Soy Juan Ramón” only
offers a settings link; it cannot save a person. Renaming does not create a new print.
Re-enrollment replaces the encrypted print only after a successful, still-valid job.
Upstream labels are opaque person UUIDs, resolved to names only inside Control.

Live branches the existing WebRTC microphone using an AudioWorklet; Scribe uses
its official PCM callback. Neither opens a second microphone. The settings recorder
opens a microphone only from the explicit recording button. All paths collect mono
PCM16 at 16 kHz. Keyboard-native dictation has no audio callback and is unsupported.
Silence windows are discarded using an amplitude gate, not a speech/liveness model.
The gate can pass background noise; use the report to evaluate realistic conditions.

One owner-scoped database lease admits at most one job, with at least ten seconds
between submissions and no backlog. Windows default to five seconds; settings select
5/10/20 for evaluation. WAV input is bounded and validated. Each browser submission
owns a UUID plus audio hash receipt. A lost browser receipt is queried, not uploaded
again. Provider creation POSTs are never automatically repeated, including ambiguous
responses. Polling backs off, honors bounded Retry-After and stops at 120 seconds.

Pausing, stopping, leaving the page, switching conversation or logging out clears the
collector and invalidates the capture. Key/catalog/preferences changes also bump an
owner generation. Late jobs are discarded before publishing or replacing voiceprints.
A new capture deactivates older captures for that account, including in other tabs.
A process restart records interrupted jobs as unknown and deactivates all captures.
Release drain refuses running speaker jobs. Deployment remains single-worker as
required by the existing cloud runbook.

The UI reports probable, unknown, inconclusive, multiple speakers, processing or
unavailable. Matching uses threshold 70 and top-two margin 10; these are experimental
scores, not identity probabilities. Multi-speaker segments remain separate, including
overlap. Live receives quiet `session.thinking.append` evidence with a 30-second
freshness bound measured from the audio window, never permission to run a tool.
Dictation observations belong to a capture and are never inserted into an editable
draft as its author's identity. No authorization, sensitive-tool blocking, passkeys,
liveness, face recognition or Hermes modification is included.

## Storage and privacy

Control keeps no audio file. In-memory WAV bytes are released after transfer. The
provider's Media API may retain uploaded audio for 48 hours and job output for 24
hours. Disabling this feature stops new sends but cannot delete/cancel upstream work
already received. Voiceprints and observations are encrypted; no audio, keys, names,
voiceprints or raw provider failures enter HTTP idempotency receipts or application
logs. Signed upload URL query strings are redacted. Authenticated responses use
`Cache-Control: no-store` and the service worker does not cache API requests.

Temporary conversations do not persist their conversation identifier here. The
explicitly enabled pilot still keeps its own owner-scoped observation/usage history;
this is separate from temporary chat text. Catalog deletion removes that voiceprint
and clears name projections in historical observations. Report exports include
names and observations and should be handled as personal data.

## Evaluation during the trial

Automated tests mock pyannote and consume no credits. Real enrollment or identification
requires explicit recording/Live/dictation use after the owner enables this integration.
The connection check is safe before doing so. No trial samples or performance numbers
are fabricated by the implementation.

1. Enroll Juan and consenting participants in quiet conditions, one person at a time.
2. Set a 5-second window and use **Test recognition** with new speech, distinct from
   enrollment. Record correct/incorrect and, when known, the actual registered person.
3. Repeat with 10 and 20 seconds, unknown people, other microphones, background noise,
   changes of speaker and overlap. Keep a separate trial note identifying the condition
   for each timestamp; do not reuse enrollment recordings as test recordings.
4. Refresh/download the JSON report. It separates window lengths, reviewed/correct,
   false matches, unknown/inconclusive, p50/p95 latency, successful billed duration and
   voiceprints created. Server upload, provider+poll wait, polling HTTP, browser upload
   and end-to-end delivery are separate timing fields. The provider does not expose
   a precise processing-vs-queue split, so we do not invent one.
5. Report accuracy only among explicitly reviewed observations; unreviewed observations
   are not ground truth. The report covers the latest 10,000 jobs (and shows 50 recent
   receipts); total precision and security suitability require a representative dataset.

Every successful identification estimates at least 20 billable seconds. Voiceprints
are counted separately. Unknown/invalidated submissions with uncertain billing are
listed separately, never treated as free. The dashboard is authoritative for credits
and actual billing; the integration does not calculate a remaining account balance.

## API and release

- `/api/v1/integrations/pyannote`, `/key`, `/test`: preferences and credentials.
- `/api/v1/speaker-recognition/people/{uuid}`: consented catalog.
- `/captures/{uuid}`, `/captures/{uuid}/jobs/{uuid}`: capture lifecycle and WAV submission.
- `/jobs/{uuid}`, `/jobs/{uuid}/feedback`, `/jobs/{uuid}/delivery`: observations and evaluation.
- `/metrics`: owner-scoped pilot report, no audio or voiceprints.

All mutations require the existing authenticated session and CSRF token. Migration
`0031_speaker_recognition` adds four tables; it does not rewrite existing rows. Rehearse
it against a restored database, verify pre-existing row counts, retain the backup and
previous immutable release, then use the normal cloud release/drain/verification
workflow. A rollback to the previous binary can leave the additive tables in place;
do not downgrade production or remove them without separately reviewing data loss.

Sources: [models](https://docs.pyannote.ai/models),
[identification](https://docs.pyannote.ai/tutorials/identification-with-voiceprints),
[API test](https://docs.pyannote.ai/api-reference/test),
[billing](https://docs.pyannote.ai/administration/billing),
[retention](https://docs.pyannote.ai/data-retention).
