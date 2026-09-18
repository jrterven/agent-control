import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { submitPrompt } from "../hooks";
import { delegateLiveRequest } from "../hooks/useOpenAILive";
import { liveDelegationConversation } from "../lib/liveDelegation";
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
  afterEach(() => vi.useRealTimers());

  it("keeps observing beyond twenty minutes through temporary offline auth and returns the verified message", async () => {
    vi.useFakeTimers();
    const observed = { submitted: vi.fn(), result: vi.fn() };
    const finished = vi.fn();
    const result = delegateLiveRequest("session-papers", "profile-newton", "User: Trabajo largo", new AbortController().signal, vi.fn(), observed).then((value) => { finished(value); return value; });
    await Promise.resolve();
    useAppStore.setState({ authState: "offline" });
    await vi.advanceTimersByTimeAsync(25 * 60_000);
    expect(finished).not.toHaveBeenCalled();
    expect(observed.submitted).toHaveBeenCalledOnce();
    useAppStore.setState({ authState: "authenticated" });
    useAppStore.getState().updateMessage("voice-answer", { content: "Trabajo completado y verificado", streaming: false });
    useAppStore.getState().setStreamingMessageId("session-papers", undefined);
    expect(await result).toContain("Trabajo completado y verificado");
    expect(observed.result).toHaveBeenCalledWith(expect.objectContaining({ id: "voice-answer", role: "assistant", content: "Trabajo completado y verificado" }));
    expect(submitPrompt).toHaveBeenCalledOnce();
  });

  it("waits for the matching agent result instead of claiming the dispatch receipt completed work", async () => {
    const controller = new AbortController();
    const finished = vi.fn();
    const result = delegateLiveRequest("session-papers", "profile-newton", "User: Busca mi archivo", controller.signal, vi.fn()).then((value) => { finished(value); return value; });
    await Promise.resolve();
    expect(submitPrompt).toHaveBeenCalledOnce();
    expect(vi.mocked(submitPrompt).mock.calls[0][0]).toContain("User: Busca mi archivo");
    expect(liveDelegationConversation(vi.mocked(submitPrompt).mock.calls[0][0])).toEqual([{ role: "user", text: "Busca mi archivo" }]);
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

  it("asks the selected agent about its actual memory and capabilities without replacing its identity", async () => {
    const result = delegateLiveRequest("session-papers", "profile-newton", "User: ¿Qué recuerdas de mi proyecto y qué herramientas tienes?", new AbortController().signal, vi.fn());
    await Promise.resolve();
    const prompt = vi.mocked(submitPrompt).mock.calls[0][0];
    expect(prompt).toContain("Keep your own identity, personality, configured instructions, memory, tools and permissions");
    expect(prompt).toContain("¿Qué recuerdas de mi proyecto y qué herramientas tienes?");
    useAppStore.getState().updateMessage("voice-answer", { content: "Soy Newton. Recuerdo tu proyecto de investigación y puedo buscar publicaciones.", streaming: false });
    useAppStore.getState().setStreamingMessageId("session-papers", undefined);
    expect(await result).toContain("Soy Newton");
    expect(submitPrompt).toHaveBeenCalledOnce();
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

  it("uses the durable final answer when rehydration clears streaming before replacing optimistic text", async () => {
    const observed = { result: vi.fn() };
    const result = delegateLiveRequest("session-papers", "profile-newton", "User: Consulta estado", new AbortController().signal, vi.fn(), observed);
    await Promise.resolve();
    const prompt = vi.mocked(submitPrompt).mock.calls[0][0];
    useAppStore.getState().updateMessage("voice-answer", { content: "Respuesta parcial", streaming: false });
    useAppStore.getState().setStreamingMessageId("session-papers", undefined);
    useAppStore.getState().setMessagesForSession("session-papers", [
      { id: "durable-user", role: "user", sessionId: "session-papers", content: prompt, createdAt: "now" },
      { id: "durable-answer", role: "assistant", sessionId: "session-papers", content: "Resultado final completo y confirmado", createdAt: "now" },
    ]);
    expect(await result).toContain("Resultado final completo y confirmado");
    expect(observed.result).toHaveBeenCalledOnce();
    expect(observed.result).toHaveBeenCalledWith(expect.objectContaining({ id: "durable-answer", content: "Resultado final completo y confirmado" }));
  });

  it("returns immediately without arming a task observer if submitPrompt cannot insert a request", async () => {
    vi.mocked(submitPrompt).mockResolvedValueOnce(undefined);
    const observed = { submitted: vi.fn(), result: vi.fn() };
    const result = await delegateLiveRequest("session-papers", "profile-newton", "User: Consulta estado", new AbortController().signal, vi.fn(), observed);
    expect(result).toContain("No new task was submitted");
    expect(observed.submitted).not.toHaveBeenCalled();
    expect(observed.result).not.toHaveBeenCalled();
    expect(submitPrompt).toHaveBeenCalledOnce();
  });
});
