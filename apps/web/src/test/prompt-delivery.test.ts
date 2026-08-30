import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { submitPrompt } from "../hooks";
import { ApiError, api } from "../lib/api";
import { useAppStore } from "../store/appStore";

describe("prompt delivery classification", () => {
  beforeEach(() => {
    useAppStore.setState({
      demoMode: false, csrfToken: "csrf-memory-only", selectedSessionId: "session-a", messages: [],
      streamingBySession: {}, pendingOperations: {}, connection: "connected",
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("binds the browser idempotency key before starting the prompt request", async () => {
    const send = vi.spyOn(api, "submitPrompt").mockImplementation(async (_sessionId, _content, idempotencyKey) => {
      const stateDuringFetch = useAppStore.getState();
      const assistantId = stateDuringFetch.pendingOperations[idempotencyKey];
      expect(stateDuringFetch.messages.find((message) => message.id === assistantId)?.role).toBe("assistant");
      return { operationId: idempotencyKey, status: "accepted" };
    });

    await submitPrompt("Mensaje correlacionado");

    expect(send).toHaveBeenCalledWith("session-a", "Mensaje correlacionado", expect.any(String), "csrf-memory-only");
  });

  it.each([
    new TypeError("network disconnected"),
    new ApiError(503, "Gateway unavailable"),
    new ApiError(409, "Prompt delivery is unknown; reconcile history before sending again"),
  ])("marks transport uncertainty as ambiguous for %s", async (error) => {
    const send = vi.spyOn(api, "submitPrompt").mockRejectedValue(error);
    const history = vi.spyOn(api, "sessionHistory").mockResolvedValue({
      items: [], sessionStatus: "streaming", activeOperation: null,
    });
    await submitPrompt("Mensaje importante");
    const state = useAppStore.getState();
    const userMessage = state.messages.find((message) => message.role === "user");
    const assistantMessage = state.messages.find((message) => message.role === "assistant");
    const idempotencyKey = send.mock.calls[0][2];
    expect(send).toHaveBeenCalledWith("session-a", "Mensaje importante", idempotencyKey, "csrf-memory-only");
    expect(userMessage?.delivery).toBe("ambiguous");
    expect(assistantMessage?.content).not.toContain("rechazó");
    expect(state.pendingOperations[idempotencyKey]).toBe(assistantMessage?.id);
    expect(history).toHaveBeenCalledWith("session-a");
  });

  it("marks an explicit non-ambiguous 4xx response as failed", async () => {
    const send = vi.spyOn(api, "submitPrompt").mockRejectedValue(new ApiError(422, "Prompt inválido"));
    const history = vi.spyOn(api, "sessionHistory").mockResolvedValue({
      items: [], sessionStatus: "ready", activeOperation: null,
    });
    await submitPrompt("Mensaje inválido");
    const state = useAppStore.getState();
    expect(state.messages.find((message) => message.role === "user")?.delivery).toBe("failed");
    expect(state.messages.find((message) => message.role === "assistant")?.content).toContain("rechazó");
    expect(state.pendingOperations[send.mock.calls[0][2]]).toBeUndefined();
    expect(history).not.toHaveBeenCalled();
  });

  it("closes a fast failed operation returned in the accepted response", async () => {
    const send = vi.spyOn(api, "submitPrompt").mockImplementation(async (_sessionId, _content, idempotencyKey) => ({
      operationId: idempotencyKey,
      status: "failed",
    }));

    await submitPrompt("Mensaje aceptado que falla");

    const state = useAppStore.getState();
    const assistant = state.messages.find((message) => message.role === "assistant");
    expect(assistant?.streaming).toBe(false);
    expect(assistant?.content).toContain("terminó con error");
    expect(state.streamingBySession["session-a"]).toBeUndefined();
    expect(state.pendingOperations[send.mock.calls[0][2]]).toBeUndefined();
  });
});
