import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OpenAILiveClient, boundedLiveCommentary, voiceContext } from "../lib/openaiLiveClient";

class Channel extends EventTarget {
  readyState = "open";
  send = vi.fn();
  close = vi.fn(() => { this.readyState = "closed"; this.dispatchEvent(new Event("close")); });
  emit(event: Record<string, unknown>) { this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(event) })); }
}
class Peer extends EventTarget {
  static latest: Peer;
  channel = new Channel();
  iceGatheringState = "complete";
  connectionState = "connected";
  localDescription = { sdp: "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n" };
  addTrack = vi.fn();
  createDataChannel = vi.fn(() => this.channel);
  createOffer = vi.fn(async () => ({ type: "offer", sdp: this.localDescription.sdp }));
  setLocalDescription = vi.fn(async () => {});
  setRemoteDescription = vi.fn(async () => {});
  close = vi.fn();
  constructor() { super(); Peer.latest = this; }
}

class CueContext {
  static latest: CueContext;
  state = "running";
  currentTime = 0;
  destination = {};
  createOscillator = vi.fn(() => ({
    frequency: { setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn() },
    connect: (node: unknown) => node, disconnect: vi.fn(), start: vi.fn(), stop: vi.fn(), onended: null,
  }));
  createGain = vi.fn(() => ({ gain: { setValueAtTime: vi.fn(), linearRampToValueAtTime: vi.fn() }, connect: vi.fn(), disconnect: vi.fn() }));
  resume = vi.fn(async () => {});
  close = vi.fn(async () => {});
  constructor() { CueContext.latest = this; }
}

