import { LivePlaybackGate } from "./livePlaybackGate";

// GPT-Live uses its own event protocol, separate from the Realtime API.
// https://developers.openai.com/api/docs/guides/voice-webrtc
export type LivePhase = "idle" | "connecting" | "listening" | "paused" | "stopping" | "waiting" | "error";
export type LiveIssue = "permissionDenied" | "auth" | "quota" | "network" | "generic" | "unconfirmed" | "contextFull";
export type LiveFragment = { role: "user" | "assistant"; text: string; start: number; end: number; order: number };
export type LiveInput = { stream: MediaStream; release: () => void };
type LiveOptions = {
  negotiate: (sdp: string, signal: AbortSignal) => Promise<{ session: { id: string }; transport: { sdp: string } }>;
  onPhase: (phase: LivePhase) => void;
  onIssue: (issue: LiveIssue) => void;
  onTranscript?: (fragments: LiveFragment[]) => void;
  onPlaybackBlocked: (blocked: boolean) => void;
  onDelegation?: (context: string, signal: AbortSignal, progress: (content: string) => void, requestText: string) => Promise<string>;
  onCameraRequest?: (context: string, signal: AbortSignal, progress: (content: string) => void, requestText: string) => Promise<string | null>;
  cameraSession?: string | null;
  acquireInput?: (signal: AbortSignal) => Promise<LiveInput>;
  initialCommentary?: string;
  disableDelegation?: boolean;
  startupTimeoutMs?: number;
  initiallyPaused?: boolean;
};

