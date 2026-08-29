import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { insertTranscriptAtSelection } from "../components/ChatView";
import { useScribeDictation } from "../hooks/useScribeDictation";
import { ApiError, api } from "../lib/api";

const scribeMock = vi.hoisted(() => {
  const handlers = new Map<string, Set<(payload?: never) => void>>();
  const connection = {
    on: vi.fn((event: string, handler: (payload?: never) => void) => {
      const listeners = handlers.get(event) ?? new Set();
      listeners.add(handler);
      handlers.set(event, listeners);
    }),
    close: vi.fn(),
    mute: vi.fn(),
    commit: vi.fn(),
  };
  const connect = vi.fn(() => connection);
  const emit = (event: string, payload?: unknown) => {
    for (const handler of handlers.get(event) ?? []) handler(payload as never);
  };
  const reset = () => {
    handlers.clear();
    connect.mockClear();
    connection.on.mockClear();
    connection.close.mockClear();
    connection.mute.mockClear();
    connection.commit.mockClear();
  };
  return { connection, connect, emit, reset };
});

vi.mock("../lib/elevenlabsScribeClient", () => ({
  Scribe: { connect: scribeMock.connect },
  CommitStrategy: { VAD: "vad" },
  RealtimeEvents: {
    SESSION_STARTED: "session_started",
    PARTIAL_TRANSCRIPT: "partial_transcript",
    COMMITTED_TRANSCRIPT: "committed_transcript",
    AUTH_ERROR: "auth_error",
    QUOTA_EXCEEDED: "quota_exceeded",
    RATE_LIMITED: "rate_limited",
    RESOURCE_EXHAUSTED: "resource_exhausted",
    ERROR: "error",
    CLOSE: "close",
  },
}));

function enableBrowserAudio() {
  Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
  Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn() } });
  Object.defineProperty(window, "AudioContext", { configurable: true, value: class AudioContext {} });
  Object.defineProperty(window, "AudioWorkletNode", { configurable: true, value: class AudioWorkletNode {} });
  Object.defineProperty(window, "WebSocket", { configurable: true, value: class WebSocket {} });
}