describe("GPT-Live WebRTC", () => {
  let track: EventTarget & { enabled: boolean; muted: boolean; readyState: string; stop: ReturnType<typeof vi.fn> };
  let getUserMedia: ReturnType<typeof vi.fn>;
  let client: OpenAILiveClient;
  function setup() {
    const options = {
      negotiate: vi.fn(async () => ({ session: { id: "live_opaque" }, transport: { sdp: "answer" } })),
      onPhase: vi.fn(), onIssue: vi.fn(), onTranscript: vi.fn(), onPlaybackBlocked: vi.fn(),
      onDelegation: vi.fn(async (_context: string, _signal: AbortSignal, _progress: (content: string) => void) => "Verified backend result"),
    };
    client = new OpenAILiveClient(options);
    return options;
  }
  beforeEach(() => {
    vi.useFakeTimers();
    track = Object.assign(new EventTarget(), { enabled: true, muted: false, readyState: "live", stop: vi.fn() });
    getUserMedia = vi.fn(async () => ({ getTracks: () => [track], getAudioTracks: () => [track] }));
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
    vi.stubGlobal("RTCPeerConnection", Peer);
    vi.stubGlobal("AudioContext", CueContext);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  });
  afterEach(() => { client?.dispose(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("negotiates media and waits for session.started without sending Realtime commands", async () => {
    const options = setup();
    expect(getUserMedia).not.toHaveBeenCalled();
    await client.start();
    const peer = Peer.latest;
    expect(peer.createDataChannel).toHaveBeenCalledWith("oai-events");
    expect(options.negotiate).toHaveBeenCalledWith(peer.localDescription.sdp, expect.any(AbortSignal));
    expect(peer.setRemoteDescription).toHaveBeenCalledWith({ type: "answer", sdp: "answer" });
    expect(peer.channel.send).not.toHaveBeenCalled();
    expect(options.onPhase).toHaveBeenLastCalledWith("connecting");
    expect(track.enabled).toBe(false);
    expect(CueContext.latest.createOscillator).not.toHaveBeenCalled();
    peer.channel.emit({ type: "session.started", session: { id: "live_opaque" } });
    expect(options.onPhase).toHaveBeenLastCalledWith("listening");
    expect(track.enabled).toBe(true);
    expect(CueContext.latest.createOscillator).toHaveBeenCalledOnce();
    peer.channel.emit({ type: "session.started" });
    expect(CueContext.latest.createOscillator).toHaveBeenCalledOnce();
  });

  it("announces readiness only after both Live startup and a usable microphone connection", async () => {
    const options = setup();
    await client.start();
    Peer.latest.connectionState = "connecting";
    Peer.latest.channel.emit({ type: "session.started" });
    expect(track.enabled).toBe(false);
    expect(options.onPhase).toHaveBeenLastCalledWith("connecting");
    expect(CueContext.latest.createOscillator).not.toHaveBeenCalled();
    track.muted = true;
    Peer.latest.connectionState = "connected";
    Peer.latest.dispatchEvent(new Event("connectionstatechange"));
    expect(track.enabled).toBe(false);
    track.muted = false;
    track.dispatchEvent(new Event("unmute"));
    expect(track.enabled).toBe(true);
    expect(options.onPhase).toHaveBeenLastCalledWith("listening");
    expect(CueContext.latest.createOscillator).toHaveBeenCalledOnce();
  });

  it("starts a fresh explanation only after session.started with no previous delegation id", async () => {
    const options = setup();
    client.dispose();
    client = new OpenAILiveClient({ ...options, initialCommentary: "Explain the verified answer in the startup context; do not repeat its task." });
    await client.start();
    expect(Peer.latest.channel.send).not.toHaveBeenCalled();
    Peer.latest.channel.emit({ type: "session.started" });
    Peer.latest.channel.emit({ type: "session.started" });
    const events = Peer.latest.channel.send.mock.calls.map(([event]) => JSON.parse(event));
    expect(events).toEqual([expect.objectContaining({ type: "session.commentary.append", delegation_id: null, content: "Explain the verified answer in the startup context; do not repeat its task." })]);
    expect(options.onDelegation).not.toHaveBeenCalled();
  });

  it("silences microphone tracks while paused and resumes the same session with no renegotiation", async () => {
    const options = setup();
    await client.start();
    client.setPaused(false);
    expect(track.enabled).toBe(false);
    Peer.latest.channel.emit({ type: "session.started" });
    client.setPaused(true);
    expect(track.enabled).toBe(false);
    expect(track.stop).not.toHaveBeenCalled();
    expect(options.onPhase).toHaveBeenLastCalledWith("paused");
    expect(Peer.latest.close).not.toHaveBeenCalled();
    expect(Peer.latest.channel.send).not.toHaveBeenCalled();
    // Source recovery and duplicate startup must never unpause automatically.
    track.dispatchEvent(new Event("unmute"));
    Peer.latest.channel.emit({ type: "session.started" });
    expect(track.enabled).toBe(false);
    expect(CueContext.latest.createOscillator).toHaveBeenCalledOnce();
    client.setPaused(false);
    expect(track.enabled).toBe(true);
    expect(options.onPhase).toHaveBeenLastCalledWith("listening");
    expect(CueContext.latest.createOscillator).toHaveBeenCalledTimes(2);
    expect(getUserMedia).toHaveBeenCalledOnce();
    expect(options.negotiate).toHaveBeenCalledOnce();
    client.setPaused(true);
    client.stop();
    client.setPaused(false);
    expect(track.enabled).toBe(false);
    expect(track.stop).toHaveBeenCalledOnce();
    expect(CueContext.latest.close).toHaveBeenCalledOnce();
    expect(options.onPhase).toHaveBeenLastCalledWith("stopping");
  });

  it("keeps a restored paused microphone disabled through startup until the user resumes", async () => {
    const options = setup();
    client.dispose();
    client = new OpenAILiveClient({ ...options, initiallyPaused: true });
    await client.start();
    expect(track.enabled).toBe(false);
    Peer.latest.dispatchEvent(new Event("connectionstatechange"));
    expect(options.onPhase).toHaveBeenLastCalledWith("connecting");
    Peer.latest.channel.emit({ type: "session.started" });
    expect(track.enabled).toBe(false);
    expect(options.onPhase).toHaveBeenLastCalledWith("paused");
    expect(CueContext.latest.createOscillator).not.toHaveBeenCalled();
    Peer.latest.channel.emit({ type: "session.started" });
    expect(track.enabled).toBe(false);
    client.setPaused(false);
    expect(track.enabled).toBe(true);
    expect(options.onPhase).toHaveBeenLastCalledWith("listening");
    expect(CueContext.latest.createOscillator).toHaveBeenCalledOnce();
  });

  it("times out an unready media connection without ever signalling listening", async () => {
    const options = setup();
    await client.start();
    Peer.latest.connectionState = "connecting";
    Peer.latest.channel.emit({ type: "session.started" });
    await vi.advanceTimersByTimeAsync(60_001);
    expect(options.onIssue).toHaveBeenCalledWith("network");
    expect(options.onPhase).not.toHaveBeenCalledWith("listening");
    expect(CueContext.latest.createOscillator).not.toHaveBeenCalled();
    expect(track.stop).toHaveBeenCalledOnce();
  });

  it("keeps readiness usable when the browser cannot play the optional cue", async () => {
    const options = setup();
    await client.start();
    CueContext.latest.state = "suspended";
    Peer.latest.channel.emit({ type: "session.started" });
    expect(options.onPhase).toHaveBeenLastCalledWith("listening");
    expect(track.enabled).toBe(true);
    expect(options.onIssue).not.toHaveBeenCalled();
  });

  it("includes late captions, delegates once and sends the correlated confirmed result while keeping the mic live", async () => {
    const options = setup();
    await client.start();
    const channel = Peer.latest.channel;
    channel.emit({ type: "session.started" });
    const delegation = { type: "session.delegation.created", event_id: "event-d1", offset_ms: 900, delegation: { id: "item_opaque", target: "client" } };
    channel.emit(delegation);
    channel.emit({ type: "session.input_transcript.delta", event_id: "t1", delta: "Busca mis archivos", start_ms: 0, end_ms: 800 });
    channel.emit({ type: "session.input_transcript.delta", event_id: "t2", delta: " de ayer.", start_ms: 800, end_ms: 1100 });
    channel.emit({ ...delegation, event_id: "event-d1-repeated" });
    await vi.advanceTimersByTimeAsync(751);
    expect(options.onDelegation).toHaveBeenCalledTimes(1);
    expect(options.onDelegation).toHaveBeenCalledWith("User: Busca mis archivos de ayer.", expect.any(AbortSignal), expect.any(Function));
    expect(JSON.parse(channel.send.mock.calls[0][0])).toMatchObject({ type: "session.commentary.append", delegation_id: "item_opaque", content: "Verified backend result" });
    expect(track.enabled).toBe(true);
    expect(track.stop).not.toHaveBeenCalled();
  });

  it("does not submit a delegation without new input or infer task text from metadata", async () => {
    const options = setup();
    await client.start();
    Peer.latest.channel.emit({ type: "session.delegation.created", delegation: { id: "item_no_input", target: "client" } });
    await vi.advanceTimersByTimeAsync(751);
    expect(options.onDelegation).not.toHaveBeenCalled();
    expect(JSON.parse(Peer.latest.channel.send.mock.calls[0][0]).content).toContain("repeat or clarify");
  });

  it("waits for final close before releasing transport and never replays completed tasks", async () => {
    const options = setup();
    await client.start();
    const channel = Peer.latest.channel;
    channel.emit({ type: "session.started" });
    client.stop();
    expect(track.enabled).toBe(false);
    expect(channel.close).not.toHaveBeenCalled();
    expect(JSON.parse(channel.send.mock.calls[0][0])).toEqual({ type: "session.close" });
    channel.emit({ type: "session.closed", reason: "close_requested", usage: { seconds: 12 } });
    expect(track.stop).toHaveBeenCalledOnce();
    expect(Peer.latest.close).toHaveBeenCalledOnce();
    expect(options.onPhase).toHaveBeenLastCalledWith("idle");
    expect(options.onIssue).not.toHaveBeenCalled();
  });

  it("reports incomplete finalization and releases resources on close timeout", async () => {
    const options = setup();
    await client.start();
    Peer.latest.channel.emit({ type: "session.started" });
    client.stop();
    await vi.advanceTimersByTimeAsync(15_001);
    expect(options.onIssue).toHaveBeenCalledWith("unconfirmed");
    expect(track.stop).toHaveBeenCalledOnce();
  });

  it("releases a microphone granted after cancellation without creating a call", async () => {
    const options = setup();
    let grant!: (value: unknown) => void;
    getUserMedia.mockReturnValue(new Promise((resolve) => { grant = resolve; }));
    const started = client.start();
    client.stop();
    grant({ getTracks: () => [track], getAudioTracks: () => [track] });
    await started;
    expect(track.stop).toHaveBeenCalledOnce();
    expect(options.negotiate).not.toHaveBeenCalled();
  });

  it("discards stale delegated results after navigation disposal", async () => {
    const options = setup();
    let finish!: (value: string) => void;
    options.onDelegation.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    await client.start();
    const channel = Peer.latest.channel;
    channel.emit({ type: "session.started" });
    channel.emit({ type: "session.input_transcript.delta", delta: "Hazlo", start_ms: 0, end_ms: 500 });
    channel.emit({ type: "session.delegation.created", delegation: { id: "item_work", target: "client" } });
    await vi.advanceTimersByTimeAsync(751);
    client.dispose();
    finish("Finished in old session");
    await vi.advanceTimersByTimeAsync(1);
    expect(channel.send.mock.calls.map((call) => JSON.parse(call[0]).type)).not.toContain("session.commentary.append");
  });

  it("fails closed on oversized events without logging provider payloads", async () => {
    const options = setup();
    const log = vi.spyOn(console, "log");
    await client.start();
    Peer.latest.channel.dispatchEvent(new MessageEvent("message", { data: "secret".repeat(12_000) }));
    expect(options.onIssue).toHaveBeenCalledWith("contextFull");
    expect(track.stop).toHaveBeenCalledOnce();
    expect(log).not.toHaveBeenCalled();
  });

  it("orders caption fragments by media time without assuming arrival order", () => {
    expect(voiceContext([
      { role: "user", text: " viernes", start: 100, end: 200, order: 0 },
      { role: "user", text: "El", start: 0, end: 100, order: 1 },
    ])).toBe("User: El viernes");
  });

  it("keeps each queued delegation tied to its own transcript boundary", async () => {
    const options = setup();
    let finishFirst!: (value: string) => void;
    options.onDelegation.mockImplementationOnce(() => new Promise((resolve) => { finishFirst = resolve; }));
    await client.start();
    const channel = Peer.latest.channel;
    channel.emit({ type: "session.started" });
    channel.emit({ type: "session.input_transcript.delta", delta: "Primera tarea.", start_ms: 0, end_ms: 500 });
    channel.emit({ type: "session.delegation.created", offset_ms: 600, delegation: { id: "item_first", target: "client" } });
    await vi.advanceTimersByTimeAsync(751);
    channel.emit({ type: "session.input_transcript.delta", delta: " Segunda tarea.", start_ms: 1000, end_ms: 1500 });
    channel.emit({ type: "session.delegation.created", offset_ms: 1600, delegation: { id: "item_second", target: "client" } });
    await vi.advanceTimersByTimeAsync(751);
    channel.emit({ type: "session.input_transcript.delta", delta: " Tercera tarea.", start_ms: 2000, end_ms: 2500 });
    channel.emit({ type: "session.delegation.created", offset_ms: 2600, delegation: { id: "item_third", target: "client" } });
    await vi.advanceTimersByTimeAsync(751);
    finishFirst("First finished");
    await vi.advanceTimersByTimeAsync(1);
    expect(options.onDelegation.mock.calls.map((args) => args[0])).toEqual([
      "User: Primera tarea.", "User: Primera tarea. Segunda tarea.", "User: Primera tarea. Segunda tarea. Tercera tarea.",
    ]);
  });

  it("bounds multilingual results below the 500-token append limit with an explicit excerpt notice", () => {
    const result = boundedLiveCommentary("確認 😀 " .repeat(1000));
    expect(new TextEncoder().encode(result).length).toBeLessThan(500);
    expect(result).toContain("full answer is in the chat");
    expect(result).not.toContain("\uFFFD");
  });
});
