import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { OpenAILiveVoicePreview, voicePreviewSupported } from "../lib/openaiLiveVoicePreview";

class Track extends EventTarget {
  enabled = true;
  stop = vi.fn();
}
class Stream {
  constructor(private tracks: Track[] = []) {}
  getTracks() { return this.tracks; }
  getAudioTracks() { return this.tracks; }
}
class Context {
  static instances: Context[] = [];
  static resumePromise: Promise<void> | undefined;
  track = new Track();
  destination = { stream: new Stream([this.track]), disconnect: vi.fn() };
  source = { offset: { value: 1 }, connect: vi.fn(), start: vi.fn(), stop: vi.fn(), disconnect: vi.fn() };
  createMediaStreamDestination = vi.fn(() => this.destination);
  createConstantSource = vi.fn(() => this.source);
  resume = vi.fn(() => Context.resumePromise ?? Promise.resolve());
  close = vi.fn(async () => {});
  constructor() { Context.instances.push(this); }
}
class Channel extends EventTarget {
  readyState = "open";
  send = vi.fn();
  close = vi.fn(() => { this.readyState = "closed"; this.dispatchEvent(new Event("close")); });
  emit(event: Record<string, unknown>) { this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(event) })); }
  sent() { return this.send.mock.calls.map(([data]) => JSON.parse(data)); }
}
class Peer extends EventTarget {
  static instances: Peer[] = [];
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
  constructor() { super(); Peer.instances.push(this); }
}

