import axe from "axe-core";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BackgroundTaskList, BackgroundTasks, taskResultAnchor } from "../components/BackgroundTasks";
import { applyRealtimeEvent, rehydrateBackgroundTasks, rehydrateSession, stopPrompt, submitPrompt } from "../hooks";
import { api } from "../lib/api";
import { normalizeBackgroundTasks } from "../lib/backgroundTasks";
import { activeResponseId, useAppStore } from "../store/appStore";
import type { BackgroundTaskSnapshot, ChatMessage, RealtimeEvent } from "../types";

const observedAt = "2026-09-18T20:00:00Z";
const snapshot: BackgroundTaskSnapshot = {
  items: [{ id: "task-12345678", state: "running", deliveryState: "pending", title: "Tarea delegada", createdAt: observedAt, updatedAt: observedAt }],
  available: true, complete: true, activeCount: 1, pendingDeliveryCount: 0, observedAt,
};
const human: ChatMessage = { id: "human-response", sessionId: "conversation", role: "assistant", content: "Respuesta humana", createdAt: "", streaming: true };
const frame = (type: string, turnId?: string, data: Record<string, unknown> = {}): RealtimeEvent => ({
  type, controlSessionId: "conversation", occurredAt: observedAt,
  data: { ...data, controlTurn: { correlation: "history", ...(turnId ? { id: turnId } : {}) } },
});

