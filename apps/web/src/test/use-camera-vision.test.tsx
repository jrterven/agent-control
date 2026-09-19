import { act, cleanup, fireEvent, render, renderHook, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { VisionAnalysisInput, VisionAnalysisResult, VisionObservation } from "@hermes-control/shared-types";
import { CameraVision } from "../components/CameraVision";
import { useCameraVision } from "../hooks/useCameraVision";
import * as capture from "../lib/cameraCapture";
import { visionApi } from "../lib/vision";
import { usePwaUpdateStore } from "../lib/pwaUpdate";

vi.mock("../lib/cameraCapture", () => ({ acquireCamera: vi.fn(), cameraDevices: vi.fn(async () => [{ deviceId: "rear", label: "Rear" }]), cameraSupported: () => true, captureCameraFrame: vi.fn(), prepareCameraVideo: vi.fn(), stopCameraStream: (stream?: MediaStream) => stream?.getTracks().forEach((track) => track.stop()) }));
vi.mock("../lib/vision", () => ({ VISION_PREFERENCES_CHANGED: "vision-preferences-changed", visionApi: { preferences: vi.fn(), savePreferences: vi.fn(), observations: vi.fn(), intent: vi.fn(), analyze: vi.fn() } }));

const options = { ownerId: "owner-1", profileId: "agent-1", sessionId: "chat-1", enabled: true, csrfToken: "csrf", offline: false };
const prefs = { modelId: "gpt-5.6-luna" as const, intervalSeconds: 5 as const, configured: true };
const observation = (payload: VisionAnalysisInput, id = "seen"): VisionObservation => ({ id, sessionId: options.sessionId, activationId: payload.activationId, capturedAt: payload.capturedAt, createdAt: payload.capturedAt, modelId: prefs.modelId, mode: payload.mode, summary: "Una taza roja", meaningfulChange: true, sceneReset: false, uncertainties: [] });
const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; };
const tick = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
function media() {
  const track = Object.assign(new EventTarget(), { stop: vi.fn(), enabled: true, muted: false, readyState: "live" });
  return { track, stream: { getTracks: () => [track], getVideoTracks: () => [track] } as unknown as MediaStream };
}

