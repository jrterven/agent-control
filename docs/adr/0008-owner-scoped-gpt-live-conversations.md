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

- Preserve ElevenLabs and add an explicit GPT-Live mode in voice settings. An
  existing ElevenLabs credential never becomes an OpenAI credential. Selecting
  a mode does not start microphone capture or spend provider quota.
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
  localhost. Display the OpenAI audio destination, account-dependent retention
  and usage notice before capture. Set `store: false`; this does not promise
  zero provider retention.
- Use `delegation: {type: "client"}` so the existing selected Control agent
  handles work. GPT-Live handles the spoken exchange; Control retains agent
  routing, authorization, approvals and task state. The Live model does not
  receive gateway credentials or direct Hermes protocol access.
- A `session.delegation.created` event carries a delegation ID and timestamp,
  not task text or structured tool arguments. Collect input and output
  transcript deltas separately, preserve their order and timing, and retain
  enough conversation context for corrections and short replies. Transcript
  fragments alone do not trigger agent work. Dispatch the delegated request
  through Control's normal authenticated conversation path.
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
- [WebRTC setup](https://developers.openai.com/api/docs/guides/voice-webrtc?api=live)
- [Client delegation](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=client)
- [Session lifecycle and transcripts](https://developers.openai.com/api/docs/guides/live-conversations)
- [WebRTC session schema and frontend permissions](https://developers.openai.com/api/reference/resources/live/methods/create)
