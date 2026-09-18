import { createHash } from "node:crypto";
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { profiles, sessions } from "../data";
import { submitPrompt } from "../hooks";
import { liveFocusMessageId, useOpenAILive } from "../hooks/useOpenAILive";
import { api } from "../lib/api";
import type { LiveIssue, LivePhase } from "../lib/openaiLiveClient";
import { useAppStore } from "../store/appStore";
import type { ChatMessage } from "../types";

type Options = {
  negotiate: (sdp: string, signal: AbortSignal) => Promise<unknown>;
  onPhase: (phase: LivePhase) => void;
  onIssue: (issue: LiveIssue) => void;
  onDelegation: (context: string, signal: AbortSignal, progress: (content: string) => void) => Promise<string>;
  initialCommentary?: string;
  initiallyPaused?: boolean;
};
const mocks = vi.hoisted(() => ({ calls: [] as MockClient[], flush: vi.fn(), append: vi.fn(), drain: vi.fn() }));
class MockClient {
  options: Options;
  controller = new AbortController();
  paused: boolean;
  started = false;
  start = vi.fn(async () => { this.options.onPhase("connecting"); await this.options.negotiate("offer", this.controller.signal); this.started = true; this.options.onPhase(this.paused ? "paused" : "listening"); });
  stop = vi.fn(() => { this.options.onPhase("stopping"); });
  dispose = vi.fn(() => { this.controller.abort(); });
  setPaused = vi.fn((paused: boolean) => { this.paused = paused; if (this.started) this.options.onPhase(paused ? "paused" : "listening"); });
  play = vi.fn();
  constructor(options: Options) { this.options = options; this.paused = options.initiallyPaused ?? false; mocks.calls.push(this); }
  closed() { this.controller.abort(); this.options.onPhase("idle"); }
  fail(issue: LiveIssue) { this.controller.abort(); this.options.onIssue(issue); this.options.onPhase("error"); }
  delegate(context = "User: Busca el informe") { return this.options.onDelegation(context, this.controller.signal, vi.fn()); }
}
vi.mock("../lib/openaiLiveClient", () => ({ OpenAILiveClient: class { constructor(options: Options) { return new MockClient(options); } }, liveSupported: () => true }));
vi.mock("../hooks", () => ({ submitPrompt: vi.fn() }));
vi.mock("../lib/api", () => ({ api: { createLiveSession: vi.fn(async () => ({ session: { id: "live" }, transport: { sdp: "answer" } })) } }));
vi.mock("../hooks/useLiveTranscripts", () => ({ useLiveTranscripts: () => ({ begin: () => ({ flush: mocks.flush, append: mocks.append, drain: mocks.drain }), calls: [] }) }));

const options = { enabled: true, sessionId: "session-papers", profileId: "profile-newton", csrfToken: "csrf-owner" };
const message: ChatMessage = { id: "final-answer", sessionId: options.sessionId, role: "assistant", content: "Encontré informe.pdf\nSin repetir ninguna acción.", createdAt: "now" };
const complete = () => {
  useAppStore.getState().updateMessage("voice-answer", { content: message.content, streaming: false });
  useAppStore.getState().setStreamingMessageId(options.sessionId, undefined);
};
const tick = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };

