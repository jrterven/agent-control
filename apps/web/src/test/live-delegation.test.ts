import { beforeEach, describe, expect, it, vi } from "vitest";
import { submitPrompt } from "../hooks";
import { delegateLiveRequest } from "../hooks/useOpenAILive";
import { profiles, sessions } from "../data";
import { useAppStore } from "../store/appStore";

vi.mock("../hooks", () => ({ submitPrompt: vi.fn() }));

describe("Live delegation to the selected agent", () => {
  beforeEach(() => {
    useAppStore.setState({ authState: "authenticated", demoMode: false, selectedProfileId: "profile-newton", selectedSessionId: "session-papers", profiles: profiles.map((p) => ({ ...p, mutable: true, capabilities: { ...p.capabilities!, prompts: true } })), sessions, messages: [], streamingBySession: {}, approvalsBySession: {}, clarificationsBySession: {} });
    vi.mocked(submitPrompt).mockReset().mockImplementation(async (content) => {
      useAppStore.setState({ messages: [
        { id: "voice-user", sessionId: "session-papers", role: "user", content, delivery: "sent", createdAt: "now" },
        { id: "voice-answer", sessionId: "session-papers", role: "assistant", content: "", streaming: true, createdAt: "now" },
      ], streamingBySession: { "session-papers": "voice-answer" } });
    });
  });

  it("waits for the matching agent result instead of claiming the dispatch receipt completed work", async () => {
    const controller = new AbortController();
    const finished = vi.fn();
    const result = delegateLiveRequest("session-papers", "profile-newton", "User: Busca mi archivo", controller.signal, vi.fn()).then((value) => { finished(value); return value; });
    await Promise.resolve();
    expect(submitPrompt).toHaveBeenCalledOnce();
    expect(vi.mocked(submitPrompt).mock.calls[0][0]).toContain("User: Busca mi archivo");
    expect(finished).not.toHaveBeenCalled();
    useAppStore.getState().updateMessage("voice-answer", { content: "Encontré informe.pdf", streaming: false });
    useAppStore.getState().setStreamingMessageId("session-papers", undefined);
    expect(await result).toContain("Encontré informe.pdf");
  });

  it("preserves approval controls and waits until the pending interaction resolves", async () => {
    const waiting = vi.fn();
    const result = delegateLiveRequest("session-papers", "profile-newton", "User: Revisa esto", new AbortController().signal, waiting);
    await Promise.resolve();
    useAppStore.setState({ approvalsBySession: { "session-papers": [{ sessionId: "session-papers", requestId: "approval", state: "pending", choices: ["once", "deny"], command: "", description: "", patternKeys: [], allowSession: false, allowPermanent: false, smartDenied: false }] } });
    expect(waiting).toHaveBeenLastCalledWith(true);
    useAppStore.setState({ approvalsBySession: {} });
    useAppStore.getState().updateMessage("voice-answer", { content: "La acción fue rechazada por el usuario.", streaming: false });
    useAppStore.getState().setStreamingMessageId("session-papers", undefined);
    expect(await result).toContain("rechazada");
  });

  it("does not retry ambiguous delivery", async () => {
    vi.mocked(submitPrompt).mockImplementation(async (content) => {
      useAppStore.setState({ messages: [{ id: "ambiguous", sessionId: "session-papers", role: "user", content, delivery: "ambiguous", createdAt: "now" }] });
    });
    const result = await delegateLiveRequest("session-papers", "profile-newton", "User: Hazlo", new AbortController().signal, vi.fn());
    expect(result).toContain("unconfirmed");
    expect(submitPrompt).toHaveBeenCalledOnce();
  });

  it("does not submit into a changed, read-only or busy conversation", async () => {
    useAppStore.setState({ selectedSessionId: "other" });
    expect(await delegateLiveRequest("session-papers", "profile-newton", "User: Hazlo", new AbortController().signal, vi.fn())).toContain("No new task");
    useAppStore.setState({ selectedSessionId: "session-papers", streamingBySession: { "session-papers": "other-work" } });
    expect(await delegateLiveRequest("session-papers", "profile-newton", "User: Hazlo", new AbortController().signal, vi.fn())).toContain("still working");
    useAppStore.setState({ streamingBySession: {}, profiles: profiles.map((p) => ({ ...p, mutable: false })) });
    expect(await delegateLiveRequest("session-papers", "profile-newton", "User: Hazlo", new AbortController().signal, vi.fn())).toContain("unavailable");
    expect(submitPrompt).not.toHaveBeenCalled();
  });

  it("reconciles the matching durable result after optimistic message IDs change", async () => {
    const result = delegateLiveRequest("session-papers", "profile-newton", "User: Consulta estado  ", new AbortController().signal, vi.fn());
    await Promise.resolve();
    const content = vi.mocked(submitPrompt).mock.calls[0][0];
    useAppStore.setState({ streamingBySession: {}, messages: [
      { id: "canonical-user", role: "user", content, sessionId: "session-papers", createdAt: "now" },
      { id: "canonical-answer", role: "assistant", content: "Confirmado desde el historial", sessionId: "session-papers", createdAt: "now" },
    ] });
    expect(await result).toContain("Confirmado desde el historial");
  });

  it("ends result listening on cancellation without cancelling or resending the agent task", async () => {
    const controller = new AbortController();
    const result = delegateLiveRequest("session-papers", "profile-newton", "User: Consulta estado", controller.signal, vi.fn());
    await Promise.resolve();
    controller.abort();
    expect(await result).toContain("voice session ended");
    expect(useAppStore.getState().streamingBySession["session-papers"]).toBe("voice-answer");
    expect(submitPrompt).toHaveBeenCalledOnce();
  });
});
