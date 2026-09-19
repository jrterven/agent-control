import { useCallback, useEffect, useRef, useState } from "react";
import type { VisionCameraPhase, VisionIntentResult, VisionMode, VisionObservation, VisionPreferenceInput, VisionPreferences } from "@hermes-control/shared-types";
import { acquireCamera, cameraDevices, cameraSupported, captureCameraFrame, prepareCameraVideo, stopCameraStream, type CameraDevice, type CameraFrame } from "../lib/cameraCapture";
import { visionApi, VISION_PREFERENCES_CHANGED } from "../lib/vision";
import { usePwaUpdateStore } from "../lib/pwaUpdate";

export type CameraPhase = VisionCameraPhase;
export type CameraIssue = "unsupported" | "permission" | "unavailable" | "capture" | "analysis" | "busy" | "configuration" | "load" | "quota" | "access" | "timeout" | "stale";
export type CameraVisionOptions = {
  ownerId?: string; profileId: string; sessionId: string; enabled: boolean; offline?: boolean;
  csrfToken?: string; recentContext?: string; configurationVersion?: string | number | boolean;
  onObservation?: (observation: VisionObservation) => void;
};
const isAbort = (error: unknown) => error instanceof DOMException && error.name === "AbortError";
const issueFor = (error: unknown, fallback: CameraIssue): CameraIssue => {
  const code = typeof error === "object" && error !== null && "code" in error ? error.code : undefined;
  if (code === "VISION_PROVIDER_LIMIT") return "quota";
  if (code === "VISION_PROVIDER_ACCESS") return "access";
  if (code === "VISION_PROVIDER_TIMEOUT") return "timeout";
  if (code === "VISION_FRAME_STALE") return "stale";
  if (code === "VISION_BUSY") return "busy";
  if (code === "OPENAI_NOT_CONFIGURED" || code === "OPENAI_SECRET_UNAVAILABLE") return "configuration";
  return error instanceof DOMException && ["NotAllowedError", "SecurityError"].includes(error.name) ? "permission" : error instanceof DOMException && ["NotFoundError", "NotReadableError", "OverconstrainedError"].includes(error.name) ? "unavailable" : error instanceof Error && error.message === "unsupported" ? "unsupported" : fallback;
};
const mergeObservations = (existing: VisionObservation[], incoming: VisionObservation[]) => [...new Map([...incoming, ...existing].map((item) => [item.id, item])).values()].sort((a, b) => b.createdAt.localeCompare(a.createdAt));

