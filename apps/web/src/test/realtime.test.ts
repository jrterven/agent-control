import { beforeEach, describe, expect, it } from "vitest";
import { profiles } from "../data";
import {
  applyRealtimeEvent,
  clearStreamingContentFilterState,
  streamingContentFilterStatsForTests,
} from "../hooks";
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
    clearStreamingContentFilterState();
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

  it("attaches bounded same-origin email references to the active response", () => {
    const previewUrl = "/api/v1/sessions/session-a/email-references/0123456789abcdef0123456789abcdef";
    expect(applyRealtimeEvent(event("message.delta", {
      delta: "Encontré un correo importante.",
      controlEmailReferences: [{
        schemaVersion: 1,
        id: "0123456789abcdef0123456789abcdef",
        provider: "gmail",
        senderName: "Google Ads",
        subject: "Verifica tu cuenta",
        previewUrl,
        openUrl: `${previewUrl}/open`,
        openMode: "search",
      }, {
        schemaVersion: 1,
        id: "fedcba9876543210fedcba9876543210",
        provider: "imap",
        subject: "Correo de Hostinger",
        previewUrl: "/api/v1/sessions/session-a/email-references/fedcba9876543210fedcba9876543210",
      }, {
        schemaVersion: 1,
        id: "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        provider: "outlook",
        subject: "Ruta rechazada",
        previewUrl: "https://attacker.example/preview",
      }],
    }))).toBe(true);

    expect(useAppStore.getState().messages[0].emailReferences).toEqual([{
      schemaVersion: 1,
      id: "0123456789abcdef0123456789abcdef",
      provider: "gmail",
      senderName: "Google Ads",
      subject: "Verifica tu cuenta",
      previewUrl,
      openUrl: `${previewUrl}/open`,
      openMode: "search",
    }, {
      schemaVersion: 1,
      id: "fedcba9876543210fedcba9876543210",
      provider: "imap",
      subject: "Correo de Hostinger",
      previewUrl: "/api/v1/sessions/session-a/email-references/fedcba9876543210fedcba9876543210",
    }]);
  });

  it("never flashes a mail-reference transport marker split across deltas", () => {
    const marker = "<!-- hermes-control-email-reference-v1";
    for (let length = 1; length < marker.length; length += 1) {
      const messageId = `assistant-boundary-${length}`;
      useAppStore.setState({
        messages: [{ ...assistant, id: messageId, content: "" }],
        streamingBySession: { "session-a": messageId },
        pendingOperations: { "operation-a": messageId },
      });
      applyRealtimeEvent(event("message.delta", { delta: `Respuesta visible.\n\n${marker.slice(0, length)}` }));
      expect(useAppStore.getState().messages[0].content, `marker boundary ${length}`).toBe("Respuesta visible.");
    }

    useAppStore.setState({
      messages: [{ ...assistant, content: "" }],
      streamingBySession: { "session-a": assistant.id },
      pendingOperations: { "operation-a": assistant.id },
    });
    useAppStore.getState().updateMessage(assistant.id, { content: "" });
    applyRealtimeEvent(event("message.delta", { delta: "Respuesta visible." }));
    applyRealtimeEvent(event("message.delta", { delta: "\n\n<!-- hermes-control-email-ref" }));
    expect(useAppStore.getState().messages[0].content).toBe("Respuesta visible.");

    applyRealtimeEvent(event("message.delta", {
      delta: "erence-v1 {\"provider\":\"gmail\",\"subject\":\"Privado\"} -->",
    }));
    expect(useAppStore.getState().messages[0].content).toBe("Respuesta visible.");
    expect(JSON.stringify(useAppStore.getState().messages[0])).not.toContain("hermes-control-email-reference");
  });

  it("filters mixed-case, whitespace-split markers with tiny bounded state", () => {
    useAppStore.getState().updateMessage(assistant.id, { content: "" });
    applyRealtimeEvent(event("message.delta", { delta: "Respuesta visible.\n\n<!" }));
    applyRealtimeEvent(event("message.delta", { delta: "--   HeRmEs-CoNtRoL-EmAiL-ReFeReNcE-v1" }));
    applyRealtimeEvent(event("message.delta", { delta: ` ${"payload-privado ".repeat(20_000)}` }));

    expect(useAppStore.getState().messages[0].content).toBe("Respuesta visible.");
    expect(streamingContentFilterStatsForTests()).toEqual({
      entries: 1,
      tombstones: 0,
      quarantinedSessions: 0,
      globalQuarantine: false,
      retainedCharacters: 0,
      largestPendingCharacters: 0,
    });

    applyRealtimeEvent(event("message.delta", { delta: "-->Texto público posterior." }));
    expect(useAppStore.getState().messages[0].content).toBe("Respuesta visible.Texto público posterior.");
    expect(streamingContentFilterStatsForTests().entries).toBe(0);

    for (let index = 0; index < 40; index += 1) {
      const messageId = `assistant-filter-bound-${index}`;
      useAppStore.setState({
        messages: [{ ...assistant, id: messageId, content: "" }],
        streamingBySession: { [`session-${index}`]: messageId },
        pendingOperations: { [`operation-${index}`]: messageId },
      });
      applyRealtimeEvent({
        type: "message.delta",
        correlationId: `operation-${index}`,
        controlSessionId: `session-${index}`,
        data: { delta: "\n\n<" },
      });
    }
    const bounded = streamingContentFilterStatsForTests();
    expect(bounded.entries).toBe(32);
    expect(bounded.tombstones).toBe(8);
    expect(bounded.largestPendingCharacters).toBeLessThanOrEqual(65);
    expect(bounded.retainedCharacters).toBeLessThanOrEqual(32 * 65);

    const evictedMessageId = "assistant-filter-bound-0";
    useAppStore.setState({
      messages: [{ ...assistant, id: evictedMessageId, sessionId: "session-0", content: "" }],
      streamingBySession: { "session-0": evictedMessageId },
      pendingOperations: { "operation-0": evictedMessageId },
    });
    applyRealtimeEvent({
      type: "message.delta",
      correlationId: "operation-0",
      controlSessionId: "session-0",
      data: { delta: "PRIVATE TAIL -->texto que también queda en cuarentena" },
    });
    expect(useAppStore.getState().messages[0].content).toBe("");
    applyRealtimeEvent({
      type: "message.completed",
      correlationId: "operation-0",
      controlSessionId: "session-0",
    });
    expect(streamingContentFilterStatsForTests().tombstones).toBe(7);

    const currentMessageId = "assistant-filter-bound-39";
    useAppStore.setState({
      messages: [{ ...assistant, id: currentMessageId, sessionId: "session-39" }],
      streamingBySession: { "session-39": currentMessageId },
      pendingOperations: { "operation-39": currentMessageId },
    });
    applyRealtimeEvent({
      type: "message.completed",
      correlationId: "operation-39",
      controlSessionId: "session-39",
    });
    expect(streamingContentFilterStatsForTests().entries).toBe(31);
    expect(useAppStore.getState().messages.find((message) => message.id === currentMessageId)?.streaming).toBe(false);

    clearStreamingContentFilterState();
    expect(streamingContentFilterStatsForTests()).toMatchObject({
      entries: 0,
      tombstones: 0,
      quarantinedSessions: 0,
      globalQuarantine: false,
    });
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
