import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { rehydrateSession } from "../hooks";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";

describe("PWA background operation recovery", () => {
  beforeEach(() => {
    useAppStore.setState({
      demoMode: false,
      messages: [],
      streamingBySession: {},
      pendingOperations: {},
      connection: "connected",
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("reconstructs an active response after process eviction and settles from history", async () => {
    const history = vi.spyOn(api, "sessionHistory");
    history.mockResolvedValueOnce({
      items: [
        { id: "durable-user", role: "user", content: "Sigue trabajando" },
      ],
      sessionStatus: "streaming",
      activeOperation: {
        operationId: "operation-background",
        status: "streaming",
        acceptedAt: "2026-08-30T20:15:00+00:00",
      },
    });

    await rehydrateSession("session-background");

    let state = useAppStore.getState();
    const recovered = state.messages.find((message) => (
      message.sessionId === "session-background" && message.role === "assistant"
    ));
    expect(recovered).toMatchObject({ content: "", streaming: true });
    expect(state.streamingBySession["session-background"]).toBe(recovered?.id);
    expect(state.pendingOperations["operation-background"]).toBe(recovered?.id);

    history.mockResolvedValueOnce({
      items: [
        { id: "durable-user", role: "user", content: "Sigue trabajando" },
        { id: "durable-answer", role: "assistant", content: "Trabajo terminado" },
      ],
      sessionStatus: "ready",
      activeOperation: null,
    });

    await rehydrateSession("session-background");

    state = useAppStore.getState();
    expect(state.streamingBySession["session-background"]).toBeUndefined();
    expect(state.pendingOperations["operation-background"]).toBeUndefined();
    expect(state.messages.filter((message) => message.sessionId === "session-background")).toEqual([
      expect.objectContaining({ id: "durable-user", role: "user" }),
      expect.objectContaining({ id: "durable-answer", role: "assistant", content: "Trabajo terminado" }),
    ]);
  });

  it("does not let an older active snapshot overwrite newer terminal history", async () => {
    type SessionHistory = Awaited<ReturnType<typeof api.sessionHistory>>;
    let resolveActive!: (value: SessionHistory) => void;
    let resolveTerminal!: (value: SessionHistory) => void;
    vi.spyOn(api, "sessionHistory")
      .mockImplementationOnce(() => new Promise((resolve) => { resolveActive = resolve; }))
      .mockImplementationOnce(() => new Promise((resolve) => { resolveTerminal = resolve; }));

    const olderRequest = rehydrateSession("session-race");
    const newerRequest = rehydrateSession("session-race");
    resolveTerminal({
      items: [{ id: "final-answer", role: "assistant", content: "Respuesta final" }],
      sessionStatus: "ready",
      activeOperation: null,
    });
    await newerRequest;
    resolveActive({
      items: [{ id: "user-race", role: "user", content: "Continúa" }],
      sessionStatus: "streaming",
      activeOperation: { operationId: "operation-race", status: "streaming" },
    });
    await olderRequest;

    const state = useAppStore.getState();
    expect(state.streamingBySession["session-race"]).toBeUndefined();
    expect(state.pendingOperations["operation-race"]).toBeUndefined();
    expect(state.messages.filter((message) => message.sessionId === "session-race")).toEqual([
      expect.objectContaining({ id: "final-answer", content: "Respuesta final" }),
    ]);
  });
});
