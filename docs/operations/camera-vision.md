# Camera vision

Camera analysis is owned by Control. Hermes keeps its configured model and its
usual `vision_analyze` image tool. Camera preferences use the owner's encrypted
OpenAI connection and select `gpt-5.6-luna` (default), `gpt-5.6-terra` or
`gpt-5.6-sol`. Saving a preference does not test model access or consume inference
quota. Provider rejection is shown without switching models.

The eye directly turns the camera on in on-demand mode and requests device
permission. A small inline preview above the normal chat composer shows the
active camera, a device selector and stop/attachment controls. There is no
separate vision window, setup step or automatic description. Activation,
resuming and switching cameras do not analyze a frame. Ask a question in the
normal chat or GPT Live to capture the current view automatically.

The camera stream is separate from Live's audio stream. Explicit hangup releases
the camera; Live's internal task suspension and reconnection leave it running.
Hiding the app, navigation, account/profile changes, going offline and credential/
preference changes stop capture. Restart requires a gesture.

On-demand requests use the normal Hermes task/approval/reconciliation path.
With an active camera, a text-only classifier first decides whether new written
or spoken requests need a current frame. Live groups the current input transcript
until 750 ms without a new fragment and starts the check even if the voice model
does not delegate. Transcript routing and provider delegation share a turn so
late delegation cannot duplicate a capture or Hermes task. Ambiguous requests
ask for clarification. Visual follow-up questions can compare the current and previous
analyzed frame. The agent answers in the normal chat; on-demand observations
remain saved as evidence without appearing as a second answer in the timeline.

Live drops output audio and captions while the current camera request is checked.
It instructs the model to wait, then measures at least 300 ms of quiet remote
audio before sending a verified result or allowing a nonvisual answer. A provider
command acknowledgement alone does not release playback. If output cannot be
observed or drained, Live closes with an error; any submitted Hermes work remains
in the chat. Verified approval notices may speak while the task is pending.
Camera switch/off invalidates pending preparation. Existing submitted tasks keep
their normal approval and reconciliation lifecycle. Camera history is not added
to Live startup as current evidence; fresh evidence accompanies its exact request
outside the displayed voice transcript.

This client intervention starts when an input transcript fragment arrives; it
cannot retract speech already heard before that event. Validate actual media and
transcript ordering on physical devices in addition to the simulated suite.
The protocol follows OpenAI's [transcript-driven delegation](https://developers.openai.com/api/docs/guides/live-delegation#react-to-transcript-fragments) and [client playback controls](https://developers.openai.com/api/docs/guides/voice-server-controls#control-playback-when-needed).

The current chat UI does not offer continuous mode or an interval preference.
The saved interval and continuous API/runtime remain compatible with older
clients, and previously published continuous observations remain in history.

Continuous observation waits 2, 5 (default), or 10 seconds after each completed
analysis, with one request in flight and no image queue. It compares the current
and previous analyzed frame with a bounded task context and textual observations.
Only relevant changes are published, at most once per 15 seconds. Only the newest
unpublished change is retained, and a fresh frame must still confirm it before
publication. Continuous observations do not create Hermes tasks or change drafts.
They are passive context in the chat and in an already connected Live call.
A new real user request to Hermes receives at most three recent observations from
the latest activation, within five minutes, marked as untrusted visual evidence.

## Data and transport

- Camera JPEGs are capped at 1280 pixels on the longest side and 1 MiB per frame.
  Two frames fit within the analysis endpoint's 3 MiB JSON body limit. Validated
  bytes reach OpenAI unchanged. No camera image enters the database, browser
  storage, generic idempotency ledger, logs or audit payloads.
- **Attach capture** uses the exact in-memory File associated with the last
  analyzed frame. It enters the ordinary attachment flow only when the user
  sends the draft. If that frame was released, the UI offers a new capture.
- Observations and replay results are encrypted with owner/conversation-bound
  authenticated encryption. Request UUID receipts prevent uncertain automatic
  retries; encrypted replay text expires after 24 hours, cleared on subsequent
  camera requests, while UUID tombstones remain until conversation deletion.
- Responses API calls use a fixed server-side endpoint, `store: false`, low
  reasoning, strict structured output and at most 600 output tokens. This
  requests no stored Response object; it does not promise zero provider retention.
- Each owner has an atomic database inference lease across workers and tabs.
  The audited HTTPX request has a 40-second total timeout and no retries; a
  60-second lease permits recovery after a worker dies. Rate limits run from
  completion, independently for the text classifier and frame analysis.
- Vision requests require normal authentication, conversation ownership and
  CSRF. Revoked routes and inactive accounts are rejected. Errors omit private
  input values and validation paths. `Permissions-Policy` allows `camera=(self)`.

API endpoints: `GET/PUT /api/v1/vision/preferences`, and under
`/api/v1/sessions/{id}/vision/`, `POST intent`, `POST analyses`, and paginated
`GET observations`. These endpoints never initiate a Live connection. Migration
`0027_camera_vision` adds preferences, encrypted observation and receipt tables.
Follow the cloud release runbook for backup, migration rehearsal and rollback;
never downgrade the running database as a shortcut.

## Validation

Mocked backend tests cover provider payloads, validation, ownership, revocation,
concurrency, cooldowns, publication, ciphertext and request receipts. Frontend
tests cover delayed grants, track cleanup, scene reset, exact attachment bytes,
late inference, classifiers and Live task resumption. The camera Playwright
suite uses generated canvas video and mocked providers in Chromium and WebKit,
including mobile viewports; it never opens a real device camera or sends images
to OpenAI.

Before claiming physical-device validation, separately exercise iPhone Safari
and installed PWA, Android Chrome, and a desktop camera: grant/deny permission,
switch cameras, stop, background/restore, and explicitly ask a visual question
in the normal chat or Live. Check that the thumbnail leaves the composer usable.
A successful real inference verifies only the selected model/account at that
time. Do not infer physical-device compatibility or model access from mocks.
