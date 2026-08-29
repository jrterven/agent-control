import { useCallback, useEffect, useRef, useState } from "react";
import type { RealtimeConnection } from "../lib/elevenlabsScribeClient";
import { ApiError, api } from "../lib/api";

export type DictationIssue = "permissionDenied" | "quota" | "network" | "auth" | "unavailable" | "tooLong" | "unconfirmed" | "generic";
export type DictationPhase = "idle" | "connecting" | "listening" | "transcribing" | "stopping" | "error";

const MAX_PARTIAL_CHARACTERS = 2_000;
const MAX_COMMITTED_CHARACTERS = 8_000;
const SCRIBE_MODEL = "scribe_v2_realtime" as const;

function browserSupportsScribe() {
  if (typeof window === "undefined" || typeof navigator === "undefined") return false;
  return Boolean(
    window.isSecureContext
    && "mediaDevices" in navigator
    && typeof navigator.mediaDevices.getUserMedia === "function"
    && typeof window.AudioContext === "function"
    && typeof window.AudioWorkletNode === "function"
    && typeof window.WebSocket === "function",
  );
}

function classifyFailure(error: unknown): DictationIssue {
  if (error instanceof ApiError) {
    const code = error.code?.toUpperCase();
    if (code && [
      "INTEGRATION_NOT_CONFIGURED",
      "INTEGRATION_CREDENTIAL_REJECTED",
      "INTEGRATION_SECRET_UNAVAILABLE",
    ].includes(code)) return "auth";
    if (code && (
      code === "TRANSCRIPTION_QUOTA_EXCEEDED"
      || code === "PROVIDER_RATE_LIMITED"
      || code === "OWNER_RATE_LIMITED"
      || code.endsWith("_RATE_LIMITED")
    )) return "quota";
    if ([401, 403].includes(error.status)) return "auth";
    if ([402, 429].includes(error.status)) return "quota";
    if (error.status >= 500) return "network";
  }
  const name = error instanceof DOMException ? error.name : "";
  const message = error instanceof Error ? error.message.toLowerCase() : "";
  if (["NotAllowedError", "SecurityError", "PermissionDeniedError"].includes(name) || /permission|notallowed|denied/.test(message)) return "permissionDenied";
  if (/quota|rate.?limit|resource.?exhaust/.test(message)) return "quota";
  if (/auth|token|unauthor|forbidden/.test(message)) return "auth";
  if (/network|socket|offline|connection|fetch/.test(message)) return "network";
  return "generic";
}

function useBrowserOnline() {
  const [online, setOnline] = useState(() => typeof navigator === "undefined" || navigator.onLine);
  useEffect(() => {
    const markOnline = () => setOnline(true);
    const markOffline = () => setOnline(false);
    window.addEventListener("online", markOnline);
    window.addEventListener("offline", markOffline);
    return () => {
      window.removeEventListener("online", markOnline);
      window.removeEventListener("offline", markOffline);
    };
  }, []);
  return online;
}

