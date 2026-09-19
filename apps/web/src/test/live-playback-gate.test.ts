import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LivePlaybackGate } from "../lib/livePlaybackGate";

class Track {
  enabled = true;
  muted = false;
  readyState = "live";
  stop = vi.fn();
}
class Node {
  connect = vi.fn();
  disconnect = vi.fn();
}
class Analyser extends Node {
  fftSize = 2048;
  amplitude = 0;
  getFloatTimeDomainData = vi.fn((samples: Float32Array) => samples.fill(this.amplitude));
}
class Context {
  static instances: Context[] = [];
  static resumePromise: Promise<void> | undefined;
  state = "running";
  frozen = false;
  get currentTime() { return this.frozen ? 0 : performance.now() / 1000; }
  destination = new Node();
  source = new Node();
  analyser = new Analyser();
  sink = Object.assign(new Node(), { gain: { value: 1 } });
  createMediaStreamSource = vi.fn(() => this.source);
  createAnalyser = vi.fn(() => this.analyser);
  createGain = vi.fn(() => this.sink);
  resume = vi.fn(() => Context.resumePromise ?? Promise.resolve());
  close = vi.fn(async () => { this.state = "closed"; });
  constructor() { Context.instances.push(this); }
}

describe("Live remote playback gate", () => {
  let gate: LivePlaybackGate;
  let audio: HTMLAudioElement;
  let track: Track;
  const stream = (tracks: Track[]) => ({ getAudioTracks: () => tracks }) as unknown as MediaStream;
  const advance = (ms: number) => vi.advanceTimersByTimeAsync(ms);
  async function start() {
    expect(await gate.start()).toBe(true);
    gate.attach(stream([track]));
    return Context.instances[0];
  }
  beforeEach(() => {
    vi.useFakeTimers();
    Context.instances = [];
    Context.resumePromise = undefined;
    vi.stubGlobal("AudioContext", Context);
    vi.stubGlobal("webkitAudioContext", undefined);
    audio = document.createElement("audio");
    track = new Track();
    gate = new LivePlaybackGate(audio);
  });
  afterEach(() => {
    gate.dispose();
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("drops old speech immediately and releases only after measured remote silence", async () => {
    const context = await start();
    context.analyser.amplitude = 0.2;
    gate.hold();
    expect(audio.muted).toBe(true);
    const released = gate.release();
    await advance(700);
    expect(audio.muted).toBe(true);
    context.analyser.amplitude = 0;
    await advance(250);
    expect(audio.muted).toBe(true);
    await advance(150);
    expect(await released).toBe(true);
    expect(audio.muted).toBe(false);
    expect(context.source.connect).toHaveBeenCalledWith(context.analyser);
    expect(context.analyser.connect).toHaveBeenCalledWith(context.sink);
    expect(context.sink.connect).toHaveBeenCalledWith(context.destination);
    expect(context.sink.gain.value).toBe(0);
    expect(track.stop).not.toHaveBeenCalled();
  });

  it("does not count a brief pause inside old speech as drained output", async () => {
    const context = await start();
    gate.hold();
    const released = gate.release();
    await advance(200);
    context.analyser.amplitude = 0.1;
    await advance(100);
    context.analyser.amplitude = 0;
    await advance(250);
    expect(audio.muted).toBe(true);
    await advance(150);
    expect(await released).toBe(true);
  });

  it("invalidates an old release when another request holds output", async () => {
    await start();
    gate.hold();
    const stale = gate.release();
    await advance(200);
    gate.hold();
    expect(await stale).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
    await advance(1_000);
    expect(audio.muted).toBe(true);
    const current = gate.release();
    await advance(350);
    expect(await current).toBe(true);
  });

  it.each([true, false])("keeps playback muted when a request is aborted (already aborted: %s)", async (alreadyAborted) => {
    await start();
    gate.hold();
    const abort = new AbortController();
    if (alreadyAborted) abort.abort();
    const released = gate.release(abort.signal);
    if (!alreadyAborted) {
      await advance(200);
      abort.abort();
    }
    expect(await released).toBe(false);
    expect(vi.getTimerCount()).toBe(0);
    await advance(1_000);
    expect(audio.muted).toBe(true);
  });

  it("times out with output still muted while remote speech continues", async () => {
    const context = await start();
    context.analyser.amplitude = 0.1;
    gate.hold();
    const released = gate.release();
    await advance(3_000);
    expect(await released).toBe(false);
    expect(audio.muted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it.each(["unsupported", "missing track", "ended track", "muted track", "disabled track", "suspended context"])(
    "never assumes silence from an unobservable remote stream: %s", async (condition) => {
      if (condition === "unsupported") {
        vi.stubGlobal("AudioContext", undefined);
        expect(await gate.start()).toBe(false);
        gate.attach(stream([track]));
      } else {
        const context = await start();
        if (condition === "missing track") gate.attach(stream([]));
        if (condition === "ended track") track.readyState = "ended";
        if (condition === "muted track") track.muted = true;
        if (condition === "disabled track") track.enabled = false;
        if (condition === "suspended context") context.state = "suspended";
      }
      gate.hold();
      expect(await gate.release()).toBe(false);
      expect(audio.muted).toBe(true);
      expect(vi.getTimerCount()).toBe(0);
    },
  );

  it("does not treat zeros from a stalled audio clock as measured silence", async () => {
    const context = await start();
    context.frozen = true;
    gate.hold();
    const released = gate.release();
    await advance(3_000);
    expect(await released).toBe(false);
    expect(audio.muted).toBe(true);
  });

  it("detaches replaced remote audio and discards a pending release", async () => {
    const context = await start();
    gate.hold();
    const released = gate.release();
    await advance(100);
    const nextTrack = new Track();
    gate.attach(stream([nextTrack]));
    expect(await released).toBe(false);
    expect(audio.muted).toBe(true);
    expect(context.source.disconnect).toHaveBeenCalledOnce();
    expect(context.analyser.disconnect).toHaveBeenCalledOnce();
    expect(context.sink.disconnect).toHaveBeenCalledOnce();
    expect(track.stop).not.toHaveBeenCalled();
    const nextRelease = gate.release();
    await advance(350);
    expect(await nextRelease).toBe(true);
  });

  it("supports a remote stream attached before the Safari audio context starts", async () => {
    vi.stubGlobal("AudioContext", undefined);
    vi.stubGlobal("webkitAudioContext", Context);
    const remote = stream([track]);
    gate.attach(remote);
    expect(await gate.start()).toBe(true);
    expect(Context.instances[0].createMediaStreamSource).toHaveBeenCalledWith(remote);
    gate.hold();
    const released = gate.release();
    await advance(350);
    expect(await released).toBe(true);
  });

  it("cleans up a pending wait on hangup without stopping the caller's remote track", async () => {
    const context = await start();
    gate.hold();
    const released = gate.release();
    gate.dispose();
    expect(await released).toBe(false);
    expect(audio.muted).toBe(true);
    expect(context.source.disconnect).toHaveBeenCalledOnce();
    expect(context.analyser.disconnect).toHaveBeenCalledOnce();
    expect(context.sink.disconnect).toHaveBeenCalledOnce();
    expect(context.close).toHaveBeenCalledOnce();
    expect(track.stop).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    await advance(1_000);
    expect(audio.muted).toBe(true);
  });

  it("cannot reopen a disposed gate when a delayed context resume finishes", async () => {
    let resume!: () => void;
    Context.resumePromise = new Promise<void>((resolve) => { resume = resolve; });
    const started = gate.start();
    gate.attach(stream([track]));
    gate.hold();
    gate.dispose();
    resume();
    expect(await started).toBe(false);
    expect(await gate.release()).toBe(false);
    expect(audio.muted).toBe(true);
    expect(Context.instances[0].close).toHaveBeenCalledOnce();
  });
});
