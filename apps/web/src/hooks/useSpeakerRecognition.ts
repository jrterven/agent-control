import { useCallback, useEffect, useRef, useState } from "react";
import { useAppStore } from "../store/appStore";
import { RecognitionCapture, speakerChanged, teeMicrophone, type SpeakerState } from "../lib/speakerRecognition";

export function useSpeakerRecognition(mode: "live" | "dictation", sessionId?: string, onObservation?: (state: SpeakerState) => void) {
  const [state, setState] = useState<SpeakerState>({ phase: "idle" });
  const owner = useAppStore((s) => `${s.userId}:${s.authGeneration}:${s.authState}`);
  const capture = useRef<RecognitionCapture | null>(null);
  const branch = useRef<AbortController | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const options = useRef({ mode, sessionId, onObservation });
  const observed = useRef(false);
  options.current = { mode, sessionId, onObservation };
  const stop = useCallback(() => {
    branch.current?.abort(); branch.current = null; stream.current = null;
    capture.current?.stop(); capture.current = null;
    setState({ phase: "idle" });
    if (observed.current) { observed.current = false; options.current.onObservation?.({ phase: "idle" }); }
  }, []);
  const start = useCallback(() => {
    if (capture.current) return;
    const auth = useAppStore.getState();
    if (auth.authState !== "authenticated" || auth.demoMode) return;
    const instance = new RecognitionCapture({ mode: options.current.mode, sessionId: options.current.sessionId, onState: (next) => {
      if (capture.current !== instance) return;
      observed.current = next.phase !== "idle";
      setState(next); options.current.onObservation?.(next);
    } });
    capture.current = instance; void instance.begin();
  }, []);
  const pcm = useCallback((samples: Int16Array) => capture.current?.pcm(samples), []);
  const microphone = useCallback((next: MediaStream | null) => {
    if (next === stream.current) return;
    stop(); if (!next) return;
    stream.current = next; start();
    const controller = new AbortController(); branch.current = controller;
    void teeMicrophone(next, pcm, controller.signal).catch(() => {
      if (!controller.signal.aborted) { capture.current?.stop(); capture.current = null; setState({ phase: "unavailable" }); }
    });
  }, [pcm, start, stop]);
  useEffect(() => {
    const changed = () => stop();
    const hidden = () => { if (document.visibilityState === "hidden") stop(); };
    window.addEventListener(speakerChanged, changed);
    window.addEventListener("pagehide", changed);
    document.addEventListener("visibilitychange", hidden);
    let channel: BroadcastChannel | undefined;
    try { channel = new BroadcastChannel(speakerChanged); channel.onmessage = changed; } catch { /* Optional. */ }
    return () => {
      window.removeEventListener(speakerChanged, changed); window.removeEventListener("pagehide", changed);
      document.removeEventListener("visibilitychange", hidden); channel?.close(); stop();
    };
  }, [owner, sessionId, stop]);
  return { state, start, stop, pcm, microphone };
}
