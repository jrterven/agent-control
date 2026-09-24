import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { PcmWindow, RecognitionCapture, pcmFromBase64, suggestsVoiceEnrollment, teeMicrophone, wavBlob } from "../lib/speakerRecognition";
import { observeScribeMicrophone, copyScribeAudio } from "../lib/scribeMicrophoneObserver";
import { useAppStore } from "../store/appStore";

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const flush = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); };
const speech = (seconds = 5) => new Int16Array(seconds * 16000).fill(1000);
const config = { enabled: true, configured: true, readyPeople: 1, generation: "version", windowSeconds: 5 };
const succeeded = { id: "job", kind: "identify", status: "succeeded", result: { state: "recognized", name: "Juan", personId: "juan", segments: [], authorizes: false } };

describe("bounded recognition capture", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    useAppStore.setState({ authState: "authenticated", userId: "owner", csrfToken: "csrf", demoMode: false });
  });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
  function setup(responder?: (path: string, init?: RequestInit) => Promise<Response> | Response) {
    const states = vi.fn();
    const fetch = vi.fn(async (path: string, init?: RequestInit) => {
      if (responder) { const result = responder(path, init); if (result) return result; }
      if (path.endsWith("pyannote")) return json(config);
      if (init?.method === "DELETE" || path.endsWith("/delivery")) return new Response(null, { status: 204 });
      if (path.includes("/jobs/")) return json(succeeded);
      return json({ generation: "version" });
    });
    vi.stubGlobal("fetch", fetch);
    const capture = new RecognitionCapture({ mode: "dictation", sessionId: "chat", onState: states });
    return { capture, fetch, states, jobs: () => fetch.mock.calls.filter(([path, init]) => path.includes("/jobs/") && init?.method === "PUT" && !path.endsWith("/delivery")) };
  }
  it("is opt-in and does not upload silence", async () => {
    const { capture, jobs } = setup(); await capture.begin(); capture.pcm(new Int16Array(80000)); await flush();
    expect(jobs()).toHaveLength(0); capture.stop();
    const disabled = setup((path) => path.endsWith("pyannote") ? json({ ...config, enabled: false }) : undefined as never);
    await disabled.capture.begin(); disabled.capture.pcm(speech()); disabled.capture.stop(); await flush();
    expect(disabled.fetch).toHaveBeenCalledTimes(1);
  });
  it("sends a mono 16k WAV with CSRF and limits submissions to one per ten seconds", async () => {
    const { capture, jobs, states } = setup(); await capture.begin(); capture.pcm(speech()); await flush();
    expect(jobs()).toHaveLength(1);
    expect(jobs()[0][1]?.headers).toMatchObject({ "Content-Type": "audio/wav", "X-CSRF-Token": "csrf" });
    expect(jobs()[0][1]?.body).toBeInstanceOf(Blob);
    expect(states).toHaveBeenLastCalledWith(expect.objectContaining({ phase: "result", captureId: capture.id, job: succeeded }));
    capture.pcm(speech()); await flush(); expect(jobs()).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(10000); capture.pcm(speech()); await flush(); expect(jobs()).toHaveLength(2); capture.stop();
  });
  it("drops windows during work and ignores a late response after stop", async () => {
    let resolve!: (value: Response) => void;
    const waiting = new Promise<Response>((done) => { resolve = done; });
    const { capture, jobs, states } = setup((path, init) => path.includes("/jobs/") && init?.method === "PUT" ? waiting : undefined as never);
    await capture.begin(); capture.pcm(speech()); await flush();
    capture.pcm(speech(20)); expect(jobs()).toHaveLength(1);
    capture.stop(); resolve(json(succeeded)); await flush();
    expect(states).toHaveBeenLastCalledWith({ phase: "idle" });
    expect(states.mock.calls.some(([state]) => state.phase === "result")).toBe(false);
  });
  it("recovers a lost receipt with GET only, never a second audio submission", async () => {
    const { capture, jobs, fetch } = setup((path, init) => {
      if (path.includes("/jobs/") && init?.method === "PUT" && !path.endsWith("/delivery")) return Promise.reject(new TypeError("network"));
      return undefined as never;
    });
    await capture.begin(); capture.pcm(speech()); await flush();
    expect(jobs()).toHaveLength(1);
    expect(fetch.mock.calls.some(([path, init]) => path.includes("/jobs/") && !init?.method)).toBe(true);
    capture.stop();
  });
  it("does not accumulate or retry failed provider work", async () => {
    const { capture, jobs, states } = setup((path, init) => path.includes("/jobs/") && init?.method === "PUT" ? json({ message: "Quota", code: "PYANNOTE_QUOTA_EXCEEDED" }, 402) : undefined as never);
    await capture.begin(); capture.pcm(speech()); await flush();
    await vi.advanceTimersByTimeAsync(30000); capture.pcm(speech(20)); await flush();
    expect(jobs()).toHaveLength(1); expect(states).toHaveBeenLastCalledWith(expect.objectContaining({ phase: "unavailable", errorCode: "PYANNOTE_QUOTA_EXCEEDED" })); capture.stop();
  });
});