export function useCameraVision(options: CameraVisionOptions) {
  const { ownerId, profileId, sessionId, enabled, offline = false, csrfToken, configurationVersion } = options;
  const scope = JSON.stringify([ownerId, profileId, sessionId, enabled, offline, configurationVersion]);
  const current = useRef(options); current.current = options;
  const scopeRef = useRef(scope); scopeRef.current = scope;
  const [phase, setPhase] = useState<CameraPhase>("idle");
  const [mode, setMode] = useState<VisionMode | null>(null);
  const [error, setError] = useState<CameraIssue | null>(null);
  const [preferences, setPreferences] = useState<VisionPreferences | null>(null);
  const [devices, setDevices] = useState<CameraDevice[]>([]);
  const [deviceId, setDeviceId] = useState("");
  const [analyzing, setAnalyzing] = useState(false);
  const [latestFrame, setLatestFrame] = useState<CameraFrame | null>(null);
  const [latestObservation, setLatestObservation] = useState<VisionObservation | null>(null);
  const [observations, setObservations] = useState<VisionObservation[]>([]);
  const [loadingObservations, setLoadingObservations] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const phaseRef = useRef<CameraPhase>("idle");
  const modeRef = useRef<VisionMode | null>(null);
  const preferencesRef = useRef<VisionPreferences | null>(null);
  const deviceRef = useRef("");
  const epoch = useRef(0);
  const activation = useRef("");
  const controller = useRef<AbortController | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const captureVideo = useRef<HTMLVideoElement | null>(null);
  const previewVideo = useRef<HTMLVideoElement | null>(null);
  const previous = useRef<CameraFrame | null>(null);
  const recent = useRef<VisionObservation | null>(null);
  const busy = useRef(false);
  const analysisCompletion = useRef<{ done: Promise<void>; finish: () => void } | null>(null);
  const pendingText = useRef<symbol | null>(null);
  const completionOwner = useRef(ownerId);
  const lastCompletedAt = useRef<number | null>(null);
  if (completionOwner.current !== ownerId) { completionOwner.current = ownerId; lastCompletedAt.current = null; }
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cursor = useRef<string | null>(null);
  const historyBusy = useRef(false);
  const metadataEpoch = useRef(0);
  const historyAbort = useRef<AbortController | null>(null);
  const preferenceAbort = useRef<AbortController | null>(null);
  const analyzeRef = useRef<(question?: string, signal?: AbortSignal, explicit?: boolean) => Promise<VisionObservation | null>>(async () => null);

  const videoRef = useCallback((video: HTMLVideoElement | null) => {
    if (previewVideo.current && previewVideo.current !== video) previewVideo.current.srcObject = null;
    previewVideo.current = video;
    if (video) { video.srcObject = stream.current; video.muted = true; video.playsInline = true; if (stream.current) void video.play().catch(() => undefined); }
  }, []);
  const release = useCallback(() => {
    epoch.current += 1;
    analysisCompletion.current?.finish(); analysisCompletion.current = null; pendingText.current = null;
    controller.current?.abort(); controller.current = null;
    if (timer.current) clearTimeout(timer.current); timer.current = null;
    stopCameraStream(stream.current); stream.current = null;
    if (captureVideo.current) { captureVideo.current.pause(); captureVideo.current.srcObject = null; captureVideo.current = null; }
    if (previewVideo.current) previewVideo.current.srcObject = null;
    previous.current = null; recent.current = null; activation.current = ""; busy.current = false;
    setLatestFrame(null); setLatestObservation(null); setAnalyzing(false);
  }, []);
  const stop = useCallback(() => {
    release(); phaseRef.current = "idle"; modeRef.current = null;
    setPhase("idle"); setMode(null); setError(null);
  }, [release]);
  const pause = useCallback(() => {
    if (phaseRef.current !== "active" && phaseRef.current !== "starting") return;
    release(); phaseRef.current = "paused"; setPhase("paused");
  }, [release]);
  const valid = (generation: number, capturedScope: string) => generation === epoch.current && capturedScope === scopeRef.current && !controller.current?.signal.aborted;

  const refreshPreferences = useCallback(async () => {
    const capturedScope = scopeRef.current;
    preferenceAbort.current?.abort();
    const request = new AbortController(); preferenceAbort.current = request;
    if (!current.current.enabled || current.current.offline || !current.current.ownerId) return;
    try {
      const result = await visionApi.preferences(request.signal);
      if (request.signal.aborted || capturedScope !== scopeRef.current) return;
      if (preferencesRef.current && (preferencesRef.current.modelId !== result.modelId || preferencesRef.current.configured !== result.configured)) stop();
      preferencesRef.current = result; setPreferences(result);
    } catch (problem) { if (!request.signal.aborted && capturedScope === scopeRef.current && !isAbort(problem)) setError("load"); }
  }, [stop]);
  const loadMore = useCallback(async () => {
    if (historyBusy.current || !current.current.enabled || current.current.offline || !current.current.ownerId) return;
    const capturedScope = scopeRef.current;
    const generation = metadataEpoch.current;
    const request = new AbortController(); historyAbort.current = request;
    historyBusy.current = true; setLoadingObservations(true);
    try {
      const result = await visionApi.observations(current.current.sessionId, cursor.current ?? undefined, request.signal);
      if (request.signal.aborted || capturedScope !== scopeRef.current || generation !== metadataEpoch.current) return;
      setObservations((existing) => mergeObservations(existing, result.items));
      cursor.current = result.nextCursor; setHasMore(Boolean(result.nextCursor));
    } catch (problem) { if (!request.signal.aborted && capturedScope === scopeRef.current && !isAbort(problem)) setError("load"); }
    finally { if (generation === metadataEpoch.current) { historyBusy.current = false; setLoadingObservations(false); } }
  }, []);
  const savePreferences = useCallback(async (value: VisionPreferenceInput) => {
    const capturedScope = scopeRef.current;
    stop();
    const saved = await visionApi.savePreferences(value, current.current.csrfToken);
    if (capturedScope !== scopeRef.current) return;
    preferencesRef.current = saved; setPreferences(saved);
    window.dispatchEvent(new Event(VISION_PREFERENCES_CHANGED));
  }, [stop]);

  const schedule = (generation: number, capturedScope: string) => {
    if (!valid(generation, capturedScope) || phaseRef.current !== "active" || modeRef.current !== "continuous" || pendingText.current) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => { timer.current = null; if (valid(generation, capturedScope) && phaseRef.current === "active" && modeRef.current === "continuous") void analyzeRef.current(undefined, undefined, false).catch(() => undefined); }, (preferencesRef.current?.intervalSeconds ?? 5) * 1000);
  };
  const analyze = useCallback(async (question?: string, externalSignal?: AbortSignal, explicit = true): Promise<VisionObservation | null> => {
    if (phaseRef.current !== "active" || !captureVideo.current || !controller.current || !modeRef.current || externalSignal?.aborted) return null;
    if (busy.current) { setError("busy"); return null; }
    const generation = epoch.current; const capturedScope = scopeRef.current;
    const request = new AbortController();
    const cancel = () => request.abort();
    const cameraSignal = controller.current.signal;
    cameraSignal.addEventListener("abort", cancel, { once: true }); externalSignal?.addEventListener("abort", cancel, { once: true });
    busy.current = true; setAnalyzing(true); setError(null);
    if (timer.current) clearTimeout(timer.current); timer.current = null;
    let finish = () => {};
    const completion = { done: new Promise<void>((resolve) => { finish = resolve; }), finish: () => finish() };
    analysisCompletion.current = completion;
    let succeeded = false;
    try {
      // Respect the server's completion-based spacing before taking a fresh photo.
      // The timestamp survives stop/switch for this owner; no image waits in a queue.
      const minimumGap = explicit ? 2_000 : (preferencesRef.current?.intervalSeconds ?? 5) * 1000;
      const remaining = lastCompletedAt.current === null ? 0 : minimumGap - (Date.now() - lastCompletedAt.current);
      if (remaining > 0) await new Promise<void>((resolve, reject) => {
        const clean = () => { clearTimeout(cooldown); request.signal.removeEventListener("abort", abort); };
        const abort = () => { clean(); reject(new DOMException("Camera operation cancelled", "AbortError")); };
        const cooldown = setTimeout(() => { clean(); resolve(); }, remaining);
        request.signal.addEventListener("abort", abort, { once: true });
        if (request.signal.aborted) abort();
      });
      if (!valid(generation, capturedScope) || request.signal.aborted || !captureVideo.current) return null;
      const frame = await captureCameraFrame(captureVideo.current, request.signal);
      if (!valid(generation, capturedScope) || request.signal.aborted) return null;
      const result = await visionApi.analyze(current.current.sessionId, {
        requestId: crypto.randomUUID(), activationId: activation.current, mode: explicit ? "on_demand" : modeRef.current,
        capturedAt: frame.capturedAt, image: frame.image,
        ...(current.current.recentContext ? { recentContext: current.current.recentContext.slice(-4000) } : {}),
        ...(previous.current ? { previousImage: previous.current.image } : {}), ...(question?.trim() ? { question: question.trim() } : {}),
      }, current.current.csrfToken, request.signal);
      if (!valid(generation, capturedScope) || request.signal.aborted) return null;
      lastCompletedAt.current = Date.now();
      // Keep exactly the bytes analyzed; taking an attachment must not take another photo.
      previous.current = frame; setLatestFrame(frame);
      recent.current = result.observation; setLatestObservation(result.observation);
      if (result.published) {
        setObservations((existing) => mergeObservations(existing, [result.observation]));
        current.current.onObservation?.(result.observation);
      }
      succeeded = true;
      return result.observation;
    } catch (problem) {
      if (valid(generation, capturedScope) && !request.signal.aborted && !isAbort(problem)) {
        // A failed inference is never retried by a timer. Resume requires a gesture.
        pause(); setError(issueFor(problem, "analysis"));
      }
      return null;
    } finally {
      cameraSignal.removeEventListener("abort", cancel); externalSignal?.removeEventListener("abort", cancel);
      if (valid(generation, capturedScope)) {
        busy.current = false; setAnalyzing(false);
        if (succeeded || request.signal.aborted) schedule(generation, capturedScope);
      }
      if (analysisCompletion.current === completion) analysisCompletion.current = null;
      completion.finish();
    }
  }, [pause]);
  analyzeRef.current = analyze;

  const start = useCallback(async (nextMode: VisionMode, selectedDevice = deviceRef.current) => {
    if (!current.current.enabled || current.current.offline || !current.current.ownerId || document.visibilityState === "hidden" || navigator.onLine === false) return;
    if (!preferencesRef.current?.configured) { setError("configuration"); return; }
    release();
    const generation = epoch.current; const capturedScope = scopeRef.current;
    const request = new AbortController(); controller.current = request;
    activation.current = crypto.randomUUID(); modeRef.current = nextMode; phaseRef.current = "starting";
    deviceRef.current = selectedDevice; setDeviceId(selectedDevice); setMode(nextMode); setPhase("starting"); setError(null);
    try {
      const acquired = await acquireCamera(request.signal, selectedDevice || undefined);
      if (!valid(generation, capturedScope) || request.signal.aborted) { stopCameraStream(acquired); return; }
      stream.current = acquired;
      acquired.getVideoTracks().forEach((track) => {
        track.addEventListener("ended", () => { if (valid(generation, capturedScope)) { stop(); setError("unavailable"); } }, { once: true });
        track.addEventListener("mute", () => { if (valid(generation, capturedScope)) { pause(); setError("unavailable"); } }, { once: true });
      });
      const video = await prepareCameraVideo(acquired, request.signal);
      if (!valid(generation, capturedScope) || request.signal.aborted) { video.srcObject = null; stopCameraStream(acquired); return; }
      captureVideo.current = video;
      if (previewVideo.current) { previewVideo.current.srcObject = acquired; void previewVideo.current.play().catch(() => undefined); }
      phaseRef.current = "active"; setPhase("active");
      void cameraDevices().then((items) => { if (valid(generation, capturedScope)) setDevices(items); }).catch(() => undefined);
      if (nextMode === "continuous") void analyzeRef.current(undefined, undefined, false).catch(() => undefined);
    } catch (problem) {
      if (!valid(generation, capturedScope) || request.signal.aborted || isAbort(problem)) return;
      stop(); setError(issueFor(problem, "unavailable"));
    }
  }, [release, stop, pause]);
  const resume = useCallback(() => modeRef.current ? start(modeRef.current) : Promise.resolve(), [start]);
  const switchDevice = useCallback((id: string) => {
    deviceRef.current = id; setDeviceId(id);
    return modeRef.current && phaseRef.current !== "idle" ? start(modeRef.current, id) : Promise.resolve();
  }, [start]);
  const takeNewCapture = useCallback(async (): Promise<CameraFrame | null> => {
    if (phaseRef.current !== "active" || !captureVideo.current || !controller.current || busy.current) return null;
    const generation = epoch.current; const capturedScope = scopeRef.current;
    busy.current = true; setAnalyzing(true);
    if (timer.current) clearTimeout(timer.current); timer.current = null;
    try {
      const frame = await captureCameraFrame(captureVideo.current, controller.current.signal);
      return valid(generation, capturedScope) ? frame : null;
    } catch (problem) { if (valid(generation, capturedScope) && !isAbort(problem)) setError("capture"); return null; }
    finally { if (valid(generation, capturedScope)) { busy.current = false; setAnalyzing(false); schedule(generation, capturedScope); } }
  }, []);
  const onLiveRequest = useCallback(async (context: string, signal: AbortSignal): Promise<VisionIntentResult & { observation?: VisionObservation }> => {
    const nonvisual: VisionIntentResult = { intent: "nonvisual", question: "" };
    const unavailable: VisionIntentResult = { intent: "visual", question: context };
    if (phaseRef.current !== "active" || !controller.current || signal.aborted) return nonvisual;
    if (pendingText.current || (busy.current && !analysisCompletion.current)) return { intent: "unclear", question: "" };
    const generation = epoch.current; const capturedScope = scopeRef.current;
    const token = Symbol("camera-text-request"); pendingText.current = token;
    const request = new AbortController(); const cameraSignal = controller.current.signal;
    const cancel = () => request.abort();
    cameraSignal.addEventListener("abort", cancel, { once: true }); signal.addEventListener("abort", cancel, { once: true });
    if (timer.current) clearTimeout(timer.current); timer.current = null;
    let ownsBusy = false;
    try {
      const inFlight = analysisCompletion.current;
      if (inFlight) {
        // Wait for the one existing frame; never queue captures or repeat its inference.
        await new Promise<void>((resolve) => {
          const settled = () => { request.signal.removeEventListener("abort", settled); resolve(); };
          request.signal.addEventListener("abort", settled, { once: true });
          void inFlight.done.then(settled);
          if (request.signal.aborted) settled();
        });
        if (!valid(generation, capturedScope) || request.signal.aborted || phaseRef.current !== "active") return unavailable;
      }
      if (timer.current) clearTimeout(timer.current); timer.current = null;
      if (busy.current) return { intent: "unclear", question: "" };
      ownsBusy = true; busy.current = true; setAnalyzing(true);
      const intent = await visionApi.intent(current.current.sessionId, { requestId: crypto.randomUUID(), text: context, ...(recent.current ? { recentContext: recent.current.summary } : {}) }, current.current.csrfToken, request.signal);
      if (!valid(generation, capturedScope) || request.signal.aborted) return { intent: "unclear", question: "" };
      ownsBusy = false; busy.current = false; setAnalyzing(false);
      if (intent.intent !== "visual") return intent;
      const observation = await analyzeRef.current(intent.question, signal);
      return { ...intent, ...(observation ? { observation } : {}) };
    } catch (problem) {
      if (valid(generation, capturedScope) && !request.signal.aborted && !isAbort(problem)) setError(issueFor(problem, "analysis"));
      return { intent: "unclear", question: "" };
    } finally {
      cameraSignal.removeEventListener("abort", cancel); signal.removeEventListener("abort", cancel);
      if (pendingText.current === token) pendingText.current = null;
      if (valid(generation, capturedScope)) {
        if (ownsBusy) { busy.current = false; setAnalyzing(false); }
        if (!busy.current) schedule(generation, capturedScope);
      }
    }
  }, []);

  useEffect(() => {
    stop(); metadataEpoch.current += 1;
    historyAbort.current?.abort(); preferenceAbort.current?.abort();
    preferencesRef.current = null; setPreferences(null); setObservations([]); cursor.current = null; historyBusy.current = false;
    setHasMore(false); setLoadingObservations(false); setDevices([]); deviceRef.current = ""; setDeviceId("");
    if (enabled && !offline && ownerId) { void refreshPreferences(); void loadMore(); const deviceScope = scopeRef.current; void cameraDevices().then((items) => { if (deviceScope === scopeRef.current) setDevices(items); }).catch(() => undefined); }
    return () => { release(); historyAbort.current?.abort(); preferenceAbort.current?.abort(); metadataEpoch.current += 1; };
  }, [scope, enabled, offline, ownerId, refreshPreferences, loadMore, release, stop]);
  useEffect(() => {
    const changed = () => { stop(); void refreshPreferences(); };
    const hidden = () => { if (document.visibilityState === "hidden") stop(); };
    window.addEventListener(VISION_PREFERENCES_CHANGED, changed);
    window.addEventListener("offline", stop); window.addEventListener("pagehide", stop);
    document.addEventListener("visibilitychange", hidden);
    return () => { window.removeEventListener(VISION_PREFERENCES_CHANGED, changed); window.removeEventListener("offline", stop); window.removeEventListener("pagehide", stop); document.removeEventListener("visibilitychange", hidden); };
  }, [refreshPreferences, stop]);

  useEffect(() => {
    usePwaUpdateStore.getState().setBlocker("camera", phase === "starting" || phase === "active");
    return () => usePwaUpdateStore.getState().setBlocker("camera", false);
  }, [phase]);

  return { videoRef, phase, mode, active: phase !== "idle", error, preferences, devices, deviceId, analyzing, latestFrame, latestObservation,
    observations, hasMore, loadingObservations, supported: cameraSupported(), start, stop, pause, resume, switchDevice, savePreferences,
    refreshPreferences, analyze, onLiveRequest, takeNewCapture, loadMore };
}
export type CameraVisionState = ReturnType<typeof useCameraVision>;
