import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OpenAILiveClient, boundedLiveCommentary, voiceContext } from "../lib/openaiLiveClient";

const playback = vi.hoisted(() => ({ release: vi.fn(), hold: vi.fn() }));
vi.mock("../lib/livePlaybackGate", () => ({ LivePlaybackGate: class {
  constructor(private audio: HTMLAudioElement) {}
  start = vi.fn(async () => true);
  attach = vi.fn();
  hold() { this.audio.muted = true; playback.hold(); }
  async release(signal?: AbortSignal) { const allowed = await playback.release(signal); if (allowed && !signal?.aborted) this.audio.muted = false; return allowed; }
  dispose() { this.audio.muted = true; }
} }));

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
      onDelegation: vi.fn(async (_context: string, _signal: AbortSignal, _progress: (content: string) => void, _requestText: string) => "Verified backend result"),
    };
    client = new OpenAILiveClient(options);
    return options;
  }
  beforeEach(() => {
    vi.useFakeTimers();
    playback.hold.mockClear(); playback.release.mockReset().mockResolvedValue(true);
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

  it("tees the same microphone and sends voice observations only as quiet, non-authorizing context", async () => {
    const options = setup(); client.dispose();
    const observe = vi.fn();
    client = new OpenAILiveClient({ ...options, onMicrophone: observe });
    expect(client.appendSpeakerObservation("recognized", "Juan", Date.now())).toBe(false);
    await client.start();
    Peer.latest.channel.emit({ type: "session.started" });
    const stream = await getUserMedia.mock.results[0].value;
    expect(observe).toHaveBeenLastCalledWith(stream);
    client.appendSpeakerObservation("recognized", "Juan", Date.now());
    const event = JSON.parse(Peer.latest.channel.send.mock.calls.at(-1)![0]);
    expect(event.type).toBe("session.thinking.append");
    expect(event.content).toContain("not authentication or permission");
    expect(event.content).toContain('"expiresAfterSeconds":30');
    expect(options.onDelegation).not.toHaveBeenCalled();
    client.setPaused(true); expect(observe).toHaveBeenLastCalledWith(null);
    client.setPaused(false); expect(observe).toHaveBeenLastCalledWith(stream);
    expect(getUserMedia).toHaveBeenCalledOnce();
    client.stop(); expect(observe).toHaveBeenLastCalledWith(null);
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
    expect(options.onDelegation).toHaveBeenCalledWith("User: Busca mis archivos de ayer.", expect.any(AbortSignal), expect.any(Function), "Busca mis archivos de ayer.");
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
    expect(options.onDelegation.mock.calls.map((args) => args[3])).toEqual(["Primera tarea.", " Segunda tarea.", " Tercera tarea."]);
  });

  it("appends camera context only to a ready existing call without delegation or connection side effects", async () => {
    const options = setup();
    expect(client.appendContext("Camera evidence")).toBe(false);
    expect(getUserMedia).not.toHaveBeenCalled();
    await client.start();
    expect(client.appendContext("Camera evidence")).toBe(false);
    const channel = Peer.latest.channel;
    channel.emit({ type: "session.started" });
    expect(client.appendContext("Camera evidence: a book")).toBe(true);
    expect(JSON.parse(channel.send.mock.calls.at(-1)![0])).toMatchObject({ type: "session.commentary.append", delegation_id: null, content: "Camera evidence: a book" });
    expect(options.onDelegation).not.toHaveBeenCalled();
    client.stop();
    expect(client.appendContext("Late camera evidence")).toBe(false);
    expect(options.negotiate).toHaveBeenCalledOnce();
  });

  function cameraSetup(handler: (context: string, signal: AbortSignal, progress: (content: string) => void, text: string) => Promise<string | null>) {
    const options = setup(); client.dispose();
    client = new OpenAILiveClient({ ...options, cameraSession: "camera-one", onCameraRequest: handler });
    return options;
  }
  const input = (text: string, start = 0) => Peer.latest.channel.emit({ type: "session.input_transcript.delta", delta: text, start_ms: start, end_ms: start + 400 });
  const sent = () => Peer.latest.channel.send.mock.calls.map(([raw]) => JSON.parse(raw));

  it("captures a spoken question without delegation, withholding unsupported speech until evidence arrives", async () => {
    let finish!: (text: string) => void;
    const camera = vi.fn(() => new Promise<string>((resolve) => { finish = resolve; }));
    const options = cameraSetup(camera); await client.start(); Peer.latest.channel.emit({ type: "session.started" });
    input("¿Qué ves?");
    expect(playback.hold).toHaveBeenCalledOnce();
    Peer.latest.channel.emit({ type: "session.output_transcript.delta", delta: "Veo una puerta inventada", start_ms: 500, end_ms: 900 });
    await vi.advanceTimersByTimeAsync(751);
    expect(camera).toHaveBeenCalledWith("User: ¿Qué ves?", expect.any(AbortSignal), expect.any(Function), "¿Qué ves?");
    expect(options.onDelegation).not.toHaveBeenCalled();
    expect(sent().filter((event) => event.type === "session.commentary.append")).toEqual([]);
    expect(options.onTranscript.mock.calls.at(-1)?.[0].map((part: any) => part.text).join("")).not.toContain("inventada");
    finish("Current evidence: objects on a shelf"); await vi.advanceTimersByTimeAsync(1);
    expect(playback.release).toHaveBeenCalledOnce();
    expect(sent().filter((event) => event.type === "session.commentary.append")).toEqual([expect.objectContaining({ delegation_id: null, content: "Current evidence: objects on a shelf" })]);
  });

  it.each(["early", "late"])("shares one capture and result with an %s provider delegation", async (timing) => {
    const camera = vi.fn(async () => "Verified current scene"); const options = cameraSetup(camera);
    await client.start(); Peer.latest.channel.emit({ type: "session.started" });
    const delegate = () => Peer.latest.channel.emit({ type: "session.delegation.created", offset_ms: 500, delegation: { id: "camera-delegation", target: "client" } });
    if (timing === "early") delegate();
    input("¿Qué ves?"); await vi.advanceTimersByTimeAsync(751);
    if (timing === "late") { delegate(); await vi.advanceTimersByTimeAsync(751); }
    expect(camera).toHaveBeenCalledOnce(); expect(options.onDelegation).not.toHaveBeenCalled();
    expect(sent().filter((event) => event.type === "session.commentary.append")).toHaveLength(1);
  });

  it("groups fragments and routes a fresh follow-up without earlier directly answered speech", async () => {
    const camera = vi.fn(async (_context: string, _signal: AbortSignal, _progress: (text: string) => void, text: string) => text === "Hola" ? null : `Fresh: ${text}`);
    cameraSetup(camera); await client.start(); Peer.latest.channel.emit({ type: "session.started" });
    input("Hola"); await vi.advanceTimersByTimeAsync(751);
    input("¿Qué ", 1000); await vi.advanceTimersByTimeAsync(300); input("ves?", 1400); await vi.advanceTimersByTimeAsync(751);
    input("¿Y ahora?", 3000); await vi.advanceTimersByTimeAsync(751);
    expect(camera.mock.calls.map((call) => call[3])).toEqual(["Hola", "¿Qué ves?", "¿Y ahora?"]);
    expect(sent().filter((event) => event.type === "session.commentary.append").map((event) => event.content)).toEqual(["Fresh: ¿Qué ves?", "Fresh: ¿Y ahora?"]);
  });

  it("leaves nonvisual speech to Live and only submits normal work when the provider delegates", async () => {
    const camera = vi.fn(async () => null); const options = cameraSetup(camera);
    await client.start(); Peer.latest.channel.emit({ type: "session.started" }); input("Busca el informe"); await vi.advanceTimersByTimeAsync(751);
    expect(camera).toHaveBeenCalledOnce(); expect(options.onDelegation).not.toHaveBeenCalled();
    Peer.latest.channel.emit({ type: "session.delegation.created", offset_ms: 500, delegation: { id: "normal-task", target: "client" } });
    await vi.advanceTimersByTimeAsync(751);
    expect(options.onDelegation).toHaveBeenCalledOnce();
    expect(options.onDelegation.mock.calls[0][3]).toBe("");
    expect(camera).toHaveBeenCalledOnce();
  });

  it("speaks a verified approval status while visual work is pending without another capture", async () => {
    let finish!: (result: string) => void;
    const camera = vi.fn((_context: string, _signal: AbortSignal, progress: (text: string) => void) => { progress("Approval is required in the chat. No action is confirmed."); return new Promise<string>((resolve) => { finish = resolve; }); });
    const options = cameraSetup(camera); await client.start(); Peer.latest.channel.emit({ type: "session.started" }); input("Mira y revisa");
    await vi.advanceTimersByTimeAsync(751);
    expect(sent().filter((event) => event.type === "session.commentary.append").map((event) => event.content)).toEqual(["Approval is required in the chat. No action is confirmed."]);
    finish("Verified visual answer"); await vi.advanceTimersByTimeAsync(1);
    expect(options.onIssue).not.toHaveBeenCalled(); expect(camera).toHaveBeenCalledOnce();
    expect(sent().filter((event) => event.type === "session.commentary.append")).toHaveLength(2);
    expect(playback.release).toHaveBeenCalledOnce();
  });

  it("shares an in-flight audio drain with a late fast nonvisual delegation", async () => {
    let drained!: (value: boolean) => void;
    playback.release.mockImplementation(() => new Promise<boolean>((resolve) => { drained = resolve; }));
    const options = cameraSetup(vi.fn(async () => null));
    await client.start(); Peer.latest.channel.emit({ type: "session.started" }); input("Busca un archivo");
    await vi.advanceTimersByTimeAsync(751);
    expect(playback.release).toHaveBeenCalledOnce();
    Peer.latest.channel.emit({ type: "session.delegation.created", offset_ms: 500, delegation: { id: "late-fast", target: "client" } });
    await vi.advanceTimersByTimeAsync(751);
    expect(options.onDelegation).toHaveBeenCalledOnce(); expect(playback.release).toHaveBeenCalledOnce();
    drained(true); await vi.advanceTimersByTimeAsync(1);
    expect(options.onIssue).not.toHaveBeenCalled();
    expect(sent().filter((event) => event.type === "session.commentary.append")).toHaveLength(1);
    expect(sent().some((event) => event.content?.includes("does not need a picture"))).toBe(false);
  });

  it("ignores camera preparation after the camera is switched off", async () => {
    let finish!: (text: string) => void;
    const camera = vi.fn((_context: string, _signal: AbortSignal) => new Promise<string>((resolve) => { finish = resolve; })); cameraSetup(camera);
    await client.start(); Peer.latest.channel.emit({ type: "session.started" }); input("Mira"); await vi.advanceTimersByTimeAsync(751);
    client.setCameraSession(null); expect(camera.mock.calls[0][1].aborted).toBe(true);
    finish("Stale image"); await vi.advanceTimersByTimeAsync(1);
    expect(sent().filter((event) => event.type === "session.commentary.append")).toEqual([]);
  });

  it("cancels an unsettled spoken capture on hangup", async () => {
    const camera = vi.fn(async () => "Unexpected image"); cameraSetup(camera);
    await client.start(); Peer.latest.channel.emit({ type: "session.started" }); input("Mira"); client.stop();
    await vi.advanceTimersByTimeAsync(751); expect(camera).not.toHaveBeenCalled();
  });

  it("fails closed when old remote speech cannot be drained", async () => {
    playback.release.mockResolvedValue(false);
    const camera = vi.fn(async () => "Verified image"); const options = cameraSetup(camera);
    await client.start(); Peer.latest.channel.emit({ type: "session.started" }); input("Mira"); await vi.advanceTimersByTimeAsync(751);
    expect(options.onIssue).toHaveBeenCalledWith("generic");
    expect(sent().filter((event) => event.type === "session.commentary.append")).toEqual([]);
  });

  it("bounds multilingual results below the 500-token append limit with an explicit excerpt notice", () => {
    const result = boundedLiveCommentary("確認 😀 " .repeat(1000));
    expect(new TextEncoder().encode(result).length).toBeLessThan(500);
    expect(result).toContain("full answer is in the chat");
    expect(result).not.toContain("\uFFFD");
  });
});
