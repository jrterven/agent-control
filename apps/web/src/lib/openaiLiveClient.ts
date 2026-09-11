// GPT-Live uses its own event protocol, separate from the Realtime API.
// https://developers.openai.com/api/docs/guides/voice-webrtc
export type LivePhase = "idle" | "connecting" | "listening" | "stopping" | "error";
export type LiveIssue = "permissionDenied" | "auth" | "quota" | "network" | "generic" | "unconfirmed" | "contextFull";
export type LiveFragment = { role: "user" | "assistant"; text: string; start: number; end: number; order: number };
type LiveOptions = {
  negotiate: (sdp: string, signal: AbortSignal) => Promise<{ session: { id: string }; transport: { sdp: string } }>;
  onPhase: (phase: LivePhase) => void;
  onIssue: (issue: LiveIssue) => void;
  onTranscript: (fragments: LiveFragment[]) => void;
  onPlaybackBlocked: (blocked: boolean) => void;
  onDelegation: (context: string, signal: AbortSignal, progress: (content: string) => void) => Promise<string>;
};

export function liveSupported() {
  return typeof window !== "undefined" && window.isSecureContext
    && typeof RTCPeerConnection === "function"
    && typeof navigator.mediaDevices?.getUserMedia === "function";
}

export function voiceContext(fragments: LiveFragment[]) {
  return [...fragments].sort((a, b) => a.start - b.start || a.order - b.order)
    .reduce<{ role: string; text: string }[]>((rows, part) => {
      const previous = rows.at(-1);
      if (previous?.role === part.role) previous.text += part.text;
      else rows.push({ role: part.role, text: part.text });
      return rows;
    }, []).map((row) => `${row.role === "user" ? "User" : "Voice assistant"}: ${row.text}`).join("\n");
}

// Each Live append accepts at most 500 tokens. UTF-8 bytes provide a
// conservative ceiling even for languages with many tokens per character.
export function boundedLiveCommentary(content: string) {
  const bytes = new TextEncoder().encode(content);
  if (bytes.length <= 420) return content;
  return "Partial backend result; the full answer is in the chat:\n"
    + new TextDecoder().decode(bytes.slice(0, 330)).replace(/\uFFFD$/, "")
    + "\n[End of excerpt]";
}

export class OpenAILiveClient {
  private peer?: RTCPeerConnection;
  private channel?: RTCDataChannel;
  private microphone?: MediaStream;
  private audio = new Audio();
  private abort = new AbortController();
  private ready = false;
  private closing = false;
  private disposed = false;
  private timers = new Set<ReturnType<typeof setTimeout>>();
  private fragments: LiveFragment[] = [];
  private seen = new Set<string>();
  private delegated = new Set<string>();
  private consumedInputs = 0;
  private queue: Promise<void> = Promise.resolve();

  constructor(private options: LiveOptions) {
    this.audio.autoplay = true;
    this.audio.setAttribute("playsinline", "");
  }

  private later(callback: () => void, delay: number) {
    const timer = setTimeout(() => { this.timers.delete(timer); callback(); }, delay);
    this.timers.add(timer);
    return timer;
  }

  private send(event: Record<string, unknown>) {
    if (this.disposed || this.channel?.readyState !== "open") return false;
    try { this.channel.send(JSON.stringify(event)); return true; }
    catch { return false; }
  }

  private fail(issue: LiveIssue) {
    if (this.disposed) return;
    this.dispose();
    this.options.onIssue(issue);
    this.options.onPhase("error");
  }

  async play() {
    try { await this.audio.play(); this.options.onPlaybackBlocked(false); }
    catch { if (!this.disposed) this.options.onPlaybackBlocked(true); }
  }

  async start() {
    this.options.onPhase("connecting");
    this.later(() => { if (!this.ready) this.fail("network"); }, 60_000);
    try {
      // Acquire on the user gesture. A late permission grant is still cleaned up.
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      if (this.disposed) { stream.getTracks().forEach((track) => track.stop()); return; }
      this.microphone = stream;
      const peer = this.peer = new RTCPeerConnection();
      peer.addEventListener("track", (event) => {
        if (this.disposed) return;
        this.audio.srcObject = new MediaStream([event.track]);
        void this.play();
      });
      peer.addEventListener("connectionstatechange", () => {
        if (["failed", "disconnected"].includes(peer.connectionState)) this.fail("network");
      });
      stream.getAudioTracks().forEach((track) => {
        track.addEventListener("ended", () => { if (!this.closing) this.fail("network"); });
        peer.addTrack(track, stream);
      });
      const channel = this.channel = peer.createDataChannel("oai-events");
      channel.addEventListener("message", ({ data }) => this.receive(data));
      channel.addEventListener("close", () => this.fail(this.closing ? "unconfirmed" : "network"));
      channel.addEventListener("error", () => this.fail("network"));
      await peer.setLocalDescription(await peer.createOffer());
      if (this.disposed) return;
      if (peer.iceGatheringState !== "complete") await new Promise<void>((resolve, reject) => {
        const finish = () => { clearTimeout(timer); this.timers.delete(timer); peer.removeEventListener("icegatheringstatechange", check); this.abort.signal.removeEventListener("abort", cancelled); };
        const check = () => { if (peer.iceGatheringState === "complete") { finish(); resolve(); } };
        const cancelled = () => { finish(); reject(new Error("Cancelled")); };
        const timer = this.later(() => { finish(); reject(new Error("ICE timeout")); }, 10_000);
        peer.addEventListener("icegatheringstatechange", check);
        this.abort.signal.addEventListener("abort", cancelled, { once: true });
        check();
      });
      if (this.disposed) return;
      const sdp = peer.localDescription?.sdp;
      if (!sdp) throw new Error("Missing SDP");
      const result = await this.options.negotiate(sdp, this.abort.signal);
      if (this.disposed) return;
      if (!result.session?.id || !result.transport?.sdp) throw new Error("Invalid negotiation");
      await peer.setRemoteDescription({ type: "answer", sdp: result.transport.sdp });
      // HTTP creation starts Live. Never send session.start on the data channel.
    } catch (error) {
      if (this.disposed) return;
      const status = typeof error === "object" && error !== null && "status" in error ? error.status : undefined;
      const code = typeof error === "object" && error !== null && "code" in error ? error.code : undefined;
      this.fail(error instanceof DOMException && ["NotAllowedError", "SecurityError"].includes(error.name)
        ? "permissionDenied" : status === 401 || status === 403 || status === 409 || code === "OPENAI_LIVE_ACCESS_DENIED" || code === "OPENAI_SECRET_UNAVAILABLE" ? "auth"
          : status === 402 || status === 429 ? "quota" : "network");
    }
  }

