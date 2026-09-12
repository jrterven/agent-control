import { OpenAILiveClient, type LiveInput, type LiveIssue, type LivePhase } from "./openaiLiveClient";

type VoicePreviewOptions = {
  negotiate: (sdp: string, signal: AbortSignal) => Promise<{ session: { id: string }; transport: { sdp: string } }>;
  onPhase: (phase: LivePhase) => void;
  onIssue: (issue: LiveIssue) => void;
  onPlaybackBlocked: (blocked: boolean) => void;
};

function audioContextConstructor() {
  if (typeof window === "undefined") return undefined;
  return window.AudioContext ?? (window as Window & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
}

export function voicePreviewSupported() {
  return typeof window !== "undefined" && window.isSecureContext
    && typeof RTCPeerConnection === "function"
    && typeof audioContextConstructor() === "function";
}

/** Live needs continuous input progress even for a greeting. Supply only zeroes. */
async function acquireSilentInput(signal: AbortSignal): Promise<LiveInput> {
  const Context = audioContextConstructor();
  if (!Context || signal.aborted) throw new Error("Voice preview unavailable");
  const context = new Context();
  let destination: MediaStreamAudioDestinationNode | undefined;
  let source: ConstantSourceNode | undefined;
  let released = false;
  const release = () => {
    if (released) return;
    released = true;
    signal.removeEventListener("abort", release);
    destination?.stream.getTracks().forEach((track) => track.stop());
    try { source?.stop(); } catch { /* The source may not have started yet. */ }
    try { source?.disconnect(); } catch { /* Cleanup must continue. */ }
    try { destination?.disconnect(); } catch { /* Cleanup must continue. */ }
    try { void context.close().catch(() => undefined); } catch { /* Already closed. */ }
  };
  signal.addEventListener("abort", release, { once: true });
  try {
    destination = context.createMediaStreamDestination();
    source = context.createConstantSource();
    source.offset.value = 0;
    source.connect(destination);
    source.start();
    // Called synchronously from the play gesture, before acquisition yields.
    await context.resume();
    if (signal.aborted || released) throw new Error("Voice preview cancelled");
    return { stream: destination.stream, release };
  } catch (error) {
    release();
    throw error;
  }
}

/** A real GPT-Live sample with no microphone, transcript handling or agent work. */
export class OpenAILiveVoicePreview {
  private client: OpenAILiveClient;
  private playbackTimer?: ReturnType<typeof setTimeout>;
  private disposed = false;

  constructor(options: VoicePreviewOptions) {
    this.client = new OpenAILiveClient({
      negotiate: options.negotiate,
      acquireInput: acquireSilentInput,
      disableDelegation: true,
      startupTimeoutMs: 30_000,
      initialCommentary: "Begin the voice sample now following the startup instructions.",
      onPhase: (phase) => {
        if (this.disposed) return;
        if (phase === "listening" && this.playbackTimer === undefined) {
          this.playbackTimer = setTimeout(() => this.stop(), 20_000);
        } else if (phase === "stopping" || phase === "idle" || phase === "error") {
          this.clearPlaybackTimer();
        }
        options.onPhase(phase);
      },
      onIssue: (issue) => { if (!this.disposed) options.onIssue(issue); },
      onPlaybackBlocked: (blocked) => { if (!this.disposed) options.onPlaybackBlocked(blocked); },
    });
  }

  private clearPlaybackTimer() {
    clearTimeout(this.playbackTimer);
    this.playbackTimer = undefined;
  }

  async start() {
    if (!this.disposed) await this.client.start();
  }

  stop() {
    if (this.disposed) return;
    this.clearPlaybackTimer();
    this.client.stop();
  }

  dispose() {
    if (this.disposed) return;
    this.disposed = true;
    this.clearPlaybackTimer();
    this.client.dispose();
  }

  async play() {
    if (!this.disposed) await this.client.play();
  }
}
