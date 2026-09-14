# ADR 0008: Owner-scoped GPT-Live voice conversations

- Status: accepted
- Date: 2026-09-11

## Context

ElevenLabs dictation and response playback provide separate speech-to-text and
text-to-speech stages. Users also need a continuous spoken conversation that
can keep listening while speaking and delegate work to the selected agent.
GPT-Live supports this through full-duplex audio and client delegation. It is a
separate voice API, not a replacement Hermes gateway or a Realtime API alias.

## Decision

- Offer ElevenLabs dictation and GPT-Live conversation as independent configured
  actions in the composer, with microphone and waveform icons respectively.
  Hide each unconfigured action; without credentials the composer remains text
  only. The legacy exclusive-provider preference/API stays compatible with old
  PWA shells but no longer gates Live sessions or the current UI. A configured
  action starts only on its own user gesture. Existing ElevenLabs credentials
  never become OpenAI credentials.
- Keep one microphone workflow active at a time, including connecting, paused
  and finalizing states. Both buttons remain visible when configured; the other
  is disabled until capture ends. Preserve drafts and attachments before Live
  starts. Suspend ElevenLabs response playback during either microphone workflow
  without changing the saved auto-read preference.
- Let Scribe pause/resume its microphone using the pinned SDK's mute/unmute
  controls. Paused speech is replaced by silence; the connection stays open.
  Late confirmed text from before the pause may enter the draft, but late events
  cannot resume capture or promote provisional text. Stop retains the bounded
  commit handshake, and navigation, background and logout still release capture.
- Persist the owner's chosen built-in Live voice separately from the provider
  and credential. Default existing accounts to `marin`, and send the validated
  choice as `session.audio.output.voice` when starting each new conversation.
  Switching providers or replacing/deleting a key retains the voice preference.
  A running Live session keeps its original voice; settings explain that the
  change applies to the next conversation.
- Allow each owner to override the general Live voice for a stable Control
  profile ID. Resolve the override server-side for the authorized selected
  profile at session creation, falling back to the owner's current general
  voice. An explicit choice equal to the general voice remains pinned when
  the general voice changes; deleting the override restores inheritance.
  These built-in voice preferences survive provider/key changes and require
  no key or upstream request to save. Deleting the owner or profile cascades
  its overrides. Same-named profiles on different gateways remain distinct.
  The settings picker can target the general voice or any agent and retains
  explicit previews; changing the target ends a preview and isolates pending
  loads and saves from the newly selected agent. Previews use the requested
  voice without changing either the general voice or an agent override.
- Let users compare voices through explicit, brief GPT-Live preview sessions.
  Explain that previews use the owner's OpenAI quota before the play action.
  Feed generated silence instead of microphone audio, request one short phrase
  in the interface language, and never dispatch a preview delegation to Hermes.
  Bound playback and connection lifetime, and stop before playing another sample
  or leaving the screen. Previewing does not save the voice or change providers.
- Each owner supplies their own OpenAI project API key through an authenticated
  write-only setting. Store it encrypted with the Control vault and bind its
  ciphertext to the owner, provider and field. Read responses expose only
  presence and non-secret preferences. Never put the key in frontend assets,
  browser storage, logs, audit events or response payloads.
- Use the exact `gpt-live-1` model. FastAPI sends the browser's SDP offer and
  server-owned session configuration to the fixed
  `https://api.openai.com/v1/live/sessions` endpoint with redirects disabled.
  The browser receives the opaque session ID and SDP answer, never the API key.
  Session creation requires authentication, owner authorization, CSRF and rate
  limiting. Its response is `no-store` and bypasses the idempotency body ledger.
- Use WebRTC for browser media and a data channel for Live events. Capture starts
  only after a user gesture and browser microphone permission on HTTPS or
  localhost. Keep the OpenAI audio destination, account-dependent retention
  and usage notice in the voice settings where the user selects the provider.
  The chat uses separate icon buttons for configured services, each with an
  accessible provider/action label. Only active voice
  status, errors and blocked-playback recovery add controls to the composer.
  Set `store: false`; this does not promise
  zero provider retention.
- Keep input tracks disabled until `session.started`, a connected peer and an
  unmuted live microphone are all available. Show connecting separately from
  listening, with a short local readiness cue when browser playback permits.
  The active composer can pause/resume microphone tracks without reconnecting,
  recording or buffering paused speech. Playback, existing delegated work and
  duration billing continue while paused; this is not a provider-session pause
  or a way to retract audio already sent. Keep pause visible independently of
  agent work/approval status, retain the end-call control, and block PWA updates
  until the call ends. Navigation/background/logout still close paused calls.
- Use `delegation: {type: "client"}` so the existing selected Control agent
  handles work. GPT-Live handles the spoken exchange; Control retains agent
  routing, authorization, approvals and task state. The Live model does not
  receive gateway credentials or direct Hermes protocol access.
- Verify the selected route's `prompt.submit` capability before a billable
  handshake. Seed each session with that profile's display name, description,
  owned conversation title and recent chat history. The trusted Spanish voice
  prompt adopts that name and delegates questions about missing memory,
  personality, projects or capabilities to the same agent. The delegation
  wrapper preserves the backend agent's own identity and configured behavior.