export function useScribeDictation({
  enabled,
  sessionId,
  csrfToken,
  languageCode,
  onCommitted,
}: {
  enabled: boolean;
  sessionId?: string;
  csrfToken?: string;
  languageCode?: string;
  onCommitted: (text: string) => void;
}) {
  const online = useBrowserOnline();
  const supported = browserSupportsScribe();
  const available = enabled && online && supported;
  const [phase, setPhase] = useState<DictationPhase>("idle");
  const [partial, setPartial] = useState("");
  const [issue, setIssue] = useState<DictationIssue | null>(null);
  const connectionRef = useRef<RealtimeConnection | null>(null);
  const generationRef = useRef(0);
  const startingRef = useRef(false);
  const stoppingRef = useRef(false);
  const stopTimerRef = useRef<number | undefined>(undefined);
  const partialRef = useRef("");
  const onCommittedRef = useRef(onCommitted);
  onCommittedRef.current = onCommitted;

  const release = useCallback((updateState = true) => {
    generationRef.current += 1;
    startingRef.current = false;
    stoppingRef.current = false;
    window.clearTimeout(stopTimerRef.current);
    stopTimerRef.current = undefined;
    partialRef.current = "";
    const connection = connectionRef.current;
    connectionRef.current = null;
    connection?.close();
    if (updateState) {
      setPartial("");
      setIssue(null);
      setPhase("idle");
    }
  }, []);

  const fail = useCallback((nextIssue: DictationIssue) => {
    generationRef.current += 1;
    startingRef.current = false;
    stoppingRef.current = false;
    window.clearTimeout(stopTimerRef.current);
    stopTimerRef.current = undefined;
    partialRef.current = "";
    const connection = connectionRef.current;
    connectionRef.current = null;
    connection?.close();
    setPartial("");
    setIssue(nextIssue);
    setPhase("error");
  }, []);

  const start = useCallback(async () => {
    // This function is called only by the microphone button's user gesture.
    // Every attempt requests a fresh single-use token and is never retried.
    if (!available || startingRef.current || connectionRef.current) {
      if (!available) fail("unavailable");
      return;
    }
    const generation = generationRef.current + 1;
    generationRef.current = generation;
    startingRef.current = true;
    setIssue(null);
    setPartial("");
    setPhase("connecting");
    try {
      const [sdk, tokenView] = await Promise.all([
        import("../lib/elevenlabsScribeClient"),
        api.createTranscriptionToken({
          ...(sessionId ? { sessionId } : {}),
          ...(languageCode ? { languageCode } : {}),
        }, csrfToken),
      ]);
      if (generationRef.current !== generation || !startingRef.current) return;
      if (tokenView.modelId !== SCRIBE_MODEL) throw new Error("Unexpected transcription model");
      const connection = sdk.Scribe.connect({
        token: tokenView.token,
        modelId: SCRIBE_MODEL,
        commitStrategy: sdk.CommitStrategy.VAD,
        includeLanguageDetection: true,
        ...(languageCode ? { languageCode } : {}),
        microphone: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
          workletPaths: { scribeAudioProcessor: "/vendor/elevenlabs/scribeAudioProcessor.js" },
        },
      });
      if (generationRef.current !== generation) {
        connection.close();
        return;
      }
      connectionRef.current = connection;
      startingRef.current = false;
      const current = () => generationRef.current === generation && connectionRef.current === connection;
      connection.on(sdk.RealtimeEvents.SESSION_STARTED, () => {
        if (current()) setPhase("listening");
      });
      connection.on(sdk.RealtimeEvents.PARTIAL_TRANSCRIPT, (event) => {
        if (!current() || typeof event.text !== "string") return;
        if (event.text.length > MAX_PARTIAL_CHARACTERS) {
          fail("tooLong");
          return;
        }
        partialRef.current = event.text;
        setPartial(event.text);
        setPhase("transcribing");
      });
      connection.on(sdk.RealtimeEvents.COMMITTED_TRANSCRIPT, (event) => {
        if (!current() || typeof event.text !== "string") return;
        if (event.text.length > MAX_COMMITTED_CHARACTERS) {
          fail("tooLong");
          return;
        }
        partialRef.current = "";
        setPartial("");
        if (event.text.trim()) onCommittedRef.current(event.text);
        if (stoppingRef.current) {
          release(true);
        } else {
          setPhase("listening");
        }
      });
      const terminal = (nextIssue: DictationIssue) => {
        if (!current()) return;
        if (stoppingRef.current) {
          // PARTIAL_TRANSCRIPT is a hypothesis, not a durable transcript. If
          // the commit handshake cannot finish, disclose the loss instead of
          // silently turning provisional words into an editable draft.
          if (partialRef.current.trim()) fail("unconfirmed");
          else if (nextIssue === "network") release(true);
          else fail(nextIssue);
          return;
        }
        fail(nextIssue);
      };
      connection.on(sdk.RealtimeEvents.AUTH_ERROR, () => terminal("auth"));
      connection.on(sdk.RealtimeEvents.QUOTA_EXCEEDED, () => terminal("quota"));
      connection.on(sdk.RealtimeEvents.RATE_LIMITED, () => terminal("quota"));
      connection.on(sdk.RealtimeEvents.RESOURCE_EXHAUSTED, () => terminal("quota"));
      connection.on(sdk.RealtimeEvents.ERROR, (event) => terminal(classifyFailure(new Error(event.error))));
      connection.on(sdk.RealtimeEvents.CLOSE, () => {
        // A provider-side close may race the explicit stop/commit handshake.
        // Report that final segment as unconfirmed; never promote a partial
        // hypothesis into the user's editable draft.
        terminal("network");
      });
    } catch (error) {
      if (generationRef.current === generation) fail(classifyFailure(error));
    } finally {
      if (generationRef.current === generation) startingRef.current = false;
    }
  }, [available, csrfToken, fail, languageCode, sessionId]);

  const stop = useCallback(() => {
    setIssue(null);
    const connection = connectionRef.current;
    if (!connection) {
      release(true);
      return;
    }
    stoppingRef.current = true;
    setPhase("stopping");
    try { connection.mute(); } catch { /* The mic may still be acquiring; close below still releases it. */ }
    try { connection.commit(); } catch { /* The bounded timeout below reports an unconfirmed final segment. */ }
    stopTimerRef.current = window.setTimeout(() => {
      if (!stoppingRef.current || connectionRef.current !== connection) return;
      if (partialRef.current.trim()) fail("unconfirmed");
      else release(true);
    }, 1_500);
  }, [fail, release]);

  useEffect(() => {
    // Session/profile changes and auth/offline transitions cannot carry an
    // active microphone or a single-use socket into the next context.
    release(true);
    return () => release(false);
  }, [available, release, sessionId]);

  useEffect(() => {
    const endPageSession = () => release(true);
    const onVisibilityChange = () => { if (document.visibilityState === "hidden") release(true); };
    window.addEventListener("pagehide", endPageSession);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      window.removeEventListener("pagehide", endPageSession);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [release]);

  return {
    available,
    phase,
    partial,
    issue,
    active: phase === "connecting" || phase === "listening" || phase === "transcribing" || phase === "stopping",
    start,
    stop,
  };
}
