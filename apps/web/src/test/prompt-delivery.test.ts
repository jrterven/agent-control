import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { applyRealtimeEvent, rehydrateSession, stopPrompt, submitPrompt } from "../hooks";
import { ApiError, api } from "../lib/api";
import { useAppStore } from "../store/appStore";

describe("prompt delivery classification", () => {
  beforeEach(() => {
    useAppStore.getState().resetPrivateState();
    useAppStore.setState({
      demoMode: false, csrfToken: "csrf-memory-only", selectedSessionId: "session-a", messages: [],
      streamingBySession: {}, pendingOperations: {}, connection: "connected",
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("does not append an old stopped response after the next prompt when history refreshes", async () => {
    vi.spyOn(api, "submitPrompt").mockImplementation(async (_session, _content, operationId) => ({ operationId, status: "accepted" }));
    vi.spyOn(api, "interrupt").mockResolvedValue(undefined);
    await submitPrompt("Primera tarea");
    const stoppedId = useAppStore.getState().streamingBySession["session-a"];
    await stopPrompt();
    await submitPrompt("Segunda tarea");
    const state = useAppStore.getState();
    const currentId = state.streamingBySession["session-a"];
    const operationId = Object.entries(state.pendingOperations).find(([, id]) => id === currentId)![0];
    vi.spyOn(api, "sessionHistory").mockResolvedValue({
      items: [
        { id: "first-user", role: "user", content: "Primera tarea" },
        { id: "second-user", role: "user", content: "Segunda tarea" },
        { id: "tool", role: "tool", tool_name: "search", content: "Buscando" },
      ],
      sessionStatus: "streaming",
      activeOperation: { operationId, status: "streaming" },
    });

    await rehydrateSession("session-a");

    expect(useAppStore.getState().messages.some((message) => message.id === stoppedId)).toBe(false);
    expect(useAppStore.getState().pendingOperations).toEqual({ [operationId]: currentId });
    expect(useAppStore.getState().messages.at(-1)).toMatchObject({ id: currentId, streaming: true, content: "" });
  });

  it("keeps the pending operation when stopping could not be confirmed", async () => {
    vi.spyOn(api, "submitPrompt").mockImplementation(async (_session, _content, operationId) => ({ operationId, status: "accepted" }));
    vi.spyOn(api, "interrupt").mockRejectedValue(new TypeError("disconnected"));
    await submitPrompt("Tarea con resultado incierto");
    const pending = useAppStore.getState().pendingOperations;
    const streaming = useAppStore.getState().streamingBySession;

    await stopPrompt();

    expect(useAppStore.getState().pendingOperations).toEqual(pending);
    expect(useAppStore.getState().streamingBySession).toEqual(streaming);
  });

  it("does not apply late correlated content or completion to a newer response", async () => {
    vi.spyOn(api, "submitPrompt").mockImplementation(async (_session, _content, operationId) => ({ operationId, status: "accepted" }));
    vi.spyOn(api, "interrupt").mockResolvedValue(undefined);
    // Leave reconciliation pending so the assertions observe the event itself.
    vi.spyOn(api, "sessionHistory").mockImplementation(() => new Promise(() => {}));
    await submitPrompt("Tarea anterior");
    const previousOperation = Object.keys(useAppStore.getState().pendingOperations)[0];
    await stopPrompt();
    await submitPrompt("Tarea actual");
    const currentId = useAppStore.getState().streamingBySession["session-a"];
    const pending = useAppStore.getState().pendingOperations;
    applyRealtimeEvent({ type: "approval.request", controlSessionId: "session-a", data: { request_id: "current-approval", command: "current action" } });

    for (const type of ["message.delta", "tool.complete", "message.completed"]) {
      applyRealtimeEvent({ type, correlationId: previousOperation, controlSessionId: "session-a", data: { delta: "Respuesta anterior", name: "old-tool" } });
    }

    expect(useAppStore.getState().messages.find((message) => message.id === currentId)).toMatchObject({ content: "", streaming: true });
    expect(useAppStore.getState().messages.find((message) => message.id === currentId)?.tools).toBeUndefined();
    expect(useAppStore.getState().streamingBySession["session-a"]).toBe(currentId);
    expect(useAppStore.getState().pendingOperations).toEqual(pending);
    expect(useAppStore.getState().approvalsBySession["session-a"]).toHaveLength(1);
  });

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

  it("replaces a fast completed placeholder with authoritative history", async () => {
    const send = vi.spyOn(api, "submitPrompt").mockImplementation(async (_sessionId, _content, idempotencyKey) => ({
      operationId: idempotencyKey,
      status: "completed",
    }));
    const history = vi.spyOn(api, "sessionHistory").mockResolvedValue({
      items: [
        { id: "durable-user", role: "user", content: "Agenda la reunión" },
        { id: "durable-answer", role: "assistant", content: "La reunión quedó agendada." },
      ],
      sessionStatus: "ready",
      activeOperation: null,
    });

    await submitPrompt("Agenda la reunión");

    const state = useAppStore.getState();
    expect(history).toHaveBeenCalledWith("session-a");
    expect(state.messages).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "durable-answer", content: "La reunión quedó agendada." }),
    ]));
    expect(state.messages.some((message) => message.role === "assistant" && !message.content.trim())).toBe(false);
    expect(state.streamingBySession["session-a"]).toBeUndefined();
    expect(state.pendingOperations[send.mock.calls[0][2]]).toBeUndefined();
  });
});