- For general capability questions, use a broad, outcome-oriented introduction
  and a few varied, verified examples. The bounded catalog is a sample, not the
  agent's capability ceiling. Ask the selected agent for a current overview and
  confirmation of skill creation/update support before describing persistent
  learning. Explain learning as reusable, improved procedures rather than model
  retraining or automatic acquisition of account access. Keep this presentation
  guidance in both the voice prompt and backend delegation context; do not
  promise arbitrary task success or a saved skill before confirmed execution.
- For administrators, add a bounded snapshot of enabled skills and configured,
  enabled toolsets through the existing capability-gated read service. Exclude
  explicitly unavailable toolsets. Preserve admin-only inventory access; other
  users ask their agent through the normal chat. Optional inventory reads have
  a two-second total deadline and fall back to unknown capabilities, never a
  claim that the agent has no tools. The snapshot is partial and refreshed on
  each voice connection; execution still checks the agent's actual permissions.
- Place connection facts in a labelled user-role reference message, separate
  from trusted instructions and after history, so old generic voice claims do
  not define the current identity. Bound it to 2 KB and history to 5.5 KB, redact
  the OpenAI key, and exclude raw SOUL, memory, configuration and credentials.
  Names and descriptions are data, not instructions. Previews stay isolated
  from this agent context, as they are from conversation history.
- A `session.delegation.created` event carries a delegation ID and timestamp,
  not task text or structured tool arguments. Collect input and output
  transcript deltas separately, preserve their order and timing, and retain
  enough conversation context for corrections and short replies. Transcript
  fragments alone do not trigger agent work. Dispatch the delegated request
  through Control's normal authenticated conversation path.
- Render recognized voice delegation messages as speaker-labelled dialogue,
  omitting the internal instruction prefix and transport labels from the chat.
  Apply this presentation to both optimistic messages and historical prompts,
  including older instruction versions. Keep the original message content for
  delivery, reconciliation and the agent's context; preserve delivery warnings
  and do not rewrite stored history or ordinary user-authored messages.
- Display input/output transcript deltas in the chat, labelled by speaker,
  including overlapping speech and late fragments. Preserve the original text,
  timestamps and arrival order; display groups use a revisable 1.2-second gap,
  not a provider-confirmed turn boundary. Captions do not imply audio playback
  completion and never trigger agent work. Reader scrolling suspends following
  new text, with an explicit return-to-latest control.
- Preserve voice history in the owner/session-bound `live_transcripts` table
  (migration 0021), encrypted with the Control vault and a call-specific AAD.
  Store text only. Authenticated reads paginate ten calls at a time; CSRF writes
  accept bounded append-only batches with a client-generated call UUID and
  fragment offset.
  Older or duplicate batches cannot truncate or duplicate a transcript;
  conflicting replacements are rejected. Conversation deletion cascades history.
  Exclude transcript content from logs, audit and idempotency response storage.
- Batch only new fragments in the background once per second, with a bounded request
  timeout, retries and visible save failure/retry controls. Flush on stop and
  navigation without making WebRTC media or delegation await storage. Receive
  final deltas during graceful close. No additional transcription model or
  provider connection is created. Interrupted connectivity can leave an unsaved
  tail; do not claim guaranteed lossless transcription or zero delivery delay.
  Describe storage and audio handling in Settings → Privacy.
- Associate each result with its original opaque delegation ID. Send concise
  speakable results with `session.commentary.append`; keep each append within
  the provider limit. Long answers use a labelled excerpt and remain available
  in full in the chat. Tool output is factual context, never trusted application
  instructions. Acknowledgment of an append does not prove that speech was
  heard or that an external action completed.
- Keep microphone, audio playback and agent progress as distinct states.
  Interrupting speech or ending a voice session does not implicitly cancel an
  agent task. Explicit voice cleanup releases local media and transport on
  stop, background, navigation, logout and errors. A new start creates a fresh
  session; old callbacks cannot dispatch into a newly selected conversation.
- Keep the existing same-origin browser API and microphone permission policy.
  OpenAI session signaling is backend-only; negotiated WebRTC media does not
  justify broadening CSP to wildcard HTTPS/WSS, remote scripts or iframes.

## Consequences

Users can switch between the existing dictation/playback workflow and a live
voice conversation. The OpenAI account must have access to GPT-Live and incurs
voice duration charges independently of any agent backend usage. Network loss,
autoplay restrictions and device microphone behavior can interrupt audio; the
application does not promise lossless capture or guaranteed response latency.

Mock provider contracts and browser media in automated tests. Verify real-device
start/stop, overlapping speech, agent delegation, result playback and provider
errors with the owner's configured account before treating acoustic performance
as validated. Keep ElevenLabs and native keyboard dictation available when the
live integration is unavailable.

## References

- [GPT-Live overview](https://developers.openai.com/api/docs/guides/live)
- [GPT-Live prompting](https://developers.openai.com/api/docs/guides/live-prompting)
- [WebRTC setup](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live)
- [Client delegation](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=client)
- [Session lifecycle and transcripts](https://developers.openai.com/api/docs/guides/live-conversations)
- [WebRTC session schema and frontend permissions](https://developers.openai.com/api/reference/resources/live/methods/create)