describe("Live consent, task suspension and verified resumption", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    mocks.calls.length = 0;
    mocks.flush.mockClear();
    mocks.append.mockClear();
    mocks.drain.mockReset().mockResolvedValue(undefined);
    vi.mocked(api.createLiveSession).mockClear();
    Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    vi.stubGlobal("crypto", {
      randomUUID: () => "random-id",
      subtle: { digest: vi.fn(async (_algorithm: string, bytes: Uint8Array) => Uint8Array.from(createHash("sha256").update(bytes).digest()).buffer) },
    });
    useAppStore.setState({ authState: "authenticated", userId: "owner-one", demoMode: false, selectedProfileId: options.profileId, selectedSessionId: options.sessionId, profiles: profiles.map((profile) => ({ ...profile, mutable: true, capabilities: { ...profile.capabilities!, prompts: true } })), sessions, messages: [], streamingBySession: {}, approvalsBySession: {}, clarificationsBySession: {} });
    vi.mocked(submitPrompt).mockReset().mockImplementation(async (content) => {
      useAppStore.setState({ messages: [
        { id: "voice-user", sessionId: options.sessionId, role: "user", content, delivery: "sent", createdAt: "now" },
        { id: "voice-answer", sessionId: options.sessionId, role: "assistant", content: "", streaming: true, createdAt: "now" },
      ], streamingBySession: { [options.sessionId]: "voice-answer" } });
    });
  });
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

  it("keeps short tasks in the same call and only closes after 15 seconds of actual work", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    expect(mocks.calls).toHaveLength(0);
    await act(async () => { await result.current.start(); });
    const call = mocks.calls[0];
    let delegated!: Promise<string>;
    await act(async () => { delegated = call.delegate(); });
    await tick(14_999);
    expect(call.stop).not.toHaveBeenCalled();
    await act(async () => { complete(); await delegated; });
    expect(call.stop).not.toHaveBeenCalled();
    expect(result.current.phase).toBe("listening");
    expect(result.current.active).toBe(true);
    expect(result.current.captureActive).toBe(true);
    expect(result.current.pendingResult).toBeNull();
    await tick(60_000);
    expect(call.stop).not.toHaveBeenCalled();
  });

  it("releases the long call, observes more than 20 minutes and resumes exactly once from the verified answer", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); });
    const first = mocks.calls[0];
    let delegated!: Promise<string>;
    await act(async () => { delegated = first.delegate(); });
    await tick(15_000);
    expect(first.stop).toHaveBeenCalledOnce();
    expect(result.current.phase).toBe("stopping");
    expect(result.current.captureActive).toBe(false);
    await act(async () => { first.closed(); });
    expect(first.controller.signal.aborted).toBe(true);
    expect(result.current.phase).toBe("waiting");
    expect(result.current.active).toBe(true);
    await tick(25 * 60_000);
    expect(result.current.working).toBe(true);
    expect(mocks.calls).toHaveLength(1);
    await act(async () => { complete(); await delegated; });
    await tick();
    expect(mocks.calls).toHaveLength(2);
    expect(api.createLiveSession).toHaveBeenLastCalledWith(expect.objectContaining({ sessionId: options.sessionId, profileId: options.profileId, purpose: "resume", focusMessageId: `sha256:${createHash("sha256").update(message.content).digest("hex")}` }), options.csrfToken, expect.any(AbortSignal));
    expect(mocks.calls[1].options.initialCommentary).toContain("Do not repeat its task");
    expect(submitPrompt).toHaveBeenCalledOnce();
    expect(result.current.phase).toBe("listening");
    expect(result.current.working).toBe(false);
  });

  it("queues a fast final result arriving during close until session.closed", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); });
    const call = mocks.calls[0];
    let delegated!: Promise<string>;
    await act(async () => { delegated = call.delegate(); });
    await tick(15_000);
    await act(async () => { complete(); await delegated; });
    expect(result.current.pendingResult?.content).toBe(message.content);
    expect(mocks.calls).toHaveLength(1);
    expect(result.current.phase).toBe("stopping");
    await act(async () => { call.closed(); });
    await tick();
    expect(mocks.calls).toHaveLength(2);
    expect(submitPrompt).toHaveBeenCalledOnce();
  });

  it("manual stop invalidates automatic resumption without cancelling the task", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); });
    const call = mocks.calls[0];
    let delegated!: Promise<string>;
    await act(async () => { delegated = call.delegate(); });
    await tick(15_000);
    await act(async () => { result.current.stop(); call.closed(); });
    expect(result.current.active).toBe(false);
    expect(useAppStore.getState().streamingBySession[options.sessionId]).toBe("voice-answer");
    await act(async () => { complete(); await delegated; });
    await tick();
    expect(mocks.calls).toHaveLength(1);
    expect(result.current.resumeAvailable).toBe(true);
    await act(async () => { await result.current.start(); });
    expect(mocks.calls).toHaveLength(2);
    expect(submitPrompt).toHaveBeenCalledOnce();
    expect(api.createLiveSession).toHaveBeenLastCalledWith(expect.objectContaining({ purpose: "resume" }), options.csrfToken, expect.any(AbortSignal));
  });

  it("background capture closes and returning never opens a microphone until an explicit gesture", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); });
    const call = mocks.calls[0];
    let delegated!: Promise<string>;
    await act(async () => { delegated = call.delegate(); });
    await act(async () => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
      document.dispatchEvent(new Event("visibilitychange"));
      call.closed();
    });
    expect(result.current.active).toBe(false);
    expect(result.current.captureActive).toBe(false);
    await act(async () => { complete(); await delegated; await result.current.start(); });
    expect(mocks.calls).toHaveLength(1);
    await act(async () => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(mocks.calls).toHaveLength(1);
    expect(result.current.resumeAvailable).toBe(true);
    await act(async () => { await result.current.start(); });
    expect(mocks.calls).toHaveLength(2);
  });

  it("explicitly reactivates a pending task without opening another call before its result", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); });
    const first = mocks.calls[0];
    let delegated!: Promise<string>;
    await act(async () => { delegated = first.delegate(); result.current.stop(); first.closed(); });
    await act(async () => { await result.current.start(); });
    expect(result.current.active).toBe(true);
    expect(result.current.phase).toBe("waiting");
    expect(result.current.captureActive).toBe(false);
    expect(mocks.calls).toHaveLength(1);
    await act(async () => { complete(); await delegated; });
    await tick();
    expect(mocks.calls).toHaveLength(2);
    expect(submitPrompt).toHaveBeenCalledOnce();
  });

  it.each(["owner", "session", "profile", "logout", "unmount"])("never reopens after changing %s", async (change) => {
    const view = renderHook((props) => useOpenAILive(props), { initialProps: options });
    await act(async () => { await view.result.current.start(); });
    const call = mocks.calls[0];
    let delegated!: Promise<string>;
    await act(async () => { delegated = call.delegate(); });
    await tick(15_000);
    await act(async () => {
      if (change === "owner") useAppStore.setState({ userId: "owner-two" });
      if (change === "session") { useAppStore.setState({ selectedSessionId: "other" }); view.rerender({ ...options, sessionId: "other" }); }
      if (change === "profile") { useAppStore.setState({ selectedProfileId: "other" }); view.rerender({ ...options, profileId: "other" }); }
      if (change === "logout") useAppStore.setState({ authState: "unauthenticated" });
      if (change === "unmount") view.unmount();
      call.closed();
      complete();
      await delegated;
    });
    await tick();
    expect(mocks.calls).toHaveLength(1);
    expect(submitPrompt).toHaveBeenCalledOnce();
  });

  it("waits through approval and clarification while voice is closed", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); });
    const call = mocks.calls[0];
    let delegated!: Promise<string>;
    await act(async () => { delegated = call.delegate(); });
    await tick(15_000);
    await act(async () => {
      call.closed();
      useAppStore.setState({ approvalsBySession: { [options.sessionId]: [{ sessionId: options.sessionId, requestId: "approval", state: "pending", choices: ["once", "deny"], command: "", description: "", patternKeys: [], allowSession: false, allowPermanent: false, smartDenied: false }] } });
      complete();
    });
    expect(result.current.waitingApproval).toBe(true);
    expect(mocks.calls).toHaveLength(1);
    await tick(25 * 60_000);
    expect(result.current.working).toBe(true);
    await act(async () => { useAppStore.setState({ approvalsBySession: {} }); await delegated; });
    await tick();
    expect(mocks.calls).toHaveLength(2);
  });

  it("retains a completed result for explicit retry after close was unconfirmed", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); });
    const call = mocks.calls[0];
    let delegated!: Promise<string>;
    await act(async () => { delegated = call.delegate(); });
    await tick(15_000);
    await act(async () => { call.fail("unconfirmed"); complete(); await delegated; });
    await tick();
    expect(mocks.calls).toHaveLength(1);
    expect(result.current.issue).toBe("unconfirmed");
    expect(result.current.active).toBe(false);
    expect(result.current.resumeAvailable).toBe(true);
    expect(result.current.pendingResult?.content).toBe(message.content);
  });

  it("explains the selected full answer by content hash without submitting any work", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    const fullMessage = { ...message, content: `  ${"Respuesta completa. ".repeat(600)}\r\nFinal.  ` };
    await act(async () => { await result.current.explain(fullMessage); });
    expect(api.createLiveSession).toHaveBeenLastCalledWith(expect.objectContaining({ purpose: "explain", focusMessageId: await liveFocusMessageId(fullMessage) }), options.csrfToken, expect.any(AbortSignal));
    expect(result.current.explainingMessageId).toBe(message.id);
    expect(submitPrompt).not.toHaveBeenCalled();
    expect(mocks.calls[0].options.initialCommentary).not.toContain(fullMessage.content);
    expect(mocks.calls[0].options.initialCommentary).toContain("verified answer selected");
  });

  it("waits for the current call's close acknowledgement before explaining and honors stop while waiting", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); });
    let explaining!: Promise<void>;
    await act(async () => { explaining = result.current.explain(message); });
    expect(mocks.calls[0].stop).toHaveBeenCalledOnce();
    expect(mocks.calls).toHaveLength(1);
    await act(async () => { result.current.stop(); mocks.calls[0].closed(); await explaining; });
    expect(mocks.calls).toHaveLength(1);
    expect(result.current.active).toBe(false);
    expect(submitPrompt).not.toHaveBeenCalled();
  });

  it("leaves an ordinary stopped conversation compact instead of offering a pending-task resume", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); result.current.stop(); mocks.calls[0].closed(); });
    expect(result.current.active).toBe(false);
    expect(result.current.resumeAvailable).toBe(false);
    expect(result.current.phase).toBe("idle");
  });

  it("does not open a fresh microphone over unrelated agent work", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    useAppStore.setState({ streamingBySession: { [options.sessionId]: "another-task" } });
    await act(async () => { await result.current.start(); });
    expect(mocks.calls).toHaveLength(0);
    expect(result.current.active).toBe(false);
    // Explaining an earlier verified answer remains an explicit user choice.
    await act(async () => { await result.current.explain(message); });
    expect(mocks.calls).toHaveLength(1);
    expect(submitPrompt).not.toHaveBeenCalled();
  });

  it("reports a delayed submission error after closing instead of waiting forever or resubmitting", async () => {
    let reject!: (reason: unknown) => void;
    vi.mocked(submitPrompt).mockImplementationOnce((content) => {
      useAppStore.setState({ messages: [{ id: "voice-user", role: "user", sessionId: options.sessionId, content, createdAt: "now" }], streamingBySession: { [options.sessionId]: "voice-answer" } });
      return new Promise<void>((_resolve, rejectPromise) => { reject = rejectPromise; });
    });
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); });
    const call = mocks.calls[0];
    let delegated!: Promise<string>;
    await act(async () => { delegated = call.delegate(); });
    await tick(15_000);
    await act(async () => { call.closed(); reject(new Error("delivery unavailable")); await delegated; });
    expect(result.current.active).toBe(false);
    expect(result.current.working).toBe(false);
    expect(result.current.issue).toBe("generic");
    expect(mocks.calls).toHaveLength(1);
    expect(submitPrompt).toHaveBeenCalledOnce();
  });

  it("cancels an explanation before capture if the app becomes hidden while preparing its hash", async () => {
    let resolve!: (digest: ArrayBuffer) => void;
    vi.mocked(crypto.subtle.digest).mockImplementationOnce(() => new Promise<ArrayBuffer>((done) => { resolve = done; }));
    const { result } = renderHook(() => useOpenAILive(options));
    let explaining!: Promise<void>;
    await act(async () => { explaining = result.current.explain(message); });
    expect(mocks.calls).toHaveLength(0);
    await act(async () => {
      Object.defineProperty(document, "visibilityState", { configurable: true, value: "hidden" });
      document.dispatchEvent(new Event("visibilitychange"));
      resolve(new ArrayBuffer(32));
      await explaining;
    });
    expect(mocks.calls).toHaveLength(0);
    expect(result.current.captureActive).toBe(false);
    expect(result.current.active).toBe(false);
    expect(result.current.phase).toBe("waiting");
    expect(result.current.resumeAvailable).toBe(true);
  });

  it("waits for an earlier conversation's transport to close before an explicit start in another chat", async () => {
    const view = renderHook((props) => useOpenAILive(props), { initialProps: options });
    await act(async () => { await view.result.current.start(); });
    const previous = mocks.calls[0];
    await act(async () => {
      useAppStore.setState({ selectedSessionId: "other" });
      view.rerender({ ...options, sessionId: "other" });
    });
    let starting!: Promise<void>;
    await act(async () => { starting = view.result.current.start(); });
    expect(previous.stop).toHaveBeenCalledOnce();
    expect(mocks.calls).toHaveLength(1);
    await act(async () => { previous.closed(); await starting; });
    expect(mocks.calls).toHaveLength(2);
    expect(api.createLiveSession).toHaveBeenLastCalledWith(expect.objectContaining({ sessionId: "other" }), options.csrfToken, expect.any(AbortSignal));
  });

  it("waits for the bounded final-caption drain after transport close before loading resumed context", async () => {
    let saved!: () => void;
    mocks.drain.mockImplementationOnce(() => new Promise<void>((resolve) => { saved = resolve; }));
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); });
    const call = mocks.calls[0];
    let delegated!: Promise<string>;
    await act(async () => { delegated = call.delegate(); });
    await tick(15_000);
    await act(async () => { call.closed(); complete(); await delegated; });
    expect(result.current.phase).toBe("waiting");
    expect(result.current.captureActive).toBe(false);
    expect(mocks.calls).toHaveLength(1);
    await act(async () => { saved(); });
    await tick();
    expect(mocks.calls).toHaveLength(2);
    expect(api.createLiveSession).toHaveBeenLastCalledWith(expect.objectContaining({ purpose: "resume" }), options.csrfToken, expect.any(AbortSignal));
  });

  it("preserves microphone pause across automatic result resumption until an explicit resume gesture", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); });
    const first = mocks.calls[0];
    let delegated!: Promise<string>;
    await act(async () => { delegated = first.delegate(); result.current.pause(); });
    expect(result.current.phase).toBe("paused");
    await tick(15_000);
    await act(async () => { first.closed(); complete(); await delegated; });
    await tick();
    expect(mocks.calls).toHaveLength(2);
    expect(mocks.calls[1].options.initiallyPaused).toBe(true);
    expect(result.current.phase).toBe("paused");
    expect(result.current.pendingResult).toBeNull();
    expect(submitPrompt).toHaveBeenCalledOnce();
    await act(async () => { result.current.resume(); });
    expect(mocks.calls[1].setPaused).toHaveBeenLastCalledWith(false);
    expect(result.current.phase).toBe("listening");
  });

  it("resets microphone pause when the user explicitly starts a new call after stopping", async () => {
    const { result } = renderHook(() => useOpenAILive(options));
    await act(async () => { await result.current.start(); result.current.pause(); result.current.stop(); mocks.calls[0].closed(); });
    await act(async () => { await result.current.start(); });
    expect(mocks.calls[1].options.initiallyPaused).toBe(false);
    expect(result.current.phase).toBe("listening");
  });
});