type CameraTurn = {
  session: string; inputs: LiveFragment[]; snapshot: LiveFragment[]; controller: AbortController;
  timer?: ReturnType<typeof setTimeout>; result?: Promise<string | null>;
  response?: Promise<void>; progress?: Promise<void>; delegationIds: Set<string>; answered: boolean; inputCount: number;
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
  private input?: LiveInput;
  private audio = new Audio();
  private playback = new LivePlaybackGate(this.audio);
  private cameraSession: string | null;
  private cameraTurns: CameraTurn[] = [];
  private cameraTurn?: CameraTurn;
  private withheldOutput = false;
  private outputEpoch = 0;
  private outputRelease?: { epoch: number; promise: Promise<boolean> };
  private abort = new AbortController();
  private ready = false;
  private connectedOnce = false;
  private paused = false;
  private inputPhase: LivePhase = "connecting";
  private readyCue?: AudioContext;
  private started = false;
  private closing = false;
  private disposed = false;
  private timers = new Set<ReturnType<typeof setTimeout>>();
  private fragments: LiveFragment[] = [];
  private seen = new Set<string>();
  private delegated = new Set<string>();
  private consumedInputs = 0;
  private queue: Promise<void> = Promise.resolve();

  constructor(private options: LiveOptions) {
    this.cameraSession = options.cameraSession ?? null;
    this.paused = options.initiallyPaused ?? false;
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

  /** Passive context only; never opens or resumes a billable voice session. */
  appendContext(content: string) {
    if (!this.ready || this.disposed || this.closing) return false;
    return this.send({ type: "session.commentary.append", event_id: crypto.randomUUID(), delegation_id: null, content: boundedLiveCommentary(content) });
  }

  private instructions(content: string) {
    return this.send({ type: "session.instructions.append", event_id: crypto.randomUUID(), delegation_id: null, content });
  }

  /** Camera availability is UI state, never a request to describe the scene. */
  setCameraSession(session: string | null) {
    if (session === this.cameraSession || this.disposed || this.closing) return;
    this.cameraSession = session;
    this.outputEpoch += 1;
    this.cameraTurns.forEach((turn) => { turn.controller.abort(); if (turn.timer) clearTimeout(turn.timer); });
    this.cameraTurn = undefined;
    if (!this.ready) return;
    this.cameraInstructions();
    if (this.withheldOutput) void this.resumeOutput(this.abort.signal).catch(() => this.fail("generic"));
  }

  private cameraInstructions() {
    this.instructions(this.cameraSession
      ? "The camera is available through Control. Do not describe it now. For each new spoken question, Control checks whether a fresh capture is needed. Wait silently during that check. Only describe the scene from the current request's verified result; never infer it from previous replies."
      : "The camera is unavailable. Earlier camera descriptions are historical, not evidence of the current scene. Do not claim to see the current surroundings. Continue the ordinary conversation.");
  }

  private resumeOutput(signal: AbortSignal): Promise<boolean> {
    if (signal.aborted || this.disposed || this.closing) return Promise.resolve(false);
    if (!this.withheldOutput) return Promise.resolve(true);
    const epoch = this.outputEpoch;
    if (this.outputRelease?.epoch === epoch) return this.outputRelease.promise;
    // Both a transcript and a late delegation can finish the same check.
    // Share their drain rather than cancelling one gate.release with another.
    const promise = (async () => {
      const released = await this.playback.release(signal);
      if (epoch !== this.outputEpoch || signal.aborted || this.disposed || this.closing) return false;
      if (!released) { this.fail("generic"); return false; }
      this.withheldOutput = false;
      return true;
    })();
    this.outputRelease = { epoch, promise };
    void promise.finally(() => { if (this.outputRelease?.promise === promise) this.outputRelease = undefined; });
    return promise;
  }

  private cameraInput(part: LiveFragment) {
    if (!this.cameraSession || !this.options.onCameraRequest || this.closing) return;
    let turn = this.cameraTurn;
    if (!turn || turn.result || turn.session !== this.cameraSession) {
      // Cancel only preparation. A submitted Hermes task keeps its observer.
      turn?.controller.abort();
      turn = { session: this.cameraSession, inputs: [], snapshot: [], controller: new AbortController(), delegationIds: new Set(), answered: false, inputCount: 0 };
      // Bound retained turn snapshots while keeping recent delegation receipts.
      if (this.cameraTurns.length >= 32) this.cameraTurns.shift()?.controller.abort();
      this.cameraTurns.push(turn);
      this.cameraTurn = turn;
      this.outputEpoch += 1;
      this.withheldOutput = true;
      this.playback.hold();
      this.instructions("Stop speaking and wait silently. Control is checking the current spoken request. Any previous camera evidence is stale for this request. Do not guess the scene or announce a result until Control returns this request's outcome.");
    }
    turn.inputs.push(part);
    turn.snapshot = [...this.fragments];
    turn.inputCount = this.fragments.filter((fragment) => fragment.role === "user").length;
    if (turn.timer) { clearTimeout(turn.timer); this.timers.delete(turn.timer); }
    const pending = turn;
    turn.timer = this.later(() => { pending.timer = undefined; void this.processCameraTurn(pending); }, 750);
  }

  private cameraTurnCurrent(turn: CameraTurn) {
    return !this.disposed && !this.closing && !turn.controller.signal.aborted && turn.session === this.cameraSession && this.cameraTurn === turn;
  }

  private cameraProgress(turn: CameraTurn, content: string) {
    turn.progress = (turn.progress ?? Promise.resolve()).then(async () => {
      if (!this.cameraTurnCurrent(turn) || !await this.resumeOutput(turn.controller.signal) || !this.cameraTurnCurrent(turn)) return;
      this.instructions("Speak only the verified task status that follows. A pending task or approval is not a visual result. Do not describe the current scene until its confirmed result is available.");
      this.send({ type: "session.commentary.append", event_id: crypto.randomUUID(), delegation_id: [...turn.delegationIds][0] ?? null, content: boundedLiveCommentary(content) });
    }).catch(() => { if (this.cameraTurnCurrent(turn)) this.fail("generic"); });
  }

  private async processCameraTurn(turn: CameraTurn) {
    if (!this.cameraTurnCurrent(turn) || !this.options.onCameraRequest) return;
    if (turn.result) return;
    const text = [...turn.inputs].sort((a, b) => a.start - b.start || a.order - b.order).map((part) => part.text).join("");
    this.consumedInputs = Math.max(this.consumedInputs, turn.inputCount);
    // Install the shared promise before invoking application code so provider
    // delegation and transcript routing can never submit this turn twice.
    turn.result = Promise.resolve().then(() => this.options.onCameraRequest!(voiceContext(turn.snapshot), turn.controller.signal, (content) => this.cameraProgress(turn, content), text));
    try {
      const result = await turn.result;
      await turn.progress;
      if (!this.cameraTurnCurrent(turn)) return;
      if (result !== null) {
        turn.answered = true;
        if (await this.resumeOutput(turn.controller.signal)) {
          this.instructions("Control has completed this spoken request. Speak only the verified result that follows. Do not repeat any earlier unverified scene description or delegate this same request again.");
          this.send({ type: "session.commentary.append", event_id: crypto.randomUUID(), delegation_id: [...turn.delegationIds][0] ?? null, content: boundedLiveCommentary(result) });
        }
      } else if (turn.delegationIds.size) {
        await this.delegateCameraTurn(turn);
      } else if (await this.resumeOutput(turn.controller.signal) && !turn.delegationIds.size && this.cameraTurnCurrent(turn)) {
        this.instructions("The camera check is complete: the latest spoken request does not need a picture. Respond to that request now using the ordinary conversation and delegation rules. Any speech during the check was withheld; give the answer once without repeating completed work.");
      }
    } catch {
      if (this.cameraTurnCurrent(turn)) this.fail("generic");
    }
  }

  private async delegateCameraTurn(turn: CameraTurn) {
    if (turn.response) return turn.response;
    turn.response = (async () => {
      if (!this.cameraTurnCurrent(turn) || !this.options.onDelegation || !turn.result) return;
      const visual = await turn.result;
      if (visual !== null || !this.cameraTurnCurrent(turn)) return;
      // The current turn has already been classified as nonvisual. An empty
      // requestText skips a second camera classifier in the normal task path.
      const result = await this.options.onDelegation(voiceContext(turn.snapshot), this.abort.signal, (content) => this.cameraProgress(turn, content), "");
      await turn.progress;
      turn.answered = true;
      if (!this.cameraTurnCurrent(turn)) return;
      if (!this.withheldOutput || await this.resumeOutput(turn.controller.signal)) {
        this.send({ type: "session.commentary.append", event_id: crypto.randomUUID(), delegation_id: [...turn.delegationIds][0] ?? null, content: boundedLiveCommentary(result) });
      }
    })();
    return turn.response;
  }

  private fail(issue: LiveIssue) {
    if (this.disposed) return;
    this.dispose();
    this.options.onIssue(issue);
    this.options.onPhase("error");
  }

  async play() {
    if (this.disposed) return;
    try { await this.audio.play(); if (!this.disposed) this.options.onPlaybackBlocked(false); }
    catch { if (!this.disposed) this.options.onPlaybackBlocked(true); }
  }

  private updateInput() {
    if (this.disposed || this.closing) return;
    const tracks = this.input?.stream.getAudioTracks() ?? [];
    const connected = this.ready && this.peer?.connectionState === "connected"
      && tracks.length > 0 && tracks.every((track) => track.readyState !== "ended" && !track.muted);
    if (connected) this.connectedOnce = true;
    // Disabled tracks send silence, so startup and pause never buffer speech
    // that could be sent unexpectedly when the user resumes.
    tracks.forEach((track) => { track.enabled = connected && !this.paused; });
    const next = connected ? (this.paused ? "paused" : "listening") : "connecting";
    if (next === this.inputPhase) return;
    this.inputPhase = next;
    this.options.onPhase(next);
    if (next === "listening") this.playReadyCue();
  }

  private playReadyCue() {
    const context = this.readyCue;
    if (!context || context.state !== "running") return;
    try {
      const tone = context.createOscillator();
      const gain = context.createGain();
      const now = context.currentTime;
      tone.frequency.setValueAtTime(660, now);
      tone.frequency.linearRampToValueAtTime(880, now + 0.12);
      gain.gain.setValueAtTime(0, now);
      gain.gain.linearRampToValueAtTime(0.06, now + 0.015);
      gain.gain.linearRampToValueAtTime(0, now + 0.16);
      tone.connect(gain).connect(context.destination);
      tone.onended = () => { tone.disconnect(); gain.disconnect(); };
      tone.start(now);
      tone.stop(now + 0.18);
    } catch { /* Readiness remains visible if the browser blocks the cue. */ }
  }

  setPaused(paused: boolean) {
    if (!this.connectedOnce || this.disposed || this.closing || paused === this.paused) return;
    this.paused = paused;
    this.updateInput();
  }

  async start() {
    if (this.started || this.disposed) return;
    this.started = true;
    if (!this.options.acquireInput) void this.playback.start();
    this.options.onPhase("connecting");
    // Unlock the optional cue on the gesture, before permission/HTTP awaits.
    // Voice previews supply their own silent input and must never play a cue.
    if (!this.options.acquireInput && typeof AudioContext === "function") {
      try { this.readyCue = new AudioContext(); void this.readyCue.resume().catch(() => {}); }
      catch { /* A visual readiness indicator is always available. */ }
    }
    this.later(() => { if (!this.connectedOnce) this.fail("network"); }, this.options.startupTimeoutMs ?? 60_000);
    try {
      // Acquire on the user gesture. A late permission grant is still cleaned up.
      const input = this.options.acquireInput
        ? await this.options.acquireInput(this.abort.signal)
        : await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
          .then((stream): LiveInput => ({ stream, release: () => stream.getTracks().forEach((track) => track.stop()) }));
      if (this.disposed) { input.release(); return; }
      this.input = input;
      const stream = input.stream;
      const peer = this.peer = new RTCPeerConnection();
      peer.addEventListener("track", (event) => {
        if (this.disposed) return;
        const output = new MediaStream([event.track]);
        this.audio.srcObject = output;
        this.playback.attach(output);
        void this.play();
      });
      peer.addEventListener("connectionstatechange", () => {
        if (["failed", "disconnected"].includes(peer.connectionState)) this.fail("network");
        else this.updateInput();
      });
      stream.getAudioTracks().forEach((track) => {
        track.enabled = false;
        track.addEventListener("ended", () => { if (!this.closing) this.fail("network"); });
        track.addEventListener("mute", () => this.updateInput());
        track.addEventListener("unmute", () => this.updateInput());
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
      if (this.ready) return;
      this.ready = true;
      if (this.closing) this.send({ type: "session.close" });
      else {
        if (this.cameraSession) this.cameraInstructions();
        if (this.options.initialCommentary) this.send({ type: "session.commentary.append", event_id: crypto.randomUUID(), delegation_id: null, content: boundedLiveCommentary(this.options.initialCommentary) });
        this.updateInput();
      }
    } else if (event.type === "session.closed") {
      const expected = this.closing || event.reason === "close_requested";
      this.dispose();
      if (!expected) this.options.onIssue("network");
      this.options.onPhase(expected ? "idle" : "error");
    } else if (event.type === "error") {
      // Provider error payloads can contain private content; never log them.
      this.fail("generic");
    } else if (event.type === "session.input_transcript.delta" || event.type === "session.output_transcript.delta") {
      if (!this.options.onTranscript && (this.options.disableDelegation || !this.options.onDelegation)) return;
      if (typeof event.delta !== "string" || !event.delta || typeof event.start_ms !== "number" || !Number.isFinite(event.start_ms) || event.start_ms < 0 || typeof event.end_ms !== "number" || !Number.isFinite(event.end_ms) || event.end_ms < event.start_ms) return;
      if (this.fragments.reduce((sum, part) => sum + part.text.length, 0) + event.delta.length > 48_000) { this.fail("contextFull"); return; }
      const role = event.type === "session.input_transcript.delta" ? "user" : "assistant";
      // Never persist words the user was prevented from hearing as an answer.
      if (role === "assistant" && this.withheldOutput) return;
      const part: LiveFragment = { role, text: event.delta, start: event.start_ms, end: event.end_ms, order: this.fragments.length };
      this.fragments.push(part);
      this.options.onTranscript?.([...this.fragments]);
      if (role === "user") this.cameraInput(part);
    } else if (event.type === "session.delegation.created" && !this.closing && !this.options.disableDelegation && this.options.onDelegation) {
      const onDelegation = this.options.onDelegation;
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
        const cameraTurn = [...this.cameraTurns].reverse().find((turn) => turn.inputs.some((part) => snapshot.includes(part)));
        if (cameraTurn && cameraTurn.session === this.cameraSession) {
          cameraTurn.delegationIds.add(id);
          if (cameraTurn.result) {
            const result = await cameraTurn.result;
            if (result === null) await this.delegateCameraTurn(cameraTurn);
            else if (cameraTurn.answered && this.cameraTurnCurrent(cameraTurn)) this.send({ type: "session.thinking.append", event_id: crypto.randomUUID(), delegation_id: id, content: "Control already handled this exact spoken request and returned its result. Do not repeat the task, capture or answer." });
          } else await this.processCameraTurn(cameraTurn);
          return;
        }
        if (inputCount <= this.consumedInputs) {
          this.send({ type: "session.commentary.append", event_id: crypto.randomUUID(), delegation_id: id, content: "No new transcribed request is available. Ask the user to repeat or clarify; do not claim a new task was submitted." });
          return;
        }
        const requestText = snapshot.filter((part) => part.role === "user").slice(this.consumedInputs).map((part) => part.text).join("");
        this.consumedInputs = inputCount;
        const result = await onDelegation(voiceContext(snapshot), this.abort.signal, (content) => {
          if (!this.disposed && !this.closing) this.send({ type: "session.commentary.append", event_id: crypto.randomUUID(), delegation_id: id, content: boundedLiveCommentary(content) });
        }, requestText);
        if (this.disposed || this.closing) return;
        this.send({ type: "session.commentary.append", event_id: crypto.randomUUID(), delegation_id: id, content: boundedLiveCommentary(result) });
      }).catch(() => { if (!this.disposed && !this.closing) this.fail("generic"); });
    }
  }

  stop() {
    if (this.disposed || this.closing) return;
    this.closing = true;
    this.cameraTurns.forEach((turn) => turn.controller.abort());
    this.playback.dispose();
    // End capture immediately; retain the transport to receive final usage.
    this.input?.stream.getTracks().forEach((track) => { track.enabled = false; });
    this.input?.release();
    this.input = undefined;
    this.audio.pause();
    void this.readyCue?.close().catch(() => {});
    this.readyCue = undefined;
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
    this.cameraTurns.forEach((turn) => turn.controller.abort());
    this.playback.dispose();
    this.timers.forEach(clearTimeout);
    this.timers.clear();
    this.input?.release();
    this.input = undefined;
    this.channel?.close();
    this.peer?.close();
    this.audio.pause();
    this.audio.srcObject = null;
    void this.readyCue?.close().catch(() => {});
    this.readyCue = undefined;
  }
}
