import { beforeEach, describe, expect, it } from "vitest";
import { useAppStore } from "../store/appStore";
import type { BootstrapData, ChatMessage, SessionSummary } from "../types";

const message = (id: string, sessionId: string, update: Partial<ChatMessage> = {}): ChatMessage => ({
  id, sessionId, role: "assistant", content: id, createdAt: "10:00", ...update,
});

describe("authentication lifetime", () => {
  it("preserves pending work across verified CSRF renewal and invalidates it across authentication changes", () => {
    useAppStore.getState().resetPrivateState();
    useAppStore.getState().setAuth("authenticated", "Owner A", "old-csrf", false, "owner-a");
    const generation = useAppStore.getState().authGeneration;
    useAppStore.setState({ csrfToken: "fresh-csrf" });
    expect(useAppStore.getState().authGeneration).toBe(generation);
    useAppStore.getState().setAuth("authenticated", "Owner A", "fresh-csrf", false, "owner-a");
    expect(useAppStore.getState().authGeneration).toBe(generation);

    useAppStore.getState().setAuth("unauthenticated");
    useAppStore.getState().setAuth("authenticated", "Owner A", "new-login-csrf", false, "owner-a");
    expect(useAppStore.getState().authGeneration).toBe(generation + 2);
    useAppStore.getState().resetPrivateState(true);
    expect(useAppStore.getState().authGeneration).toBe(generation + 3);
  });
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

  it("purges several deleted-agent sessions and preserves unrelated chat state", () => {
    const removedA = { ...session, id: "session-removed-a", workspaceId: "workspace-a" };
    const removedB = { ...session, id: "session-removed-b", workspaceId: "workspace-a" };
    const retained = { ...session, id: "session-retained", workspaceId: "workspace-a" };
    useAppStore.setState({
      sessions: [removedA, removedB, retained],
      selectedSessionId: removedA.id,
      selectedProfileId: session.profileId,
      selectedWorkspaceId: "workspace-a",
      workspaces: [{ id: "workspace-a", name: "Trabajo", description: "", sessionCount: 3, updatedAt: "ahora" }],
      messages: [message("removed-message", removedA.id), message("retained-message", retained.id)],
      streamingBySession: { [removedB.id]: "removed-stream", [retained.id]: "retained-stream" },
      sessionUsageById: { [removedA.id]: { totalTokens: 10 }, [retained.id]: { totalTokens: 20 } },
      pendingOperations: { "removed-operation": "removed-message", "retained-operation": "retained-message" },
    });

    useAppStore.getState().removeSessions([removedA.id, removedB.id]);

    const state = useAppStore.getState();
    expect(state.sessions.map((item) => item.id)).toEqual([retained.id]);
    expect(state.messages.map((item) => item.id)).toEqual(["retained-message"]);
    expect(state.selectedSessionId).toBe(retained.id);
    expect(state.streamingBySession).toEqual({ [retained.id]: "retained-stream" });
    expect(state.sessionUsageById).toEqual({ [retained.id]: { totalTokens: 20 } });
    expect(state.pendingOperations).toEqual({ "retained-operation": "retained-message" });
    expect(state.workspaces[0].sessionCount).toBe(1);
  });
});

