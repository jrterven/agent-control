import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ChatMessage } from "../types";
import { api, type SpeechTokenView } from "../lib/api";
import { loadPreference, savePreference } from "../lib/db";
import { usePwaUpdateStore } from "../lib/pwaUpdate";

const AUTO_READ_PREFERENCE = "speech:auto-read";
const AUDIO_TYPE = "audio/mpeg";

function abortError() {
  return new DOMException("Audio playback was stopped", "AbortError");
}

function isPlaybackPermissionError(error: unknown) {
  return error instanceof DOMException && error.name === "NotAllowedError";
}

function waitForMediaSourceOpen(mediaSource: MediaSource, signal: AbortSignal) {
  if (signal.aborted) return Promise.reject(abortError());
  if (mediaSource.readyState === "open") return Promise.resolve();
  return new Promise<void>((resolve, reject) => {
    const cleanup = () => {
      mediaSource.removeEventListener("sourceopen", opened);
      mediaSource.removeEventListener("sourceclose", closed);
      signal.removeEventListener("abort", aborted);
    };
    const opened = () => { cleanup(); resolve(); };
    const closed = () => { cleanup(); reject(new Error("Media source closed before opening")); };
    const aborted = () => { cleanup(); reject(abortError()); };
    mediaSource.addEventListener("sourceopen", opened, { once: true });
    mediaSource.addEventListener("sourceclose", closed, { once: true });
    signal.addEventListener("abort", aborted, { once: true });
  });
}

function appendSourceBuffer(sourceBuffer: SourceBuffer, bytes: ArrayBuffer, signal: AbortSignal) {
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise<void>((resolve, reject) => {
    const cleanup = () => {
      sourceBuffer.removeEventListener("updateend", updated);
      sourceBuffer.removeEventListener("error", failed);
      signal.removeEventListener("abort", aborted);
    };
    const updated = () => { cleanup(); resolve(); };
    const failed = () => { cleanup(); reject(new Error("The browser rejected the audio buffer")); };
    const aborted = () => { cleanup(); reject(abortError()); };
    sourceBuffer.addEventListener("updateend", updated, { once: true });
    sourceBuffer.addEventListener("error", failed, { once: true });
    signal.addEventListener("abort", aborted, { once: true });
    try {
      sourceBuffer.appendBuffer(bytes);
    } catch (error) {
      cleanup();
      reject(error);
    }
  });
}

async function loadHistoryMediaSource(
  mediaSource: MediaSource,
  response: Response,
  signal: AbortSignal,
) {
  await waitForMediaSourceOpen(mediaSource, signal);
  if (signal.aborted) throw abortError();
  const sourceBuffer = mediaSource.addSourceBuffer(AUDIO_TYPE);
  sourceBuffer.mode = "sequence";
  const reader = response.body?.getReader();
  if (!reader) {
    const bytes = await response.arrayBuffer();
    if (bytes.byteLength) await appendSourceBuffer(sourceBuffer, bytes, signal);
  } else {
    const cancelReader = () => { void reader.cancel().catch(() => undefined); };
    signal.addEventListener("abort", cancelReader, { once: true });
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (signal.aborted) throw abortError();
        if (done) break;
        if (value.byteLength) {
          await appendSourceBuffer(sourceBuffer, value.slice().buffer as ArrayBuffer, signal);
        }
      }
    } catch (error) {
      await reader.cancel().catch(() => undefined);
      throw error;
    } finally {
      signal.removeEventListener("abort", cancelReader);
      reader.releaseLock();
    }
  }
  if (mediaSource.readyState === "open") mediaSource.endOfStream();
}

export type SpeechPlaybackStatus = "idle" | "loading" | "playing" | "paused" | "error";
export type LiveSpeechStatus = "idle" | "connecting" | "speaking" | "error";

export function textForSpeech(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, " ")
    .replace(/```[\s\S]*$/g, " ")
    .replace(/MEDIA:\[[^\]]*\]\([^)]*\)/gi, " ")
    .replace(/MEDIA:\[[\s\S]*$/gi, " ")
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, "$1")
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/https?:\/\/\S+/g, " ")
    .replace(/[`#*_~>|]/g, " ")
    .replace(/^\s*[-+]\s+/gm, "")
    .replace(/\s+/g, " ")
    .trim();
}