describe("GPT-Live voice samples", () => {
  let preview: OpenAILiveVoicePreview | undefined;
  let getUserMedia: ReturnType<typeof vi.fn>;
  let mediaDevicesDescriptor: PropertyDescriptor | undefined;
  function setup() {
    const options = {
      negotiate: vi.fn(async (_sdp: string, _signal: AbortSignal) => ({ session: { id: "live_preview" }, transport: { sdp: "answer" } })),
      onPhase: vi.fn(), onIssue: vi.fn(), onPlaybackBlocked: vi.fn(),
    };
    preview = new OpenAILiveVoicePreview(options);
    return options;
  }
  beforeEach(() => {
    vi.useFakeTimers();
    Context.instances = [];
    Context.resumePromise = undefined;
    Peer.instances = [];
    getUserMedia = vi.fn();
    mediaDevicesDescriptor = Object.getOwnPropertyDescriptor(navigator, "mediaDevices");
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
    vi.stubGlobal("isSecureContext", true);
    vi.stubGlobal("AudioContext", Context);
    vi.stubGlobal("webkitAudioContext", undefined);
    vi.stubGlobal("RTCPeerConnection", Peer);
    vi.stubGlobal("MediaStream", Stream);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue();
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
  });
  afterEach(() => {
    preview?.dispose();
    preview = undefined;
    if (mediaDevicesDescriptor) Object.defineProperty(navigator, "mediaDevices", mediaDevicesDescriptor);
    else Reflect.deleteProperty(navigator, "mediaDevices");
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("supports WebAudio without requiring microphone APIs and recognizes the Safari constructor", () => {
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: undefined });
    expect(voicePreviewSupported()).toBe(true);
    vi.stubGlobal("AudioContext", undefined);
    vi.stubGlobal("webkitAudioContext", Context);
    expect(voicePreviewSupported()).toBe(true);
    vi.stubGlobal("webkitAudioContext", undefined);
    expect(voicePreviewSupported()).toBe(false);
    vi.stubGlobal("AudioContext", Context);
    vi.stubGlobal("isSecureContext", false);
    expect(voicePreviewSupported()).toBe(false);
  });

  it("negotiates a continuous zero-valued input without ever opening the microphone", async () => {
    const options = setup();
    expect(Context.instances).toHaveLength(0);
    await preview!.start();
    const context = Context.instances[0];
    const peer = Peer.instances[0];
    expect(context.source.offset.value).toBe(0);
    expect(context.source.connect).toHaveBeenCalledWith(context.destination);
    expect(context.source.start).toHaveBeenCalledOnce();
    expect(context.resume).toHaveBeenCalledOnce();
    expect(peer.addTrack).toHaveBeenCalledWith(context.track, context.destination.stream);
    expect(options.negotiate).toHaveBeenCalledWith(peer.localDescription.sdp, expect.any(AbortSignal));
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(peer.channel.sent()).toEqual([]);
  });

  it("starts its sample exactly once after session.started and ignores unexpected delegation or captions", async () => {
    const options = setup();
    await preview!.start();
    await preview!.start();
    const channel = Peer.instances[0].channel;
    channel.emit({ type: "session.started", event_id: "start1" });
    channel.emit({ type: "session.started", event_id: "start2" });
    channel.emit({ type: "session.input_transcript.delta", delta: "Submit this task", start_ms: 0, end_ms: 100 });
    channel.emit({ type: "session.output_transcript.delta", delta: "Unexpected caption", start_ms: 0, end_ms: 100 });
    channel.emit({ type: "session.delegation.created", delegation: { id: "item_unexpected", target: "client" }, offset_ms: 200 });
    await vi.advanceTimersByTimeAsync(800);
    expect(options.negotiate).toHaveBeenCalledOnce();
    expect(Context.instances).toHaveLength(1);
    expect(options.onPhase.mock.calls.filter(([phase]) => phase === "listening")).toHaveLength(1);
    expect(channel.sent()).toEqual([expect.objectContaining({
      type: "session.commentary.append", delegation_id: null,
      content: "Begin the voice sample now following the startup instructions.",
    })]);
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it("automatically ends after 20 seconds and allows a bounded final-event drain", async () => {
    const options = setup();
    await preview!.start();
    const context = Context.instances[0];
    const peer = Peer.instances[0];
    peer.channel.emit({ type: "session.started" });
    await vi.advanceTimersByTimeAsync(19_999);
    expect(peer.channel.sent().some((event) => event.type === "session.close")).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(options.onPhase).toHaveBeenLastCalledWith("stopping");
    expect(context.track.stop).toHaveBeenCalledOnce();
    expect(context.source.stop).toHaveBeenCalledOnce();
    expect(context.close).toHaveBeenCalledOnce();
    expect(peer.close).not.toHaveBeenCalled();
    expect(peer.channel.sent().at(-1)).toEqual({ type: "session.close" });
    peer.channel.emit({ type: "session.closed", reason: "close_requested" });
    expect(options.onPhase).toHaveBeenLastCalledWith("idle");
    expect(peer.close).toHaveBeenCalledOnce();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(options.onIssue).not.toHaveBeenCalled();
    expect(context.close).toHaveBeenCalledOnce();
  });

  it("releases transport after at most 15 seconds if graceful finalization never arrives", async () => {
    const options = setup();
    await preview!.start();
    const peer = Peer.instances[0];
    peer.channel.emit({ type: "session.started" });
    await vi.advanceTimersByTimeAsync(35_000);
    expect(options.onIssue).toHaveBeenLastCalledWith("unconfirmed");
    expect(options.onPhase).toHaveBeenLastCalledWith("error");
    expect(peer.close).toHaveBeenCalledOnce();
    expect(Context.instances[0].close).toHaveBeenCalledOnce();
  });

  it("times out startup at 30 seconds and discards a late negotiation result", async () => {
    const options = setup();
    let finish!: (value: Awaited<ReturnType<typeof options.negotiate>>) => void;
    options.negotiate.mockReturnValue(new Promise((resolve) => { finish = resolve; }));
    const started = preview!.start();
    await vi.advanceTimersByTimeAsync(30_000);
    expect(options.onIssue).toHaveBeenLastCalledWith("network");
    expect(Context.instances[0].close).toHaveBeenCalledOnce();
    expect(options.negotiate.mock.calls[0][1].aborted).toBe(true);
    finish({ session: { id: "late" }, transport: { sdp: "late answer" } });
    await started;
    expect(Peer.instances[0].setRemoteDescription).not.toHaveBeenCalled();
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it.each(["stop", "dispose", "timeout"] as const)("cleans up a deferred AudioContext.resume on %s without creating a call", async (action) => {
    let resume!: () => void;
    Context.resumePromise = new Promise<void>((resolve) => { resume = resolve; });
    const options = setup();
    const started = preview!.start();
    if (action === "timeout") await vi.advanceTimersByTimeAsync(30_000);
    else preview![action]();
    const context = Context.instances[0];
    expect(context.track.stop).toHaveBeenCalledOnce();
    expect(context.source.disconnect).toHaveBeenCalledOnce();
    expect(context.close).toHaveBeenCalledOnce();
    resume();
    await started;
    expect(options.negotiate).not.toHaveBeenCalled();
    expect(Peer.instances).toHaveLength(0);
    expect(context.close).toHaveBeenCalledOnce();
  });

  it("closes WebAudio resources after provider errors and suppresses stale playback updates", async () => {
    let finishPlay!: () => void;
    vi.mocked(HTMLMediaElement.prototype.play).mockReturnValue(new Promise<void>((resolve) => { finishPlay = resolve; }));
    const options = setup();
    await preview!.start();
    const peer = Peer.instances[0];
    peer.channel.emit({ type: "session.started" });
    peer.dispatchEvent(Object.assign(new Event("track"), { track: new Track() }));
    peer.channel.emit({ type: "error", error: { message: "private provider payload" } });
    expect(options.onIssue).toHaveBeenLastCalledWith("generic");
    expect(Context.instances[0].close).toHaveBeenCalledOnce();
    finishPlay();
    await Promise.resolve();
    expect(options.onPlaybackBlocked).not.toHaveBeenCalled();
  });

  it("offers manual playback when autoplay is blocked and stops output on disposal", async () => {
    vi.mocked(HTMLMediaElement.prototype.play).mockRejectedValueOnce(new DOMException("Blocked", "NotAllowedError"));
    const options = setup();
    await preview!.start();
    Peer.instances[0].dispatchEvent(Object.assign(new Event("track"), { track: new Track() }));
    await vi.advanceTimersByTimeAsync(0);
    expect(options.onPlaybackBlocked).toHaveBeenLastCalledWith(true);
    await preview!.play();
    expect(options.onPlaybackBlocked).toHaveBeenLastCalledWith(false);
    preview!.dispose();
    expect(HTMLMediaElement.prototype.pause).toHaveBeenCalled();
    expect(Context.instances[0].close).toHaveBeenCalledOnce();
    expect(Peer.instances[0].close).toHaveBeenCalledOnce();
    expect(getUserMedia).not.toHaveBeenCalled();
  });
});