describe("camera permission and analysis lifecycle", () => {
  let inputs: ReturnType<typeof media>[];
  let frameNumber: number;
  beforeEach(() => {
    vi.useFakeTimers(); vi.clearAllMocks(); inputs = []; frameNumber = 0;
    Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.mocked(visionApi.preferences).mockResolvedValue(prefs);
    vi.mocked(visionApi.observations).mockResolvedValue({ items: [], nextCursor: null });
    vi.mocked(visionApi.intent).mockResolvedValue({ intent: "nonvisual", question: "" });
    vi.mocked(visionApi.analyze).mockImplementation(async (_session, payload) => ({ observation: observation(payload, `seen-${frameNumber}`), published: true }));
    vi.mocked(capture.acquireCamera).mockImplementation(async () => { const input = media(); inputs.push(input); return input.stream; });
    vi.mocked(capture.prepareCameraVideo).mockImplementation(async (stream) => { const video = document.createElement("video"); video.srcObject = stream; return video; });
    vi.mocked(capture.captureCameraFrame).mockImplementation(async () => { frameNumber += 1; return { file: new File([`frame-${frameNumber}`], `frame-${frameNumber}.jpg`, { type: "image/jpeg" }), image: `data:image/jpeg;base64,frame${frameNumber}`, capturedAt: new Date().toISOString(), width: 1280, height: 720 }; });
  });
  afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

  it("opens on-demand setup by default and stays silent after the separate activation gesture", async () => {
    function Harness() { const camera = useCameraVision(options); return <CameraVision camera={camera} onLook={vi.fn()} onAttach={vi.fn()} />; }
    render(<Harness />); await tick();
    fireEvent.click(screen.getByRole("button", { name: "Cámara" }));
    expect(capture.acquireCamera).not.toHaveBeenCalled();
    expect(screen.getByText("Preguntar sobre la cámara")).toBeInTheDocument();
    expect(screen.queryByText("Elegir modo de cámara")).not.toBeInTheDocument();
    expect(screen.getByText("gpt-5.6-luna")).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Cámara del dispositivo" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Activar cámara" })); await tick();
    expect(capture.acquireCamera).toHaveBeenCalledOnce();
    await tick(60_000);
    expect(capture.captureCameraFrame).not.toHaveBeenCalled();
    expect(visionApi.intent).not.toHaveBeenCalled();
    expect(visionApi.analyze).not.toHaveBeenCalled();
  });

  it("requires choosing automatic observation and returns to on demand after stopping", async () => {
    function Harness() { const camera = useCameraVision(options); return <CameraVision camera={camera} onLook={vi.fn()} onAttach={vi.fn()} />; }
    render(<Harness />); await tick();
    fireEvent.click(screen.getByRole("button", { name: "Cámara" }));
    fireEvent.click(screen.getByRole("button", { name: "Cambiar modo" }));
    expect(screen.getByRole("button", { name: /Preguntar sobre la cámara/ })).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: /Seguimiento automático/ }));
    expect(visionApi.analyze).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Activar cámara" })); await tick();
    expect(visionApi.analyze).toHaveBeenCalledOnce();
    expect(vi.mocked(visionApi.analyze).mock.calls[0][1].mode).toBe("continuous");
    fireEvent.click(screen.getAllByRole("button", { name: "Apagar cámara" })[0]); await tick();
    fireEvent.click(screen.getByRole("button", { name: "Cámara" }));
    expect(screen.getByText("Preguntar sobre la cámara")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Activar cámara" })); await tick(60_000);
    expect(visionApi.analyze).toHaveBeenCalledOnce();
  });

  it("captures only for visual questions in on-demand mode, including after resume and switching devices", async () => {
    const onObservation = vi.fn();
    const { result } = renderHook(() => useCameraVision({ ...options, onObservation })); await tick();
    await act(async () => { await result.current.start("on_demand"); });
    await act(async () => { await result.current.onLiveRequest("Busca el informe", new AbortController().signal); });
    expect(capture.captureCameraFrame).not.toHaveBeenCalled();
    const question = "¿Qué dice la etiqueta de esta taza?";
    vi.mocked(visionApi.intent).mockResolvedValueOnce({ intent: "visual", question });
    await act(async () => { await result.current.onLiveRequest(question, new AbortController().signal); });
    expect(visionApi.analyze).toHaveBeenCalledOnce();
    expect(vi.mocked(visionApi.analyze).mock.calls[0][1]).toMatchObject({ mode: "on_demand", question });
    expect(onObservation).toHaveBeenCalledOnce();
    await tick(60_000);
    act(() => result.current.pause());
    await act(async () => { await result.current.resume(); await result.current.switchDevice("rear"); });
    await tick(60_000);
    expect(capture.captureCameraFrame).toHaveBeenCalledOnce();
    expect(visionApi.analyze).toHaveBeenCalledOnce();
    vi.mocked(visionApi.intent).mockResolvedValueOnce({ intent: "visual", question: "¿Y ahora?" });
    await act(async () => { await result.current.onLiveRequest("¿Y ahora?", new AbortController().signal); });
    expect(visionApi.analyze).toHaveBeenCalledTimes(2);
    expect(capture.captureCameraFrame).toHaveBeenCalledTimes(2);
  });

  it("never activates on mount; explicit activation blocks PWA reload and stop releases memory", async () => {
    const { result } = renderHook(() => useCameraVision(options)); await tick();
    expect(capture.acquireCamera).not.toHaveBeenCalled();
    await act(async () => { await result.current.start("on_demand"); });
    expect(result.current.phase).toBe("active"); expect(usePwaUpdateStore.getState().blockers.camera).toBe(true);
    await act(async () => { await result.current.analyze("¿Qué ves?"); });
    expect(result.current.latestFrame).not.toBeNull();
    act(() => result.current.stop());
    expect(inputs[0].track.stop).toHaveBeenCalledOnce(); expect(result.current.latestFrame).toBeNull();
    expect(result.current.phase).toBe("idle"); expect(usePwaUpdateStore.getState().blockers.camera).toBe(false);
  });

  it("stops a late permission grant after cancellation without activating or capturing", async () => {
    const permission = deferred<MediaStream>(); vi.mocked(capture.acquireCamera).mockReturnValueOnce(permission.promise);
    const { result } = renderHook(() => useCameraVision(options)); await tick();
    let pending!: Promise<void>; act(() => { pending = result.current.start("continuous"); });
    act(() => result.current.stop()); const late = media();
    await act(async () => { permission.resolve(late.stream); await pending; });
    expect(late.track.stop).toHaveBeenCalledOnce(); expect(result.current.phase).toBe("idle"); expect(visionApi.analyze).not.toHaveBeenCalled();
  });

  it("discards inference and captured bytes when the owner or conversation changes", async () => {
    const provider = deferred<VisionAnalysisResult>(); vi.mocked(visionApi.analyze).mockReturnValueOnce(provider.promise);
    const onObservation = vi.fn(); const { result, rerender } = renderHook((scope) => useCameraVision({ ...scope, onObservation }), { initialProps: options }); await tick();
    await act(async () => { await result.current.start("on_demand"); });
    let pending!: ReturnType<typeof result.current.analyze>; act(() => { pending = result.current.analyze(); }); await tick();
    const payload = vi.mocked(visionApi.analyze).mock.calls[0][1];
    rerender({ ...options, ownerId: "owner-2", sessionId: "chat-2" }); await tick();
    await act(async () => { provider.resolve({ observation: observation(payload), published: true }); await pending; });
    expect(onObservation).not.toHaveBeenCalled(); expect(result.current.latestFrame).toBeNull(); expect(result.current.observations).toEqual([]); expect(result.current.phase).toBe("idle");
  });

  it("waits for each result plus the interval, with one inference and no frame queue", async () => {
    const first = deferred<VisionAnalysisResult>(); vi.mocked(visionApi.analyze).mockReturnValueOnce(first.promise);
    const onObservation = vi.fn(); const { result } = renderHook(() => useCameraVision({ ...options, onObservation })); await tick();
    await act(async () => { await result.current.start("continuous"); }); await tick(60_000);
    expect(visionApi.analyze).toHaveBeenCalledTimes(1); expect(capture.captureCameraFrame).toHaveBeenCalledTimes(1);
    const payload = vi.mocked(visionApi.analyze).mock.calls[0][1]; expect(payload.mode).toBe("continuous"); expect(payload.previousImage).toBeUndefined();
    await act(async () => { first.resolve({ observation: observation(payload), published: false }); });
    expect(onObservation).not.toHaveBeenCalled(); await tick(4_999); expect(visionApi.analyze).toHaveBeenCalledTimes(1);
    await tick(1); expect(visionApi.analyze).toHaveBeenCalledTimes(2);
    expect(vi.mocked(visionApi.analyze).mock.calls[1][1].previousImage).toBe(payload.image);
    expect(onObservation).toHaveBeenCalledOnce();
    act(() => result.current.pause()); await tick(60_000); expect(visionApi.analyze).toHaveBeenCalledTimes(2); expect(result.current.latestFrame).toBeNull();
  });

  it("switches camera with a new activation and no comparison to the previous camera", async () => {
    const { result } = renderHook(() => useCameraVision(options)); await tick();
    await act(async () => { await result.current.start("continuous"); }); await tick();
    const original = vi.mocked(visionApi.analyze).mock.calls[0][1];
    await act(async () => { await result.current.switchDevice("rear"); }); await tick(5_000);
    const switched = vi.mocked(visionApi.analyze).mock.calls[1][1];
    expect(inputs[0].track.stop).toHaveBeenCalledOnce(); expect(capture.acquireCamera).toHaveBeenLastCalledWith(expect.any(AbortSignal), "rear");
    expect(switched.activationId).not.toBe(original.activationId); expect(switched.previousImage).toBeUndefined();
  });

  it("classifies without images in either mode and explicit visual requests publish as on demand", async () => {
    const { result } = renderHook(() => useCameraVision(options)); await tick();
    await act(async () => { await result.current.start("continuous"); }); await tick();
    await act(async () => { await result.current.onLiveRequest("Busca un informe", new AbortController().signal); });
    expect(capture.captureCameraFrame).toHaveBeenCalledTimes(1);
    expect(vi.mocked(visionApi.intent).mock.calls[0][1]).not.toHaveProperty("image");
    vi.mocked(visionApi.intent).mockResolvedValueOnce({ intent: "visual", question: "¿Qué tengo enfrente?" });
    let question!: ReturnType<typeof result.current.onLiveRequest>;
    act(() => { question = result.current.onLiveRequest("¿Y ahora?", new AbortController().signal); });
    await tick(1_999); expect(capture.captureCameraFrame).toHaveBeenCalledTimes(1);
    await tick(1);
    await act(async () => { expect((await question).observation?.summary).toBe("Una taza roja"); });
    expect(vi.mocked(visionApi.analyze).mock.calls[1][1]).toMatchObject({ mode: "on_demand", question: "¿Qué tengo enfrente?" });
  });

  it.each(["visual", "nonvisual"] as const)("waits for an existing continuous frame before one %s text classification", async (intent) => {
    const provider = deferred<VisionAnalysisResult>();
    vi.mocked(visionApi.analyze).mockReturnValueOnce(provider.promise);
    vi.mocked(visionApi.intent).mockResolvedValueOnce({ intent, question: intent === "visual" ? "¿Qué ves ahora?" : "" });
    const { result } = renderHook(() => useCameraVision(options)); await tick();
    await act(async () => { await result.current.start("continuous"); }); await tick();
    const firstFrame = vi.mocked(visionApi.analyze).mock.calls[0][1];
    let pending!: ReturnType<typeof result.current.onLiveRequest>;
    act(() => { pending = result.current.onLiveRequest("Mi nueva pregunta", new AbortController().signal); });
    await tick(10_000);
    expect(visionApi.intent).not.toHaveBeenCalled(); expect(capture.captureCameraFrame).toHaveBeenCalledTimes(1);
    await act(async () => { expect((await result.current.onLiveRequest("Otra pregunta", new AbortController().signal)).intent).toBe("unclear"); });
    await act(async () => { provider.resolve({ observation: observation(firstFrame), published: false }); });
    if (intent === "visual") {
      await tick(1_999); expect(capture.captureCameraFrame).toHaveBeenCalledTimes(1);
      await tick(1);
    }
    await act(async () => {
      const answer = await pending; expect(answer.intent).toBe(intent);
      if (intent === "visual") expect(answer.observation?.summary).toBe("Una taza roja");
    });
    expect(visionApi.intent).toHaveBeenCalledOnce();
    expect(vi.mocked(visionApi.intent).mock.calls[0][1].text).toBe("Mi nueva pregunta");
    expect(capture.captureCameraFrame).toHaveBeenCalledTimes(intent === "visual" ? 2 : 1);
    const count = vi.mocked(visionApi.analyze).mock.calls.length;
    await tick(5_000); expect(visionApi.analyze).toHaveBeenCalledTimes(count + 1);
  });

  it("cancels an explicit cooldown before capturing and retains owner spacing across stop", async () => {
    const { result } = renderHook(() => useCameraVision(options)); await tick();
    await act(async () => { await result.current.start("on_demand"); await result.current.analyze(); });
    act(() => result.current.stop());
    await act(async () => { await result.current.start("on_demand"); });
    let pending!: ReturnType<typeof result.current.analyze>;
    act(() => { pending = result.current.analyze("¿Y ahora?"); });
    await tick(1_999); expect(capture.captureCameraFrame).toHaveBeenCalledTimes(1);
    await act(async () => { result.current.stop(); expect(await pending).toBeNull(); });
    await tick(10_000); expect(capture.captureCameraFrame).toHaveBeenCalledTimes(1);
  });

  it("cancels waiting text when the in-flight camera is stopped without classifying or retrying", async () => {
    const provider = deferred<VisionAnalysisResult>(); vi.mocked(visionApi.analyze).mockReturnValueOnce(provider.promise);
    const { result } = renderHook(() => useCameraVision(options)); await tick();
    await act(async () => { await result.current.start("continuous"); }); await tick();
    const payload = vi.mocked(visionApi.analyze).mock.calls[0][1];
    let pending!: ReturnType<typeof result.current.onLiveRequest>;
    act(() => { pending = result.current.onLiveRequest("¿Qué ves?", new AbortController().signal); });
    await act(async () => { result.current.stop(); const answer = await pending; expect(answer.intent).toBe("visual"); expect(answer.observation).toBeUndefined(); });
    await act(async () => { provider.resolve({ observation: observation(payload), published: false }); });
    await tick(10_000); expect(visionApi.intent).not.toHaveBeenCalled(); expect(visionApi.analyze).toHaveBeenCalledOnce();
  });

  it("resumes continuous observation after a classifier outlasts its scheduled interval", async () => {
    const classifier = deferred<{ intent: "nonvisual"; question: string }>();
    vi.mocked(visionApi.intent).mockReturnValueOnce(classifier.promise);
    const { result } = renderHook(() => useCameraVision(options)); await tick();
    await act(async () => { await result.current.start("continuous"); }); await tick();
    expect(visionApi.analyze).toHaveBeenCalledTimes(1);
    let pending!: ReturnType<typeof result.current.onLiveRequest>;
    act(() => { pending = result.current.onLiveRequest("Busca el informe", new AbortController().signal); });
    await tick(10_000);
    expect(visionApi.analyze).toHaveBeenCalledTimes(1);
    await act(async () => { classifier.resolve({ intent: "nonvisual", question: "" }); await pending; });
    await tick(4_999); expect(visionApi.analyze).toHaveBeenCalledTimes(1);
    await tick(1); expect(visionApi.analyze).toHaveBeenCalledTimes(2);
    await tick(5_000); expect(visionApi.analyze).toHaveBeenCalledTimes(3);
  });

  it("returns unclear when the camera stops during classification", async () => {
    const classifier = deferred<{ intent: "visual"; question: string }>(); vi.mocked(visionApi.intent).mockReturnValueOnce(classifier.promise);
    const { result } = renderHook(() => useCameraVision(options)); await tick(); await act(async () => { await result.current.start("on_demand"); });
    let pending!: ReturnType<typeof result.current.onLiveRequest>; act(() => { pending = result.current.onLiveRequest("¿Qué ves?", new AbortController().signal); });
    act(() => result.current.stop());
    await act(async () => { classifier.resolve({ intent: "visual", question: "¿Qué ves?" }); expect((await pending).intent).toBe("unclear"); });
    expect(capture.captureCameraFrame).not.toHaveBeenCalled();
  });

  it("attaches exactly the analyzed File without taking another capture", async () => {
    const { result } = renderHook(() => useCameraVision(options)); await tick();
    await act(async () => { await result.current.start("on_demand"); await result.current.analyze(); });
    const exactFile = result.current.latestFrame!.file; const onAttach = vi.fn();
    const view = render(<CameraVision camera={result.current} onLook={vi.fn()} onAttach={onAttach} />);
    fireEvent.click(screen.getByRole("button", { name: "Adjuntar captura analizada" }));
    expect(onAttach).toHaveBeenCalledWith(exactFile); expect(capture.captureCameraFrame).toHaveBeenCalledTimes(1);
    view.rerender(<CameraVision camera={result.current} attachDisabled onLook={vi.fn()} onAttach={onAttach} />);
    expect(screen.getByRole("button", { name: "Adjuntar captura analizada" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Tomar y adjuntar nueva captura" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cámara" })).toBeEnabled();
  });

  it("pauses on provider failure, reports quota, and does not retry automatically", async () => {
    vi.mocked(visionApi.analyze).mockRejectedValueOnce(Object.assign(new Error("safe API message"), { code: "VISION_PROVIDER_LIMIT" }));
    const { result } = renderHook(() => useCameraVision(options)); await tick();
    await act(async () => { await result.current.start("continuous"); }); await tick(60_000);
    expect(result.current.phase).toBe("paused"); expect(result.current.error).toBe("quota"); expect(inputs[0].track.stop).toHaveBeenCalledOnce(); expect(visionApi.analyze).toHaveBeenCalledOnce();
  });

  it("clears camera on visibility, offline, key or preference changes without resuming", async () => {
    const { result } = renderHook(() => useCameraVision(options)); await tick();
    await act(async () => { await result.current.start("on_demand"); });
    act(() => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" }); document.dispatchEvent(new Event("visibilitychange")); });
    expect(result.current.phase).toBe("idle");
    act(() => { Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" }); document.dispatchEvent(new Event("visibilitychange")); });
    await tick(); expect(capture.acquireCamera).toHaveBeenCalledTimes(1);
    await act(async () => { await result.current.start("on_demand"); });
    act(() => window.dispatchEvent(new Event("vision-preferences-changed"))); await tick(); expect(result.current.phase).toBe("idle");
    await act(async () => { await result.current.start("on_demand"); });
    act(() => window.dispatchEvent(new Event("offline"))); expect(result.current.phase).toBe("idle");
  });
});
