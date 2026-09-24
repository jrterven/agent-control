import { ApiError, request } from "./api";
import { useAppStore } from "../store/appStore";

function speakerRequest<T>(path: string, init?: RequestInit) {
  return request<T>(path, { ...init, headers: { "X-CSRF-Token": useAppStore.getState().csrfToken ?? "", ...init?.headers } });
}

export const speakerRoot = "/speaker-recognition";
export const speakerConfig = "/integrations/pyannote";
export type SpeakerConfiguration = { enabled: boolean; configured: boolean; generation: string; windowSeconds: 5 | 10 | 20; connectionTested: boolean; recognitionTested: boolean; readyPeople: number; model: string };
export type VoicePerson = { id: string; name: string; ready: boolean; consent: boolean; model: string };
export type SpeakerDecision = { state: "recognized" | "unknown" | "inconclusive" | "multiple" | "enrolled"; personId: string | null; name: string | null; authorizes: false; segments: { start: number; end: number; speaker: string; state: string; personId: string | null; name: string | null; score: number | null; margin: number | null }[] };
export type SpeakerJob = { id: string; captureId: string; kind: string; status: string; result: SpeakerDecision | null; duration: number; timings: Record<string, number>; billedSeconds: number; errorCode: string | null; feedback: string | null; expectedPersonId: string | null; createdAt: string; authorizes: false };
export type SpeakerState = { phase: "idle" | "collecting" | "processing" | "result" | "unavailable"; seconds?: number; job?: SpeakerJob; errorCode?: string; observedAt?: number; captureId?: string };
export type SpeakerSummary = { jobs: number; succeeded: number; reviewed: number; correct: number; falseMatches: number; unknown: number; inconclusive: number; latencyMs: Record<string, { p50: number | null; p95: number | null }> };
export type SpeakerMetrics = { summary: SpeakerSummary; windows: Record<string, SpeakerSummary>; estimatedBillableSeconds: number; voiceprintsCreated: number; unresolvedCharges: number; recent: SpeakerJob[]; sampleLimit: number };
export const speakerChanged = "speaker-recognition-changed";
export function suggestsVoiceEnrollment(text: string) {
  return /^(?:hola[,!]?\s*)?(?:soy|me llamo|my name is)\s+[\p{L}][\p{L}\p{M} .'-]{1,80}[.!]?$/iu.test(text.trim());
}
export function notifySpeakerChanged() {
  window.dispatchEvent(new Event(speakerChanged));
  // The event contains no credentials or biometric data.
  try { const channel = new BroadcastChannel(speakerChanged); channel.postMessage("changed"); channel.close(); } catch { /* Optional cross-tab signaling. */ }
}
export function speakerMutation<T>(path: string, method: string, body?: unknown) {
  return speakerRequest<T>(path, { method, ...(body !== undefined ? { body: JSON.stringify(body) } : {}) });
}

export function pcmFromBase64(value: string) {
  const raw = atob(value);
  const bytes = Uint8Array.from(raw, (c) => c.charCodeAt(0));
  return new Int16Array(bytes.buffer, 0, Math.floor(bytes.length / 2));
}
export function wavBlob(pcm: Int16Array) {
  const buffer = new ArrayBuffer(44 + pcm.length * 2);
  const view = new DataView(buffer);
  const word = (offset: number, value: string) => [...value].forEach((c, i) => view.setUint8(offset + i, c.charCodeAt(0)));
  word(0, "RIFF"); view.setUint32(4, 36 + pcm.length * 2, true); word(8, "WAVE"); word(12, "fmt ");
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true); view.setUint32(28, 32000, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  word(36, "data"); view.setUint32(40, pcm.length * 2, true);
  pcm.forEach((sample, i) => view.setInt16(44 + i * 2, sample, true));
  return new Blob([buffer], { type: "audio/wav" });
}

/** Fixed memory, no queued windows. Every chunk is a copy from the existing mic. */
export class PcmWindow {
  private samples: Int16Array;
  private length = 0;
  constructor(readonly seconds: number, private onWindow: (samples: Int16Array, speech: boolean) => void) { this.samples = new Int16Array(seconds * 16000); }
  clear() { this.samples.fill(0); this.length = 0; }
  get elapsed() { return this.length / 16000; }
  push(chunk: Int16Array) {
    let offset = 0;
    while (offset < chunk.length) {
      const count = Math.min(chunk.length - offset, this.samples.length - this.length);
      this.samples.set(chunk.subarray(offset, offset + count), this.length);
      this.length += count; offset += count;
      if (this.length === this.samples.length) {
        const complete = this.samples;
        this.samples = new Int16Array(complete.length); this.length = 0;
        let energy = 0; for (const sample of complete) energy += sample * sample;
        this.onWindow(complete, Math.sqrt(energy / complete.length) > 180);
        complete.fill(0);
      }
    }
  }
}

/** The branch never owns or stops tracks. Only its Web Audio nodes are released. */
export async function teeMicrophone(stream: MediaStream, receive: (pcm: Int16Array) => void, signal: AbortSignal) {
  const context = new AudioContext();
  let source: MediaStreamAudioSourceNode | undefined;
  let worklet: AudioWorkletNode | undefined;
  let gain: GainNode | undefined;
  const close = () => {
    signal.removeEventListener("abort", close);
    if (worklet) { worklet.port.onmessage = null; worklet.port.close(); worklet.disconnect(); }
    source?.disconnect(); gain?.disconnect();
    void context.close().catch(() => {});
  };
  signal.addEventListener("abort", close, { once: true });
  try {
    await context.audioWorklet.addModule("/vendor/elevenlabs/scribeAudioProcessor.js");
    if (signal.aborted) { close(); return close; }
    source = context.createMediaStreamSource(stream);
    worklet = new AudioWorkletNode(context, "scribeAudioProcessor", { channelCount: 1, channelCountMode: "explicit" });
    worklet.port.postMessage({ type: "configure", inputSampleRate: context.sampleRate, outputSampleRate: 16000 });
    worklet.port.onmessage = ({ data }) => { if (!signal.aborted && data.audioData instanceof ArrayBuffer) receive(new Int16Array(data.audioData)); };
    gain = context.createGain(); gain.gain.value = 0;
    source.connect(worklet).connect(gain).connect(context.destination);
    await context.resume();
    return close;
  } catch (error) { close(); throw error; }
}

export class RecognitionCapture {
  readonly id = crypto.randomUUID();
  private active = true;
  private ready = false;
  private captureRequested = false;
  private busy = false;
  private lastSend = -Infinity;
  private collector?: PcmWindow;
  private abort = new AbortController();
  private timer?: ReturnType<typeof setTimeout>;
  private expiry?: ReturnType<typeof setTimeout>;
  private state: SpeakerState = { phase: "idle" };
  constructor(private options: { mode: "live" | "dictation" | "enroll" | "test"; sessionId?: string; personId?: string; onState: (state: SpeakerState) => void }) {}
  private emit(state: SpeakerState) { if (this.active) { this.state = { ...state, captureId: this.id }; this.options.onState(this.state); } }
  async begin() {
    try {
      const config = await request<SpeakerConfiguration>(speakerConfig, { signal: this.abort.signal });
      if (!this.active) return;
      if (!config.enabled || !config.configured || (!config.readyPeople && this.options.mode !== "enroll")) return;
      this.captureRequested = true;
      const created = await speakerMutation<{ generation: string }>(`${speakerRoot}/captures/${this.id}`, "PUT", { mode: this.options.mode, sessionId: this.options.sessionId, personId: this.options.personId });
      if (!this.active) { this.closeRemote(); return; }
      if (created.generation !== config.generation) { this.stop(); return; }
      this.collector = new PcmWindow(this.options.mode === "enroll" ? 20 : config.windowSeconds, (samples, speech) => {
        if (!this.active || this.busy || Date.now() - this.lastSend < 10000) return;
        if (!speech) {
          if (this.options.mode === "enroll" || this.options.mode === "test") this.emit({ phase: "unavailable", errorCode: "PYANNOTE_NO_SPEECH" });
          return;
        }
        // Blob owns a copy before the collector wipes its PCM buffer.
        void this.submit(wavBlob(samples));
      });
      this.ready = true; this.emit({ phase: "collecting", seconds: 0 });
    } catch (error) { if (this.active) this.emit({ phase: "unavailable", errorCode: error instanceof ApiError ? error.code : undefined }); }
  }
  pcm = (samples: Int16Array) => {
    if (!this.active || !this.ready || this.busy) return;
    this.collector?.push(samples);
    if (this.state.phase === "collecting") this.emit({ phase: "collecting", seconds: this.collector?.elapsed });
  };
  private async submit(audio: Blob) {
    this.busy = true; this.lastSend = Date.now(); clearTimeout(this.expiry);
    this.emit({ phase: "processing" });
    const id = crypto.randomUUID();
    const started = Date.now();
    let uploadMs = 0;
    const deadline = setTimeout(() => {
      if (!this.active) return;
      this.ready = false;
      this.emit({ phase: "unavailable", errorCode: "PYANNOTE_TIMEOUT" });
      this.abort.abort(); this.closeRemote();
    }, 140000);
    try {
      // One PUT only. A lost receipt is resolved by reading this UUID, never by re-uploading.
      let job: SpeakerJob;
      try {
        job = await speakerRequest<SpeakerJob>(`${speakerRoot}/captures/${this.id}/jobs/${id}`, { method: "PUT", headers: { "Content-Type": "audio/wav" }, body: audio, signal: this.abort.signal });
      } catch (error) {
        if (!this.active || this.abort.signal.aborted) return;
        if (error instanceof ApiError && error.status < 500) throw error;
        job = await request<SpeakerJob>(`${speakerRoot}/jobs/${id}`, { signal: this.abort.signal });
      }
      uploadMs = Date.now() - started;
      let delay = 700;
      while (this.active && ["pending", "uploading", "running"].includes(job.status) && Date.now() - started < 135000) {
        await new Promise<void>((resolve) => {
          const done = () => { clearTimeout(this.timer); this.abort.signal.removeEventListener("abort", done); resolve(); };
          this.timer = setTimeout(done, delay); this.abort.signal.addEventListener("abort", done, { once: true });
        });
        if (!this.active || this.abort.signal.aborted) return;
        job = await request<SpeakerJob>(`${speakerRoot}/jobs/${id}`, { signal: this.abort.signal });
        delay = Math.min(4000, delay * 1.5);
      }
      if (!this.active || this.abort.signal.aborted) return;
      if (job.status !== "succeeded" || !job.result) throw new ApiError(503, "Recognition unavailable", job.errorCode ?? "PYANNOTE_UNAVAILABLE");
      const remainingFreshMs = 30000 - (Date.now() - started);
      if (this.options.mode === "live" && remainingFreshMs <= 0) this.emit({ phase: "collecting", seconds: 0 });
      else this.emit({ phase: "result", job, observedAt: started });
      void speakerMutation(`${speakerRoot}/jobs/${id}/delivery`, "PUT", { uploadMs, deliveryMs: Date.now() - started }).catch(() => {});
      if (this.options.mode === "test" || this.options.mode === "enroll") this.ready = false;
      else this.expiry = setTimeout(() => this.emit({ phase: "collecting", seconds: 0 }), Math.max(0, remainingFreshMs));
    } catch (error) {
      if (!this.active || this.abort.signal.aborted) return;
      this.ready = false; // Explicit mic restart retries a failed provider; no background charge loop.
      this.emit({ phase: "unavailable", errorCode: error instanceof ApiError ? error.code : undefined });
    } finally { clearTimeout(deadline); this.busy = false; this.collector?.clear(); }
  }
  private closeRemote() { if (this.captureRequested) void speakerRequest(`${speakerRoot}/captures/${this.id}`, { method: "DELETE", keepalive: true }).catch(() => {}); }
  stop() {
    if (!this.active) return;
    this.active = false; this.ready = false; this.abort.abort(); clearTimeout(this.timer); clearTimeout(this.expiry);
    this.collector?.clear(); this.closeRemote();
    this.options.onState({ phase: "idle" });
  }
}