it("clears partial PCM on reset and emits exact windows", () => {
  const sizes: number[] = []; const speechFlags: boolean[] = [];
  const collector = new PcmWindow(5, (pcm, flag) => { sizes.push(pcm.length); speechFlags.push(flag); });
  collector.push(speech(3)); collector.clear(); collector.push(speech(2)); expect(sizes).toEqual([]);
  collector.push(speech(3)); collector.push(new Int16Array(80000));
  expect(sizes).toEqual([80000, 80000]); expect(speechFlags).toEqual([true, false]);
  expect(wavBlob(speech()).size).toBe(160044);
  expect(pcmFromBase64(btoa(String.fromCharCode(1, 0, 255, 255)))).toEqual(new Int16Array([1, -1]));
});

it("observes the Scribe callback without requesting another microphone and isolates failures", () => {
  const config = {}; const receive = vi.fn(); const stop = observeScribeMicrophone(config, receive);
  copyScribeAudio({}, "ignored"); copyScribeAudio(config, "pcm"); expect(receive).toHaveBeenCalledExactlyOnceWith("pcm");
  stop(); copyScribeAudio(config, "ignored"); expect(receive).toHaveBeenCalledTimes(1);
  observeScribeMicrophone(config, () => { throw new Error("pilot failure"); }); expect(() => copyScribeAudio(config, "pcm")).not.toThrow();
});

it("uses the provided live stream and releases only its audio branch", async () => {
  const tracks = [{ stop: vi.fn() }]; const stream = { getTracks: () => tracks } as unknown as MediaStream;
  const source = { connect: vi.fn(() => node), disconnect: vi.fn() };
  const gain = { gain: { value: 1 }, connect: vi.fn(), disconnect: vi.fn() };
  const node = { port: { postMessage: vi.fn(), close: vi.fn(), onmessage: null as ((event: MessageEvent) => void) | null }, connect: vi.fn(() => gain), disconnect: vi.fn() };
  const createSource = vi.fn(() => source); const close = vi.fn(async () => {});
  vi.stubGlobal("AudioContext", class { sampleRate = 48000; destination = {}; audioWorklet = { addModule: vi.fn(async () => {}) }; createMediaStreamSource = createSource; createGain = () => gain; resume = async () => {}; close = close; });
  vi.stubGlobal("AudioWorkletNode", class { constructor() { return node; } });
  const receive = vi.fn(); const controller = new AbortController();
  await teeMicrophone(stream, receive, controller.signal);
  expect(createSource).toHaveBeenCalledWith(stream);
  expect(node.port.postMessage).toHaveBeenCalledWith({ type: "configure", inputSampleRate: 48000, outputSampleRate: 16000 });
  node.port.onmessage?.(new MessageEvent("message", { data: { audioData: new Int16Array([7]).buffer } })); expect(receive).toHaveBeenCalledOnce();
  controller.abort(); expect(close).toHaveBeenCalled(); expect(tracks[0].stop).not.toHaveBeenCalled(); expect(node.port.onmessage).toBeNull(); vi.unstubAllGlobals();
});

it("a spoken introduction only proposes enrollment", () => {
  expect(suggestsVoiceEnrollment("Soy Juan Ramón")).toBe(true);
  expect(suggestsVoiceEnrollment("Revisa mis correos")).toBe(false);
});