describe("bootstrap selection reconciliation", () => {
  const bootstrap: BootstrapData = {
    gateways: [{
      id: "gateway-remote",
      name: "Gateway remoto",
      location: "Túnel privado",
      status: "connected",
      version: "0.20.5",
      sha: null,
      capabilities: {
        realtime: true,
        sessions: true,
        prompts: true,
        interrupt: true,
        cron: true,
        profiles: true,
        config: false,
        memory: false,
      },
    }],
    profiles: [{
      id: "profile-jarvis",
      gatewayId: "gateway-remote",
      technicalName: "jarvis",
      displayName: "Jarvis",
      model: "gpt-test",
      status: "ready",
      mutable: false,
    }],
    workspaces: [{
      id: "workspace-papers",
      name: "Papers",
      description: "",
      sessionCount: 1,
      updatedAt: "ahora",
    }],
    sessions: [
      {
        id: "automation-session",
        profileId: "profile-jarvis",
        storedSessionId: "automation-stored",
        title: "Automatización de hoy",
        preview: "",
        updatedAt: "2026-11-07T12:00:00Z",
      },
      {
        id: "workspace-session",
        workspaceId: "workspace-papers",
        profileId: "profile-jarvis",
        storedSessionId: "workspace-stored",
        title: "Chat en Papers",
        preview: "",
        updatedAt: "2026-11-06T12:00:00Z",
      },
    ],
    automations: [],
  };

  beforeEach(() => {
    useAppStore.getState().resetPrivateState();
  });

  it("preserves a no-workspace automation session during background refresh", () => {
    useAppStore.setState({
      bootstrapLoaded: true,
      selectedGatewayId: "gateway-remote",
      selectedProfileId: "profile-jarvis",
      selectedWorkspaceId: "",
      selectedSessionId: "automation-session",
    });

    useAppStore.getState().hydrateBootstrap(bootstrap);

    expect(useAppStore.getState().selectedWorkspaceId).toBe("");
    expect(useAppStore.getState().selectedSessionId).toBe("automation-session");
  });

  it("restores a no-workspace conversation during the first bootstrap after an app update", () => {
    useAppStore.setState({
      bootstrapLoaded: false,
      selectedGatewayId: "gateway-remote",
      selectedProfileId: "profile-jarvis",
      selectedWorkspaceId: "",
      selectedSessionId: "automation-session",
    });

    useAppStore.getState().hydrateBootstrap(bootstrap);

    expect(useAppStore.getState().selectedWorkspaceId).toBe("");
    expect(useAppStore.getState().selectedSessionId).toBe("automation-session");
  });

  it("ignores input order and pins, selects the latest chat's workspace, and keeps explicit selections on refresh", () => {
    const projection = { ...bootstrap, sessions: [
      { ...bootstrap.sessions[0], pinnedAt: "2026-11-07T13:00:00Z" },
      { ...bootstrap.sessions[1], updatedAt: "2026-11-07T14:00:00Z" },
      { ...bootstrap.sessions[0], id: "other-agent", profileId: "other", updatedAt: "2026-11-08T14:00:00Z" },
    ] };
    useAppStore.getState().hydrateBootstrap(projection);
    expect(useAppStore.getState()).toMatchObject({ selectedSessionId: "workspace-session", selectedWorkspaceId: "workspace-papers" });
    useAppStore.getState().selectSession("automation-session");
    useAppStore.getState().hydrateBootstrap(projection);
    expect(useAppStore.getState().selectedSessionId).toBe("automation-session");
  });

  it.each(["profile", "gateway"])("opens the latest chat across workspaces when selecting a %s", (route) => {
    useAppStore.getState().hydrateBootstrap(bootstrap);
    useAppStore.getState().selectWorkspace("workspace-papers");
    if (route === "profile") useAppStore.getState().selectProfile("profile-jarvis");
    else useAppStore.getState().selectGateway("gateway-remote");
    expect(useAppStore.getState()).toMatchObject({ selectedSessionId: "automation-session", selectedWorkspaceId: "" });
  });

  it("keeps a new chat empty during bootstrap refresh", () => {
    useAppStore.getState().prepareChat();
    useAppStore.getState().hydrateBootstrap(bootstrap);
    expect(useAppStore.getState().selectedSessionId).toBe("");
  });

  it("opens the latest chat across workspaces during the initial bootstrap", () => {
    useAppStore.getState().hydrateBootstrap(bootstrap);

    expect(useAppStore.getState().selectedWorkspaceId).toBe("");
    expect(useAppStore.getState().selectedSessionId).toBe("automation-session");
  });
});