function base64Bytes(value: string): Uint8Array {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

class LiveAudioSink {
  readonly audio = new Audio();
  private mediaSource?: MediaSource;
  private sourceBuffer?: SourceBuffer;
  private objectUrl?: string;
  private queue: Uint8Array[] = [];
  private fallback: Uint8Array[] = [];
  private fallbackMode = true;
  private final = false;
  private stopped = false;

  constructor(private readonly onStatus: (status: LiveSpeechStatus) => void) {
    this.audio.preload = "auto";
    this.audio.addEventListener("playing", () => this.onStatus("speaking"));
    this.audio.addEventListener("ended", () => this.onStatus("idle"));
    this.audio.addEventListener("error", () => { if (!this.stopped) this.onStatus("error"); });
    if (typeof MediaSource !== "undefined" && MediaSource.isTypeSupported(AUDIO_TYPE)) {
      this.fallbackMode = false;
      this.mediaSource = new MediaSource();
      this.objectUrl = URL.createObjectURL(this.mediaSource);
      this.audio.src = this.objectUrl;
      this.mediaSource.addEventListener("sourceopen", () => {
        if (!this.mediaSource || this.stopped) return;
        try {
          this.sourceBuffer = this.mediaSource.addSourceBuffer(AUDIO_TYPE);
          this.sourceBuffer.mode = "sequence";
          this.sourceBuffer.addEventListener("updateend", () => this.flush());
          this.fallback = [];
          this.flush();
        } catch {
          this.sourceBuffer = undefined;
          this.fallbackMode = true;
          if (this.final) this.playFallback();
        }
      }, { once: true });
    }
  }

  append(bytes: Uint8Array) {
    if (this.stopped || !bytes.length) return;
    if (this.fallbackMode || !this.sourceBuffer) this.fallback.push(bytes);
    this.queue.push(bytes);
    this.flush();
  }

  finish() {
    if (this.stopped) return;
    this.final = true;
    if (this.fallbackMode) {
      this.playFallback();
      return;
    }
    this.flush();
  }

  private playFallback() {
    const blob = new Blob(
      this.fallback.map((chunk) => chunk.slice().buffer as ArrayBuffer),
      { type: AUDIO_TYPE },
    );
    if (!blob.size) {
      this.onStatus("idle");
      return;
    }
    if (this.objectUrl) URL.revokeObjectURL(this.objectUrl);
    this.objectUrl = URL.createObjectURL(blob);
    this.audio.src = this.objectUrl;
    void this.audio.play().catch(() => this.onStatus("error"));
  }

  private flush() {
    if (this.stopped || !this.sourceBuffer || this.sourceBuffer.updating) return;
    const next = this.queue.shift();
    if (next) {
      try {
        this.sourceBuffer.appendBuffer(next.slice().buffer);
        if (this.audio.paused) void this.audio.play().catch(() => this.onStatus("error"));
      } catch {
        this.onStatus("error");
      }
      return;
    }
    if (this.final && this.mediaSource?.readyState === "open") {
      try { this.mediaSource.endOfStream(); } catch { /* already closed */ }
    }
  }

  stop() {
    this.stopped = true;
    this.audio.pause();
    this.audio.removeAttribute("src");
    this.audio.load();
    this.queue = [];
    this.fallback = [];
    if (this.objectUrl) URL.revokeObjectURL(this.objectUrl);
    this.objectUrl = undefined;
    this.onStatus("idle");
  }
}

class LiveTtsController {
  private socket?: WebSocket;
  private sink: LiveAudioSink;
  private pending = "";
  private finishRequested = false;
  private stopped = false;

  constructor(
    private readonly token: SpeechTokenView,
    onStatus: (status: LiveSpeechStatus) => void,
  ) {
    this.sink = new LiveAudioSink(onStatus);
    const url = new URL(`wss://api.elevenlabs.io/v1/text-to-speech/${encodeURIComponent(token.voiceId)}/stream-input`);
    url.searchParams.set("single_use_token", token.token);
    url.searchParams.set("model_id", token.modelId);
    url.searchParams.set("output_format", "mp3_44100_128");
    url.searchParams.set("inactivity_timeout", "60");
    this.socket = new WebSocket(url);
    this.socket.addEventListener("open", () => {
      if (this.stopped || !this.socket) return;
      this.socket.send(JSON.stringify({
        text: " ",
        voice_settings: { stability: 0.5, similarity_boost: 0.8, speed: 1 },
      }));
      this.flush(true);
      if (this.finishRequested) this.closeInput();
    });
    this.socket.addEventListener("message", (event) => {
      if (this.stopped || typeof event.data !== "string") return;
      try {
        const payload = JSON.parse(event.data) as { audio?: unknown; is_final?: unknown };
        if (typeof payload.audio === "string" && payload.audio) this.sink.append(base64Bytes(payload.audio));
        if (payload.is_final === true) this.sink.finish();
      } catch {
        // Unknown provider events are ignored; the socket remains usable.
      }
    });
    this.socket.addEventListener("error", () => { if (!this.stopped) onStatus("error"); });
    this.socket.addEventListener("close", () => {
      if (!this.stopped && this.finishRequested) this.sink.finish();
    });
  }

  feed(delta: string) {
    if (this.stopped || !delta) return;
    this.pending += delta;
    this.flush(false);
  }

  private flush(force: boolean) {
    if (this.socket?.readyState !== WebSocket.OPEN || !this.pending) return;
    let boundary = this.pending.length;
    if (!force && this.pending.length < 80 && !/[.!?;:\n]\s*$/.test(this.pending)) return;
    if (!force) {
      const whitespace = Math.max(this.pending.lastIndexOf(" "), this.pending.lastIndexOf("\n"));
      if (whitespace > 0) boundary = whitespace + 1;
    }
    const raw = this.pending.slice(0, boundary);
    this.pending = this.pending.slice(boundary);
    const text = textForSpeech(raw);
    if (text) this.socket.send(JSON.stringify({ text: `${text} ` }));
  }

  finish() {
    if (this.stopped || this.finishRequested) return;
    this.finishRequested = true;
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.flush(true);
      this.closeInput();
    }
  }

  private closeInput() {
    if (this.socket?.readyState === WebSocket.OPEN) this.socket.send(JSON.stringify({ text: "" }));
  }

  stop() {
    this.stopped = true;
    this.socket?.close(1000, "stopped");
    this.socket = undefined;
    this.sink.stop();
  }
}