describe("Scribe realtime dictation", () => {
  beforeEach(() => {
    enableBrowserAudio();
    scribeMock.reset();
    vi.spyOn(api, "createTranscriptionToken").mockResolvedValue({ token: "single-use-token", expiresAt: "2026-08-29T12:15:00Z", modelId: "scribe_v2_realtime" });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("loads the official SDK only from a microphone gesture and inserts only committed text", async () => {
    const committed = vi.fn();
    const { result } = renderHook(() => useScribeDictation({ enabled: true, sessionId: "session-a", csrfToken: "csrf-memory", onCommitted: committed }));

    expect(api.createTranscriptionToken).not.toHaveBeenCalled();
    await act(async () => { await result.current.start(); });

    expect(api.createTranscriptionToken).toHaveBeenCalledWith({ sessionId: "session-a" }, "csrf-memory");
    expect(scribeMock.connect).toHaveBeenCalledWith(expect.objectContaining({
      token: "single-use-token",
      modelId: "scribe_v2_realtime",
      commitStrategy: "vad",
      microphone: expect.objectContaining({
        workletPaths: { scribeAudioProcessor: "/vendor/elevenlabs/scribeAudioProcessor.js" },
      }),
    }));

    act(() => scribeMock.emit("session_started"));
    act(() => scribeMock.emit("partial_transcript", { text: "mensaje prov" }));
    expect(result.current.partial).toBe("mensaje prov");
    expect(committed).not.toHaveBeenCalled();

    act(() => scribeMock.emit("committed_transcript", { text: "mensaje definitivo" }));
    expect(committed).toHaveBeenCalledWith("mensaje definitivo");
    expect(result.current.partial).toBe("");
  });

  it("commits before closing and reports an unconfirmed provisional segment on timeout", async () => {
    vi.useFakeTimers();
    const committed = vi.fn();
    const { result } = renderHook(() => useScribeDictation({ enabled: true, sessionId: "session-a", csrfToken: "csrf-memory", onCommitted: committed }));
    await act(async () => { await result.current.start(); });
    act(() => scribeMock.emit("session_started"));
    act(() => scribeMock.emit("partial_transcript", { text: "últimas palabras" }));

    act(() => result.current.stop());
    expect(scribeMock.connection.mute).toHaveBeenCalledTimes(1);
    expect(scribeMock.connection.commit).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("stopping");
    expect(committed).not.toHaveBeenCalled();

    act(() => vi.advanceTimersByTime(1_501));
    expect(committed).not.toHaveBeenCalled();
    expect(scribeMock.connection.close).toHaveBeenCalled();
    expect(result.current.phase).toBe("error");
    expect(result.current.issue).toBe("unconfirmed");
  });

  it("does not promote provisional words if the provider closes while a stop commit is pending", async () => {
    vi.useFakeTimers();
    const committed = vi.fn();
    const { result } = renderHook(() => useScribeDictation({ enabled: true, sessionId: "session-a", csrfToken: "csrf-memory", onCommitted: committed }));
    await act(async () => { await result.current.start(); });
    act(() => scribeMock.emit("session_started"));
    act(() => scribeMock.emit("partial_transcript", { text: "cierre con palabras" }));

    act(() => result.current.stop());
    act(() => scribeMock.emit("close"));

    expect(committed).not.toHaveBeenCalled();
    expect(result.current.phase).toBe("error");
    expect(result.current.issue).toBe("unconfirmed");
    act(() => vi.advanceTimersByTime(1_600));
    expect(committed).not.toHaveBeenCalled();
  });

  it("times out quietly without a false unconfirmed warning when no partial text existed", async () => {
    vi.useFakeTimers();
    const committed = vi.fn();
    const { result } = renderHook(() => useScribeDictation({ enabled: true, sessionId: "session-a", csrfToken: "csrf-memory", onCommitted: committed }));
    await act(async () => { await result.current.start(); });
    act(() => scribeMock.emit("session_started"));
    act(() => result.current.stop());
    act(() => vi.advanceTimersByTime(1_501));

    expect(committed).not.toHaveBeenCalled();
    expect(result.current.phase).toBe("idle");
    expect(result.current.issue).toBeNull();
  });

  it("does not duplicate a transcript committed during the stop grace period", async () => {
    vi.useFakeTimers();
    const committed = vi.fn();
    const { result } = renderHook(() => useScribeDictation({ enabled: true, sessionId: "session-a", csrfToken: "csrf-memory", onCommitted: committed }));
    await act(async () => { await result.current.start(); });
    act(() => scribeMock.emit("partial_transcript", { text: "provisional" }));
    act(() => result.current.stop());
    act(() => scribeMock.emit("committed_transcript", { text: "definitivo" }));

    expect(committed).toHaveBeenCalledTimes(1);
    expect(committed).toHaveBeenCalledWith("definitivo");
    act(() => vi.advanceTimersByTime(1_600));
    expect(committed).toHaveBeenCalledTimes(1);
  });

  it("releases the socket on page hide and does not reuse or retry a failed token", async () => {
    const committed = vi.fn();
    const { result } = renderHook(() => useScribeDictation({ enabled: true, sessionId: "session-a", csrfToken: "csrf-memory", onCommitted: committed }));
    await act(async () => { await result.current.start(); });
    act(() => scribeMock.emit("session_started"));
    act(() => window.dispatchEvent(new Event("pagehide")));
    expect(scribeMock.connection.close).toHaveBeenCalledTimes(1);
    expect(result.current.phase).toBe("idle");
    expect(api.createTranscriptionToken).toHaveBeenCalledTimes(1);
  });

  it("maps microphone permission and quota failures to bounded UI states", async () => {
    const { result, unmount } = renderHook(() => useScribeDictation({ enabled: true, csrfToken: "csrf-memory", onCommitted: vi.fn() }));
    await act(async () => { await result.current.start(); });
    act(() => scribeMock.emit("error", { error: "Permission denied by browser" }));
    expect(result.current.issue).toBe("permissionDenied");
    unmount();

    scribeMock.reset();
    vi.mocked(api.createTranscriptionToken).mockRejectedValueOnce(new ApiError(429, "rate limited"));
    const second = renderHook(() => useScribeDictation({ enabled: true, csrfToken: "csrf-memory", onCommitted: vi.fn() }));
    await act(async () => { await second.result.current.start(); });
    await waitFor(() => expect(second.result.current.issue).toBe("quota"));
  });

  it("uses backend error codes without treating every conflict as quota", async () => {
    vi.mocked(api.createTranscriptionToken).mockRejectedValueOnce(new ApiError(
      409,
      "credential unavailable",
      "INTEGRATION_SECRET_UNAVAILABLE",
    ));
    const authFailure = renderHook(() => useScribeDictation({ enabled: true, csrfToken: "csrf-memory", onCommitted: vi.fn() }));
    await act(async () => { await authFailure.result.current.start(); });
    await waitFor(() => expect(authFailure.result.current.issue).toBe("auth"));
    authFailure.unmount();

    vi.mocked(api.createTranscriptionToken).mockRejectedValueOnce(new ApiError(
      409,
      "conflicting request",
      "TRANSCRIPTION_REQUEST_CONFLICT",
    ));
    const conflict = renderHook(() => useScribeDictation({ enabled: true, csrfToken: "csrf-memory", onCommitted: vi.fn() }));
    await act(async () => { await conflict.result.current.start(); });
    await waitFor(() => expect(conflict.result.current.issue).toBe("generic"));
  });

  it("merges committed text at the caret without sending or replacing the editable draft", () => {
    expect(insertTranscriptAtSelection("Revisa mañana", "esto", 6, 6)).toEqual({ value: "Revisa esto mañana", caret: 11 });
    expect(insertTranscriptAtSelection("Borra esto", "cámbialo", 0, 5)).toEqual({ value: "cámbialo esto", caret: 8 });
  });
});
