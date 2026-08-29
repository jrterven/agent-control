// Load only ElevenLabs' official Scribe modules. The package's browser barrel
// also imports its unrelated LiveKit conversational stack, adding ~600 kB and
// console paths that are outside realtime transcription. These pinned ESM
// modules keep the official Scribe client and web microphone implementation
// while preserving a small, auditable lazy chunk.
import { setScribeMicrophoneSetup } from "@elevenlabs/scribe-registry";
import { webScribeMicrophoneSetup } from "@elevenlabs/scribe-web-microphone";

setScribeMicrophoneSetup(webScribeMicrophoneSetup);

export { CommitStrategy, RealtimeEvents, Scribe } from "@elevenlabs/scribe-core";
export type { RealtimeConnection } from "@elevenlabs/scribe-core";