describe("background task conversation state", () => {
  beforeEach(() => {
    useAppStore.getState().resetPrivateState();
    useAppStore.setState({ authState: "authenticated", userId: "owner-a", selectedSessionId: "conversation", demoMode: false, connection: "connected" });
    vi.spyOn(api, "backgroundTasks").mockResolvedValue(snapshot);
  });
  afterEach(() => vi.restoreAllMocks());

  it("normalizes the public inventory without retaining internal instructions, results or unknown state claims", () => {
    const parsed = normalizeBackgroundTasks({ ...snapshot, items: [{ ...snapshot.items[0], state: "invented", deliveryState: "internal_result", goal: "PRIVATE PROMPT", result: "INTERNAL RESULT" }] });
    expect(parsed?.items[0]).toMatchObject({ state: "unknown", deliveryState: "unknown" });
    expect(JSON.stringify(parsed)).not.toMatch(/PRIVATE|INTERNAL/);
    expect(normalizeBackgroundTasks({ ...snapshot, observedAt: "not a timestamp" })).toBeUndefined();
  });

  it("keeps newer events over slow snapshots and preserves tasks missing from partial inventory", async () => {
    let resolve!: (value: BackgroundTaskSnapshot) => void;
    vi.mocked(api.backgroundTasks).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const pending = rehydrateBackgroundTasks("conversation");
    applyRealtimeEvent({ type: "background.tasks", controlSessionId: "conversation", data: { ...snapshot, observedAt: "2026-09-18T20:01:00Z", activeCount: 0, items: [{ ...snapshot.items[0], state: "completed" }] } });
    resolve(snapshot);
    await pending;
    expect(useAppStore.getState().backgroundTasksBySession.conversation.items[0].state).toBe("completed");
    useAppStore.getState().setBackgroundTasks("conversation", { ...snapshot, items: [], complete: false, activeCount: null, observedAt: "2026-09-18T20:02:00Z" });
    expect(useAppStore.getState().backgroundTasksBySession.conversation.items).toHaveLength(1);
    useAppStore.getState().setBackgroundTasks("conversation", { ...snapshot, items: [], activeCount: 0, observedAt: "2026-09-18T20:03:00Z" });
    expect(useAppStore.getState().backgroundTasksBySession.conversation.items).toHaveLength(0);
  });

  it("clears task inventories on removal and rejects reads finishing after an account switch", async () => {
    useAppStore.getState().setBackgroundTasks("conversation", snapshot);
    useAppStore.getState().setBackgroundTasks("other", snapshot);
    useAppStore.getState().removeSession("conversation");
    expect(useAppStore.getState().backgroundTasksBySession.conversation).toBeUndefined();
    expect(useAppStore.getState().backgroundTasksBySession.other).toBeDefined();
    let resolve!: (value: BackgroundTaskSnapshot) => void;
    vi.mocked(api.backgroundTasks).mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const pending = rehydrateBackgroundTasks("conversation");
    useAppStore.getState().setAuth("authenticated", "Other", "csrf", false, "owner-b");
    resolve(snapshot);
    await pending;
    expect(useAppStore.getState().backgroundTasksBySession).toEqual({});
  });

  it("isolates spontaneous streams and double starts from a pending human response and its approval", async () => {
    vi.spyOn(api, "sessionHistory").mockImplementation(() => new Promise(() => {}));
    useAppStore.setState({ messages: [human], streamingBySession: { conversation: human.id }, pendingOperations: { "human-op": human.id } });
    applyRealtimeEvent({ type: "approval.request", controlSessionId: "conversation", data: { request_id: "approval", command: "test action" } });
    applyRealtimeEvent(frame("message.start", "wrapper"));
    applyRealtimeEvent(frame("message.start", "runner"));
    expect(useAppStore.getState().messages).toHaveLength(1);
    applyRealtimeEvent(frame("message.delta", "runner", { delta: "Aviso de una tarea" }));
    applyRealtimeEvent(frame("tool.completed", "runner", { name: "fixture_lookup", summary: "Consulta terminada" }));
    applyRealtimeEvent(frame("message.completed", "runner", { usage: { total: 900, active_subagents: 2 } }));
    const state = useAppStore.getState();
    expect(state.messages.find((message) => message.id === human.id)).toMatchObject({ content: "Respuesta humana", streaming: true });
    expect(state.messages.find((message) => message.controlTurnId === "runner")).toMatchObject({ content: "Aviso de una tarea", streaming: false, tools: [expect.objectContaining({ name: "fixture_lookup" })] });
    expect(state.messages).toHaveLength(2);
    expect(state.streamingBySession.conversation).toBe(human.id);
    expect(state.pendingOperations["human-op"]).toBe(human.id);
    expect(state.approvalsBySession.conversation).toHaveLength(1);
    expect(state.runtimeTurnBySession.conversation).toBeUndefined();
    expect(state.sessionUsageById.conversation).toMatchObject({ totalTokens: 900, activeSubagents: 2 });
  });

  it("does not merge orphan deltas or let an old native terminal unlock a newer coordinator turn", () => {
    vi.spyOn(api, "sessionHistory").mockImplementation(() => new Promise(() => {}));
    useAppStore.setState({ messages: [human], streamingBySession: { conversation: human.id } });
    applyRealtimeEvent(frame("message.delta", undefined, { delta: "Uncorrelated text" }));
    applyRealtimeEvent(frame("message.start", "older"));
    applyRealtimeEvent(frame("message.start", "newer"));
    applyRealtimeEvent(frame("message.completed", "older"));
    expect(useAppStore.getState().messages).toEqual([human]);
    expect(useAppStore.getState().runtimeTurnBySession.conversation).toBe("newer");
  });

  it("does not clear a newer runtime gate when an explicitly requested stop resolves late", async () => {
    let resolve!: () => void;
    vi.spyOn(api, "interrupt").mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    applyRealtimeEvent(frame("message.start", "old-stopped"));
    applyRealtimeEvent(frame("message.delta", "old-stopped", { delta: "Respuesta anterior" }));
    const stopping = stopPrompt();
    applyRealtimeEvent(frame("message.start", "new-speaking"));
    resolve();
    await stopping;
    expect(useAppStore.getState().runtimeTurnBySession.conversation).toBe("new-speaking");
  });

  it("settles both native and human response placeholders after a confirmed stop", async () => {
    vi.spyOn(api, "interrupt").mockResolvedValue(undefined);
    useAppStore.setState({ messages: [human], streamingBySession: { conversation: human.id }, pendingOperations: { "human-op": human.id } });
    applyRealtimeEvent(frame("message.start", "native-stopped"));
    applyRealtimeEvent(frame("tool.completed", "native-stopped", { name: "search" }));

    await stopPrompt();

    expect(useAppStore.getState().pendingOperations).toEqual({});
    expect(useAppStore.getState().messages.every((message) => !message.streaming)).toBe(true);
    useAppStore.getState().setMessagesForSession("conversation", [{ id: "later-user", sessionId: "conversation", role: "user", content: "Nueva tarea", createdAt: "" }]);
    expect(useAppStore.getState().messages.map((message) => message.id)).toEqual(["later-user"]);
  });

  it("rehydrates task origin and waits for complete history when reconnecting mid-turn", async () => {
    const history = vi.spyOn(api, "sessionHistory").mockResolvedValueOnce({
      items: [{ id: "partial", role: "assistant", content: "Prefijo conservado" }], sessionStatus: "streaming", activeOperation: null, activeTurnId: "resumed-native",
    });
    await rehydrateSession("conversation");
    expect(activeResponseId(useAppStore.getState(), "conversation")).toBe("control-turn-conversation-resumed-native");
    applyRealtimeEvent(frame("message.delta", "resumed-native", { delta: " continuación" }));
    expect(useAppStore.getState().messages).toHaveLength(1);
    expect(useAppStore.getState().messages[0].content).toBe("Prefijo conservado");
    history.mockResolvedValueOnce({ items: [{ id: "durable-result", role: "assistant", content: "Respuesta completa", controlTurnOrigin: { kind: "background_task", taskId: snapshot.items[0].id } }], sessionStatus: "ready", activeOperation: null, activeTurnId: null });
    applyRealtimeEvent(frame("message.completed", "resumed-native"));
    await waitFor(() => expect(useAppStore.getState().messages[0].id).toBe("durable-result"));
    expect(useAppStore.getState().messages[0].controlTurnOrigin).toEqual({ kind: "background_task", taskId: snapshot.items[0].id });
    expect(activeResponseId(useAppStore.getState(), "conversation")).toBeUndefined();
  });

  it("does not duplicate a partial live response when reconnect history contains the same fragment", async () => {
    applyRealtimeEvent(frame("message.start", "reconnect-live"));
    applyRealtimeEvent(frame("message.delta", "reconnect-live", { delta: "Prefijo único" }));
    vi.spyOn(api, "sessionHistory").mockResolvedValue({
      items: [{ id: "native-partial", role: "assistant", content: "Prefijo único" }], activeOperation: null, sessionStatus: "streaming", activeTurnId: "reconnect-live",
    });
    await rehydrateSession("conversation");
    applyRealtimeEvent(frame("message.delta", "reconnect-live", { delta: " sin base conocida" }));
    expect(useAppStore.getState().messages).toEqual([expect.objectContaining({ id: "native-partial", content: "Prefijo único" })]);
    expect(activeResponseId(useAppStore.getState(), "conversation")).toBeDefined();
  });

  it("cannot settle a human prompt submitted after a history read started", async () => {
    let resolve!: (value: Awaited<ReturnType<typeof api.sessionHistory>>) => void;
    vi.spyOn(api, "sessionHistory").mockImplementationOnce(() => new Promise((done) => { resolve = done; }));
    const pending = rehydrateSession("conversation");
    const acknowledgedUser: ChatMessage = { id: "new-acknowledged-human", sessionId: "conversation", role: "user", content: "Nueva petición ya recibida", delivery: "sent", createdAt: "" };
    useAppStore.setState({ messages: [acknowledgedUser, human], streamingBySession: { conversation: human.id }, pendingOperations: { "human-op": human.id } });
    resolve({ items: [], activeOperation: null, sessionStatus: "ready", activeTurnId: null });
    await pending;
    expect(useAppStore.getState().streamingBySession.conversation).toBe(human.id);
    expect(useAppStore.getState().pendingOperations["human-op"]).toBe(human.id);
    expect(useAppStore.getState().messages).toEqual([acknowledgedUser, human]);
  });

  it("permits a new human message with delegated tasks running but blocks concurrent coordinator turns", async () => {
    const submit = vi.spyOn(api, "submitPrompt").mockResolvedValue({ operationId: "new-human", status: "streaming" });
    useAppStore.getState().setBackgroundTasks("conversation", snapshot);
    useAppStore.getState().setRuntimeTurn("conversation", "still-speaking");
    await submitPrompt("No debe enviarse aún");
    expect(submit).not.toHaveBeenCalled();
    useAppStore.getState().setRuntimeTurn("conversation");
    await submitPrompt("Pregunta independiente");
    expect(submit).toHaveBeenCalledTimes(1);
    expect(submit.mock.calls[0][1]).toBe("Pregunta independiente");
  });

  it("preserves the acknowledged queued message until its own prompt is present in authoritative history", async () => {
    vi.spyOn(api, "submitPrompt").mockResolvedValue({ operationId: "queued-human", status: "queued" });
    const history = vi.spyOn(api, "sessionHistory").mockResolvedValue({
      items: [{ id: "old-background-result", role: "assistant", content: "Terminó una tarea anterior", controlTurnOrigin: { kind: "background_task" } }],
      activeOperation: { operationId: "queued-human", status: "queued" }, sessionStatus: "ready", activeTurnId: null,
    });
    await submitPrompt("Petición explícita que ganó una carrera");
    await rehydrateSession("conversation");
    expect(useAppStore.getState().messages.find((message) => message.role === "user")).toMatchObject({ content: "Petición explícita que ganó una carrera", delivery: "queued" });
    expect(useAppStore.getState().pendingOperations["queued-human"]).toBeDefined();
    history.mockResolvedValueOnce({
      items: [{ id: "queued-durable-user", role: "user", content: "Petición explícita que ganó una carrera" }],
      activeOperation: { operationId: "queued-human", status: "streaming" }, sessionStatus: "streaming", activeTurnId: "queued-native",
    });
    await rehydrateSession("conversation");
    expect(useAppStore.getState().messages.filter((message) => message.role === "user")).toEqual([expect.objectContaining({ id: "queued-durable-user", delivery: "sent" })]);
    expect(useAppStore.getState().pendingOperations["queued-human"]).toBeDefined();
    history.mockResolvedValueOnce({
      items: [{ id: "queued-durable-user", role: "user", content: "Petición explícita que ganó una carrera" }, { id: "queued-durable-answer", role: "assistant", content: "Respondida" }],
      activeOperation: null, sessionStatus: "ready", activeTurnId: null,
    });
    await rehydrateSession("conversation");
    expect(useAppStore.getState().messages.filter((message) => message.role === "user")).toHaveLength(1);
    expect(useAppStore.getState().pendingOperations["queued-human"]).toBeUndefined();
  });

  it("renders scoped task states accessibly and links only to the coordinator's public response", async () => {
    const result: ChatMessage = { id: "result", sessionId: "conversation", role: "assistant", content: "Resultado público", createdAt: "", controlTurnOrigin: { kind: "background_task", taskId: snapshot.items[0].id } };
    const { container } = render(<BackgroundTaskList snapshot={{ ...snapshot, activeCount: null, complete: false, items: [{ ...snapshot.items[0], state: "unknown" }] }} messages={[result]} />);
    fireEvent.click(screen.getByText("Tareas de esta conversación"));
    expect(screen.getByText("Estado por confirmar", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("Estado parcial; puede haber más tareas en curso.")).toBeVisible();
    expect(screen.getByRole("link", { name: "Ver respuesta" })).toHaveAttribute("href", "#task-result-result");
    expect(screen.queryByText("Resultado público")).not.toBeInTheDocument();
    expect((await axe.run(container)).violations).toHaveLength(0);
  });

  it("does not mistake incomplete or unrelated assistant messages for a task response", () => {
    const taskOrigin = { kind: "background_task" as const, taskId: snapshot.items[0].id };
    const messages: ChatMessage[] = [
      { ...human, id: "ordinary", streaming: false },
      { ...human, id: "still-writing", controlTurnOrigin: taskOrigin },
      { ...human, id: "empty", content: " \n ", streaming: false, controlTurnOrigin: taskOrigin },
      { ...human, id: "tools-only", content: "", tools: [{ id: "lookup", name: "lookup", label: "Lookup", summary: "Progress", status: "completed" }], streaming: false, controlTurnOrigin: taskOrigin },
      { ...human, id: "another-task", streaming: false, controlTurnOrigin: { ...taskOrigin, taskId: "other-task" } },
    ];
    render(<BackgroundTaskList snapshot={{ ...snapshot, activeCount: 0, pendingDeliveryCount: 1, items: [{ ...snapshot.items[0], state: "completed" }] }} messages={messages} />);
    fireEvent.click(screen.getByText("Tareas de esta conversación"));
    expect(screen.queryByRole("link", { name: "Ver respuesta" })).not.toBeInTheDocument();
    expect(screen.getByText("Respuesta pendiente de entrega.")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("1 entregas pendientes");
  });

  it.each(["completed", "failed", "cancelled"] as const)("keeps the finished response available for a %s task when a later stream is incomplete", (state) => {
    const result: ChatMessage = { ...human, id: "finished", streaming: false, controlTurnOrigin: { kind: "background_task", taskId: snapshot.items[0].id } };
    render(<BackgroundTaskList snapshot={{ ...snapshot, activeCount: 0, items: [{ ...snapshot.items[0], state, deliveryState: "delivered" }] }} messages={[result, { ...result, id: "later-partial", streaming: true }]} />);
    fireEvent.click(screen.getByText("Tareas de esta conversación"));
    expect(screen.getByRole("link", { name: "Ver respuesta" })).toHaveAttribute("href", "#task-result-finished");
    expect(screen.queryByText("Confirmación de entrega pendiente.")).not.toBeInTheDocument();
  });

  it("allows a finished public audio response without requiring text", () => {
    const audio: ChatMessage = { ...human, id: "audio-response", streaming: false, content: "", media: [{ id: "audio", kind: "audio", mediaType: "audio/mpeg" }], controlTurnOrigin: { kind: "background_task", taskId: snapshot.items[0].id } };
    render(<BackgroundTaskList snapshot={snapshot} messages={[audio]} />);
    fireEvent.click(screen.getByText("Tareas de esta conversación"));
    expect(screen.getByRole("link", { name: "Ver respuesta" })).toHaveAttribute("href", "#task-result-audio-response");
  });

  it("navigates to the response without acknowledging or clearing native pending delivery", () => {
    const result: ChatMessage = { ...human, id: "public-result", streaming: false, controlTurnOrigin: { kind: "background_task", taskId: snapshot.items[0].id } };
    useAppStore.getState().setBackgroundTasks("conversation", { ...snapshot, activeCount: 0, pendingDeliveryCount: 1, items: [{ ...snapshot.items[0], state: "completed" }] });
    useAppStore.setState({ messages: [result] });
    const nativeSnapshot = useAppStore.getState().backgroundTasksBySession.conversation;
    const submit = vi.spyOn(api, "submitPrompt");
    render(<><BackgroundTasks sessionId="conversation" /><article id={taskResultAnchor(result.id)} tabIndex={-1}>Resultado público</article></>);
    const target = screen.getByText("Resultado público");
    const scrollIntoView = vi.fn();
    Object.defineProperty(target, "scrollIntoView", { configurable: true, value: scrollIntoView });
    fireEvent.click(screen.getByText("Tareas de esta conversación"));
    fireEvent.click(screen.getByRole("link", { name: "Ver respuesta" }));
    expect(scrollIntoView).toHaveBeenCalledWith({ block: "center", behavior: "smooth" });
    expect(target).toHaveFocus();
    expect(useAppStore.getState().backgroundTasksBySession.conversation).toBe(nativeSnapshot);
    expect(nativeSnapshot.pendingDeliveryCount).toBe(1);
    expect(nativeSnapshot.items[0].deliveryState).toBe("pending");
    expect(screen.getByText("Confirmación de entrega pendiente.")).toBeVisible();
    expect(submit).not.toHaveBeenCalled();
  });
});