  private receive(data: unknown) {
    if (this.disposed || typeof data !== "string") return;
    if (data.length > 65_536) { this.fail("contextFull"); return; }
    let event: Record<string, unknown>;
    try { event = JSON.parse(data); } catch { this.fail("generic"); return; }
    if (!event || typeof event !== "object") return;
    if (typeof event.event_id === "string") {
      if (this.seen.has(event.event_id)) return;
      if (this.seen.size >= 20_000) { this.fail("contextFull"); return; }
      this.seen.add(event.event_id);
    }
    if (event.type === "session.started") {
      this.ready = true;
      if (this.closing) this.send({ type: "session.close" });
      else this.options.onPhase("listening");
    } else if (event.type === "session.closed") {
      const expected = this.closing || event.reason === "close_requested";
      this.dispose();
      if (!expected) this.options.onIssue("network");
      this.options.onPhase(expected ? "idle" : "error");
    } else if (event.type === "error") {
      // Provider error payloads can contain private content; never log them.
      this.fail("generic");
    } else if (event.type === "session.input_transcript.delta" || event.type === "session.output_transcript.delta") {
      if (typeof event.delta !== "string" || typeof event.start_ms !== "number" || !Number.isFinite(event.start_ms) || typeof event.end_ms !== "number" || !Number.isFinite(event.end_ms)) return;
      if (this.fragments.reduce((sum, part) => sum + part.text.length, 0) + event.delta.length > 48_000) { this.fail("contextFull"); return; }
      this.fragments.push({ role: event.type === "session.input_transcript.delta" ? "user" : "assistant", text: event.delta, start: event.start_ms, end: event.end_ms, order: this.fragments.length });
      this.options.onTranscript([...this.fragments]);
    } else if (event.type === "session.delegation.created" && !this.closing) {
      const delegation = event.delegation as { id?: unknown; target?: unknown } | undefined;
      if (delegation?.target !== "client" || typeof delegation.id !== "string" || this.delegated.has(delegation.id)) return;
      const id = delegation.id;
      this.delegated.add(id);
      // Transcript deltas may trail the delegation. Keep receiving while waiting.
      const offset = typeof event.offset_ms === "number" && Number.isFinite(event.offset_ms) ? event.offset_ms : undefined;
      const settled = new Promise<LiveFragment[]>((resolve) => {
        const cancelled = () => resolve([]);
        this.later(() => {
          this.abort.signal.removeEventListener("abort", cancelled);
          // Capture independently of queued backend work. A later request
          // must never absorb speech belonging to subsequent delegations.
          resolve(this.fragments.filter((part) => offset === undefined || part.start <= offset));
        }, 750);
        this.abort.signal.addEventListener("abort", cancelled, { once: true });
      });
      this.queue = this.queue.then(async () => {
        const snapshot = await settled;
        if (this.disposed || this.closing) return;
        const inputCount = snapshot.filter((part) => part.role === "user").length;
        if (inputCount <= this.consumedInputs) {
          this.send({ type: "session.commentary.append", event_id: crypto.randomUUID(), delegation_id: id, content: "No new transcribed request is available. Ask the user to repeat or clarify; do not claim a new task was submitted." });
          return;
        }
        this.consumedInputs = inputCount;
        const result = await this.options.onDelegation(voiceContext(snapshot), this.abort.signal, (content) => {
          if (!this.disposed && !this.closing) this.send({ type: "session.commentary.append", event_id: crypto.randomUUID(), delegation_id: id, content: boundedLiveCommentary(content) });
        });
        if (this.disposed || this.closing) return;
        this.send({ type: "session.commentary.append", event_id: crypto.randomUUID(), delegation_id: id, content: boundedLiveCommentary(result) });
      }).catch(() => { if (!this.disposed && !this.closing) this.fail("generic"); });
    }
  }

  stop() {
    if (this.disposed || this.closing) return;
    this.closing = true;
    // End capture immediately; retain the transport to receive final usage.
    this.microphone?.getTracks().forEach((track) => { track.enabled = false; track.stop(); });
    this.microphone = undefined;
    this.audio.pause();
    this.options.onPhase("stopping");
    if (!this.ready) { this.dispose(); this.options.onPhase("idle"); return; }
    this.send({ type: "session.close" });
    this.later(() => this.fail("unconfirmed"), 15_000);
  }

  dispose() {
    if (this.disposed) return;
    if (!this.closing && this.ready) this.send({ type: "session.close" });
    this.disposed = true;
    this.abort.abort();
    this.timers.forEach(clearTimeout);
    this.timers.clear();
    this.microphone?.getTracks().forEach((track) => track.stop());
    this.channel?.close();
    this.peer?.close();
    this.audio.pause();
    this.audio.srcObject = null;
  }
}
