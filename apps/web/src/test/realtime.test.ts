import { beforeEach, describe, expect, it } from "vitest";
import { profiles } from "../data";
import { applyRealtimeEvent } from "../hooks";
import { useAppStore } from "../store/appStore";
import type { ChatMessage, RealtimeEvent } from "../types";

const assistant: ChatMessage = {
  id: "assistant-local", sessionId: "session-a", role: "assistant", content: "Respuesta", createdAt: "10:00", streaming: true,
  tools: [{ id: "tool-active", name: "search", label: "Búsqueda", status: "running", summary: "Iniciando" }],
};

function event(type: string, data: Record<string, unknown> = {}): RealtimeEvent {
  return { type, correlationId: "operation-a", controlSessionId: "session-a", data };
}

describe("normalized realtime events", () => {
  beforeEach(() => {
    useAppStore.setState({
      messages: [structuredClone(assistant)],
      sessionUsageById: {},
      streamingBySession: { "session-a": assistant.id },
      pendingOperations: { "operation-a": assistant.id },
      profiles,
      selectedGatewayId: "gateway-home",
      selectedProfileId: "profile-newton",
      connection: "connected",
    });
  });

  it("reuses an active tool for progress without a tool id and supports terminal tool spellings", () => {
    applyRealtimeEvent(event("tool.progress", { name: "search", summary: "3 de 5" }));
    let tools = useAppStore.getState().messages[0].tools ?? [];
    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({ id: "tool-active", status: "running", summary: "3 de 5" });

    applyRealtimeEvent(event("tool.complete", { name: "search", summary: "5 resultados" }));
    tools = useAppStore.getState().messages[0].tools ?? [];
    expect(tools).toHaveLength(1);
    expect(tools[0]).toMatchObject({ id: "tool-active", status: "completed" });

    useAppStore.getState().updateMessage(assistant.id, { tools: [{ ...tools[0], status: "running" }] });
    applyRealtimeEvent(event("tool.error", { name: "search", summary: "Sin conexión" }));
    expect(useAppStore.getState().messages[0].tools?.[0]).toMatchObject({ id: "tool-active", status: "failed" });
    expect(useAppStore.getState().messages[0].activity).toMatchObject([
      { kind: "tool", status: "running", summary: "3 de 5" },
      { kind: "tool", status: "completed", summary: "5 resultados" },
      { kind: "tool", status: "failed", summary: "Sin conexión" },
    ]);
  });

  it("keeps private reasoning opaque and bounds the public activity trace", () => {
    expect(applyRealtimeEvent(event("reasoning.omitted", {
      omitted: true,
      delta: "PRIVATE-COT-MUST-NOT-REACH-THE-TRACE",
    }))).toBe(true);
    expect(useAppStore.getState().messages[0].activity).toBeUndefined();
    expect(JSON.stringify(useAppStore.getState().messages[0])).not.toContain("PRIVATE-COT");

    for (let index = 0; index < 90; index += 1) {
      applyRealtimeEvent({
        ...event("tool.progress", {
          name: `tool-${index}`,
          label: `Herramienta ${index}`,
          summary: `${index} ${"detalle ".repeat(80)}`,
        }),
        eventId: `activity-${index}`,
      });
    }

    const activity = useAppStore.getState().messages[0].activity ?? [];
    expect(activity).toHaveLength(80);
    expect(activity[0].id).toBe("activity-10");
    expect(activity.at(-1)?.summary?.length).toBeLessThanOrEqual(240);
  });

  it.each(["message.error", "run.interrupted", "interrupted"])("treats %s as a terminal stream event", (type) => {
    expect(applyRealtimeEvent(event(type))).toBe(true);
    const state = useAppStore.getState();
    expect(state.messages[0].streaming).toBe(false);
    expect(state.streamingBySession["session-a"]).toBeUndefined();
    expect(state.pendingOperations["operation-a"]).toBeUndefined();
  });

  it("clears every pending binding for the resolved message when a terminal event omits correlationId", () => {
    useAppStore.setState({
      pendingOperations: { "request-key": assistant.id, "upstream-key": assistant.id, unrelated: "other-message" },
    });

    expect(applyRealtimeEvent({ type: "message.completed", controlSessionId: "session-a" })).toBe(true);

    expect(useAppStore.getState().pendingOperations).toEqual({ unrelated: "other-message" });
  });

  it("applies global connection events only to the selected gateway/profile route", () => {
    expect(applyRealtimeEvent({
      type: "control.connection",
      gatewayId: "gateway-home",
      profileName: "default",
      data: { state: "offline" },
    })).toBe(true);
    expect(useAppStore.getState().connection).toBe("offline");

    useAppStore.getState().setConnection("connected");
    expect(applyRealtimeEvent({
      type: "control.connection",
      gatewayId: "gateway-home",
      profileName: "jarvis",
      data: { state: "offline" },
    })).toBe(true);
    expect(useAppStore.getState().connection).toBe("connected");
  });

  it("records a bounded session usage snapshot without requiring a streaming message", () => {
    useAppStore.setState({ messages: [], streamingBySession: {}, pendingOperations: {} });

    expect(applyRealtimeEvent({
      type: "session.usage",
      controlSessionId: "session-a",
      occurredAt: "2026-08-28T18:30:00Z",
      data: {
        usage: {
          input: 12_000,
          output: 800,
          total: 12_800,
          calls: 4,
          context_used: 53_248,
          context_max: 128_000,
          context_percent: 41.6,
          reasoning: "PRIVATE-CONTENT",
          model: "private-model",
          negative: -1,
        },
      },
    })).toBe(true);

    expect(useAppStore.getState().sessionUsageById["session-a"]).toEqual({
      inputTokens: 12_000,
      outputTokens: 800,
      totalTokens: 12_800,
      apiCalls: 4,
      contextUsed: 53_248,
      contextMax: 128_000,
      contextPercent: 41.6,
      reportedAt: "2026-08-28T18:30:00Z",
    });
  });

  it("keeps telemetry from a terminal message even when its stream binding is absent", () => {
    useAppStore.setState({ messages: [], streamingBySession: {}, pendingOperations: {} });

    expect(applyRealtimeEvent({
      type: "message.complete",
      controlSessionId: "session-a",
      data: { usage: { total: 900, context_used: 700, context_max: 4_000 } },
    })).toBe(true);
    expect(useAppStore.getState().sessionUsageById["session-a"]).toEqual({
      totalTokens: 900,
      contextUsed: 700,
      contextMax: 4_000,
    });
  });
});
