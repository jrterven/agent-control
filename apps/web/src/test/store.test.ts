import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../store/appStore";
import type { ChatMessage, SessionSummary } from "../types";

const message = (id: string, sessionId: string, update: Partial<ChatMessage> = {}): ChatMessage => ({
  id, sessionId, role: "assistant", content: id, createdAt: "10:00", ...update,
});

describe("session history reconciliation", () => {
  beforeEach(() => {
    useAppStore.setState({
      messages: [
        message("local-complete", "session-a"),
        message("local-user-sent", "session-a", { role: "user", delivery: "sent" }),
        message("local-user-sending", "session-a", { role: "user", delivery: "sending" }),
        message("local-ambiguous", "session-a", { role: "user", delivery: "ambiguous" }),
        message("local-streaming", "session-a", { streaming: true }),
        message("local-operation", "session-a"),
        message("other-session", "session-b"),
      ],
      streamingBySession: { "session-a": "local-streaming" },
      pendingOperations: { "operation-a": "local-operation" },
    });
  });

  it("replaces completed local UUIDs while preserving only unresolved messages", () => {
    const history = [
      message("remote-user", "session-a", { role: "user", delivery: "sent" }),
      message("remote-assistant", "session-a"),
    ];

    useAppStore.getState().setMessagesForSession("session-a", history);

    const state = useAppStore.getState();
    expect(state.messages.map((item) => item.id)).toEqual([
      "other-session",
      "remote-user",
      "remote-assistant",
      "local-user-sending",
      "local-ambiguous",
      "local-streaming",
      "local-operation",
    ]);
    expect(state.messages.find((item) => item.id === "local-complete")).toBeUndefined();
    expect(state.messages.find((item) => item.id === "local-user-sent")).toBeUndefined();
  });
});

describe("session usage snapshots", () => {
  const session: SessionSummary = {
    id: "session-a",
    profileId: "profile-a",
    storedSessionId: "stored-a",
    title: "Sesión A",
    preview: "",
    updatedAt: "ahora",
  };

  beforeEach(() => {
    useAppStore.getState().resetPrivateState();
    useAppStore.setState({ sessions: [session], selectedSessionId: session.id });
  });

  it("replaces authoritative snapshots and removes them with their session", () => {
    useAppStore.getState().setSessionUsage(session.id, { totalTokens: 1_200, contextUsed: 800 });
    useAppStore.getState().setSessionUsage(session.id, { apiCalls: 3 });
    expect(useAppStore.getState().sessionUsageById[session.id]).toEqual({ apiCalls: 3 });

    useAppStore.getState().removeSession(session.id);
    expect(useAppStore.getState().sessionUsageById[session.id]).toBeUndefined();
  });
});