type LiveRef = {
  messageId: string;
  processedText: string;
  controller?: LiveTtsController;
  pending: string;
  finishRequested: boolean;
};

export function useSpeechPlayback({
  available,
  sessionId,
  historySessionId,
  csrfToken,
  streamingMessage,
}: {
  available: boolean;
  sessionId: string;
  historySessionId?: string;
  csrfToken?: string;
  streamingMessage?: ChatMessage;
}) {
  const [liveEnabled, setLiveEnabledState] = useState(false);
  const [liveStatus, setLiveStatus] = useState<LiveSpeechStatus>("idle");
  const [status, setStatus] = useState<SpeechPlaybackStatus>("idle");
  const [activeMessageId, setActiveMessageId] = useState<string>();
  const [rate, setRateState] = useState(1);
  const [error, setError] = useState(false);
  const historyAudioRef = useRef<HTMLAudioElement | undefined>(undefined);
  const historyUrlRef = useRef<string | undefined>(undefined);
  const historyAbortRef = useRef<AbortController | undefined>(undefined);
  const liveRef = useRef<LiveRef | undefined>(undefined);
  const setUpdateBlocker = usePwaUpdateStore((state) => state.setBlocker);

  const stopHistory = useCallback(() => {
    historyAbortRef.current?.abort();
    historyAbortRef.current = undefined;
    const audio = historyAudioRef.current;
    if (audio) {
      audio.pause();
      audio.currentTime = 0;
      audio.removeAttribute("src");
      audio.load();
    }
    if (historyUrlRef.current) URL.revokeObjectURL(historyUrlRef.current);
    historyUrlRef.current = undefined;
    setActiveMessageId(undefined);
    setStatus("idle");
    setError(false);
  }, []);

  const stopLive = useCallback(() => {
    liveRef.current?.controller?.stop();
    liveRef.current = undefined;
    setLiveStatus("idle");
  }, []);

  useEffect(() => {
    let active = true;
    void loadPreference(AUTO_READ_PREFERENCE).then((value) => {
      if (active) setLiveEnabledState(value === "true");
    }).catch(() => undefined);
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!available || !liveEnabled) {
      stopLive();
      return;
    }
    if (streamingMessage) {
      let current = liveRef.current;
      if (current?.messageId !== streamingMessage.id) {
        stopLive();
        stopHistory();
        current = {
          messageId: streamingMessage.id,
          processedText: "",
          pending: "",
          finishRequested: false,
        };
        liveRef.current = current;
        setLiveStatus("connecting");
        void api.createSpeechToken({ sessionId }, csrfToken).then((token) => {
          const active = liveRef.current;
          if (!active || active.messageId !== streamingMessage.id) return;
          active.controller = new LiveTtsController(token, setLiveStatus);
          if (active.pending) active.controller.feed(active.pending);
          if (active.finishRequested) active.controller.finish();
        }).catch(() => {
          if (liveRef.current?.messageId === streamingMessage.id) setLiveStatus("error");
        });
      }
      const normalized = textForSpeech(streamingMessage.content);
      if (normalized.length > current.processedText.length && normalized.startsWith(current.processedText)) {
        const delta = normalized.slice(current.processedText.length);
        current.processedText = normalized;
        if (current.controller) current.controller.feed(delta);
        else current.pending += delta;
      } else if (normalized !== current.processedText) {
        // Markdown constructs can become fully known only after later deltas.
        // Advance the stable projection without replaying already queued text.
        current.processedText = normalized;
      }
      return;
    }
    const current = liveRef.current;
    if (current && !current.finishRequested) {
      current.finishRequested = true;
      if (current.controller) current.controller.finish();
    }
  }, [available, csrfToken, liveEnabled, sessionId, stopHistory, stopLive, streamingMessage]);

  useEffect(() => () => {
    stopHistory();
    stopLive();
  }, [sessionId, stopHistory, stopLive]);

  useEffect(() => {
    setUpdateBlocker("speech", status === "loading" || status === "playing" || status === "paused" || liveStatus === "connecting" || liveStatus === "speaking");
    return () => setUpdateBlocker("speech", false);
  }, [liveStatus, setUpdateBlocker, status]);

  const setLiveEnabled = useCallback((enabled: boolean) => {
    setLiveEnabledState(enabled);
    void savePreference(AUTO_READ_PREFERENCE, String(enabled)).catch(() => undefined);
    if (!enabled) stopLive();
  }, [stopLive]);

  const speak = useCallback(async (message: ChatMessage) => {
    const text = textForSpeech(message.content);
    if (!available || !text) return;
    stopLive();
    stopHistory();
    setActiveMessageId(message.id);
    setStatus("loading");
    setError(false);
    const abort = new AbortController();
    historyAbortRef.current = abort;
    try {
      const supportsMediaSource = typeof MediaSource !== "undefined"
        && MediaSource.isTypeSupported(AUDIO_TYPE);
      const mediaSource = supportsMediaSource ? new MediaSource() : undefined;
      const audio = new Audio();
      audio.preload = "auto";
      audio.playbackRate = rate;
      audio.addEventListener("playing", () => setStatus("playing"));
      audio.addEventListener("pause", () => { if (!audio.ended && audio.currentTime > 0) setStatus("paused"); });
      audio.addEventListener("ended", () => setStatus("idle"));
      audio.addEventListener("error", () => { setStatus("error"); setError(true); });
      historyAudioRef.current = audio;

      let initialPlay: Promise<unknown> | undefined;
      if (mediaSource) {
        // Android preserves the user activation at the moment ``play`` is
        // called, not after the POST and MP3 download finish. Attach an empty
        // MediaSource and start playback synchronously from the speaker tap;
        // the generated bytes can then arrive without being treated as
        // forbidden autoplay by the installed PWA.
        const objectUrl = URL.createObjectURL(mediaSource);
        historyUrlRef.current = objectUrl;
        audio.src = objectUrl;
        initialPlay = audio.play().then(() => undefined, (failure: unknown) => failure);
      }

      const response = await api.streamSpeech(text, historySessionId, csrfToken, abort.signal);
      if (mediaSource) {
        await loadHistoryMediaSource(mediaSource, response, abort.signal);
        const playbackFailure = await initialPlay;
        if (playbackFailure) {
          if (isPlaybackPermissionError(playbackFailure)) {
            // The audio is ready. Keep the player usable so the explicit play
            // control can satisfy stricter or user-configured autoplay rules.
            setStatus("paused");
            setError(false);
            return;
          }
          throw playbackFailure;
        }
        return;
      }

      const blob = await response.blob();
      if (abort.signal.aborted) return;
      const objectUrl = URL.createObjectURL(blob);
      historyUrlRef.current = objectUrl;
      audio.src = objectUrl;
      try {
        await audio.play();
      } catch (failure) {
        if (isPlaybackPermissionError(failure)) {
          setStatus("paused");
          setError(false);
          return;
        }
        throw failure;
      }
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === "AbortError") return;
      setStatus("error");
      setError(true);
    }
  }, [available, csrfToken, historySessionId, rate, stopHistory, stopLive]);

  const togglePause = useCallback(() => {
    const audio = historyAudioRef.current;
    if (!audio) return;
    if (audio.paused) void audio.play().catch(() => { setStatus("error"); setError(true); });
    else audio.pause();
  }, []);

  const setRate = useCallback((next: number) => {
    setRateState(next);
    if (historyAudioRef.current) historyAudioRef.current.playbackRate = next;
  }, []);

  return useMemo(() => ({
    liveEnabled,
    liveStatus,
    setLiveEnabled,
    status,
    activeMessageId,
    rate,
    error,
    speak,
    togglePause,
    stop: stopHistory,
    setRate,
  }), [activeMessageId, error, liveEnabled, liveStatus, rate, setLiveEnabled, setRate, speak, status, stopHistory, togglePause]);
}
