import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { applyRealtimeEvent, continueIncompleteResponse, rehydrateSession, respondToApproval } from "../hooks";
import { api, ApiError } from "../lib/api";
import * as drafts from "../lib/db";
import { profiles, sessions } from "../data";
import { useAppStore } from "../store/appStore";

describe("PWA background operation recovery", () => {
  beforeEach(() => {
    useAppStore.setState({
      authState: "authenticated",
      csrfToken: "csrf-background",
      demoMode: false,
      messages: [],
      streamingBySession: {},
      pendingOperations: {},
      connection: "connected",
      approvalsBySession: {},
      clarificationsBySession: {},
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it.each(["", " \n\t", "\u200b", "\u200e\u2060\ufeff\uFE0F"])("recovers a stopped but invisible answer (%j) without replaying the prompt", async (content) => {
    vi.spyOn(api, "sessionHistory").mockResolvedValue({
      items: [
        { id: "sensor-question", role: "user", content: "¿Qué es este sensor?", controlAttachments: [{ kind: "image", name: "sensor.jpg", mediaType: "image/jpeg", size: 128, mediaId: "a".repeat(32) }] },
        { id: "vision-call", role: "assistant", content: "", finish_reason: "tool_calls", tool_calls: [{ id: "vision", name: "vision_analyze" }] },
        { id: "vision-result", role: "tool", tool_name: "vision_analyze", tool_call_id: "vision", content: "Image already in context" },
        { id: "invisible-answer", role: "assistant", content, finish_reason: "stop" },
      ], sessionStatus: "ready", activeOperation: null, activeTurnId: null,
    });
    const submit = vi.spyOn(api, "submitPrompt");
    await rehydrateSession("sensor-session");
    await rehydrateSession("sensor-session");
    const messages = useAppStore.getState().messages;
    expect(messages.filter((message) => message.responseIncomplete)).toHaveLength(1);
    expect(messages.at(-1)).toMatchObject({ responseIncomplete: true, content: expect.stringContaining("sin entregar") });
    expect(messages.some((message) => message.id === "invisible-answer")).toBe(false);
    expect(messages.find((message) => message.id === "sensor-question")?.attachments?.[0].mediaId).toBe("a".repeat(32));
    expect(messages.some((message) => message.tools?.[0].name === "vision_analyze")).toBe(true);
    expect(submit).not.toHaveBeenCalled();
  });

  it.each([
    { content: "👩‍💻", tool_calls: [] },
    { content: "", controlMedia: [{ id: "b".repeat(32), kind: "audio", mediaType: "audio/mpeg" }] },
    { content: "", controlMedia: [{ id: "b".repeat(32), kind: "image", mediaType: "image/jpeg" }] },
  ])("keeps visible and media-only answers after resume: %j", async (answer) => {
    vi.spyOn(api, "sessionHistory").mockResolvedValue({ items: [
      { id: "user", role: "user", content: "Responde" },
      { id: "answer", role: "assistant", finish_reason: "stop", ...answer },
    ], sessionStatus: "ready", activeOperation: null });
    await rehydrateSession("visible-session");
    expect(useAppStore.getState().messages.some((message) => message.responseIncomplete)).toBe(false);
    expect(useAppStore.getState().messages.find((message) => message.id === "answer")).toBeDefined();
  });

  it("does not show a terminal notice while a native turn is still active", async () => {
    vi.spyOn(api, "sessionHistory").mockResolvedValue({ items: [
      { id: "user", role: "user", content: "Responde" },
      { id: "answer", role: "assistant", content: "\u200b", finish_reason: "stop" },
    ], sessionStatus: "streaming", activeTurnId: "native-turn", activeOperation: null });
    await rehydrateSession("active-invisible-session");
    expect(useAppStore.getState().messages.some((message) => message.responseIncomplete)).toBe(false);
  });

  it("continues only the selected incomplete answer once, preserves the draft and does not re-upload attachments", async () => {
    useAppStore.setState({ profiles: profiles.map((profile) => ({ ...profile, mutable: true })), sessions, selectedProfileId: "profile-newton", selectedSessionId: "session-papers", runtimeTurnBySession: {},
      messages: [{ id: "missing-answer", sessionId: "session-papers", role: "assistant", content: "No answer", createdAt: "", responseIncomplete: true }],
    });
    const clearDraft = vi.spyOn(drafts, "clearDraft");
    const submit = vi.spyOn(api, "submitPrompt").mockResolvedValue({ operationId: "continued-op", status: "accepted" });
    const first = continueIncompleteResponse("missing-answer");
    await continueIncompleteResponse("missing-answer");
    await first;
    expect(submit).toHaveBeenCalledTimes(1);
    expect(submit).toHaveBeenCalledWith("session-papers", expect.stringContaining("No repitas acciones externas"), expect.any(String), "csrf-background");
    expect(clearDraft).not.toHaveBeenCalled();
    useAppStore.setState({ selectedSessionId: "other-session" });
    await continueIncompleteResponse("missing-answer");
    expect(submit).toHaveBeenCalledTimes(1);
  });

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

  it("shows an explicit interruption when tools ran but no final answer survived", async () => {
    const history = vi.spyOn(api, "sessionHistory").mockResolvedValue({
      items: [
        { id: "interrupted-user", role: "user", content: "Agenda dos reuniones" },
        {
          id: "assistant-tool-stub",
          role: "assistant",
          content: "Voy a usar el calendario.",
          finish_reason: "tool_calls",
          tool_calls: [{ id: "calendar-call", name: "calendar.create" }],
        },
        {
          id: "calendar-result",
          role: "tool",
          tool_call_id: "calendar-call",
          tool_name: "calendar.create",
          content: "Primer paso registrado",
        },
      ],
      sessionStatus: "interrupted",
      activeOperation: null,
    });

    await rehydrateSession("session-interrupted");

    const messages = useAppStore.getState().messages.filter((message) => (
      message.sessionId === "session-interrupted"
    ));
    expect(messages).toEqual(expect.arrayContaining([
      expect.objectContaining({
        role: "assistant",
        content: "Voy a usar el calendario.",
      }),
      expect.objectContaining({
        role: "assistant",
        content: expect.stringContaining("se interrumpió antes"),
      }),
    ]));
    expect(useAppStore.getState().streamingBySession["session-interrupted"]).toBeUndefined();
  });

  it("reattaches a stale runtime without resending the prompt", async () => {
    const history = vi.spyOn(api, "sessionHistory")
      .mockResolvedValueOnce({
        items: [
          { id: "restart-user", role: "user", content: "Agenda dos reuniones" },
          {
            id: "restart-tool-stub",
            role: "assistant",
            content: "",
            finish_reason: "tool_calls",
          },
        ],
        sessionStatus: "streaming",
        activeOperation: {
          operationId: "operation-restart",
          status: "streaming",
          recoveryRequired: true,
        },
      })
      .mockResolvedValueOnce({
        items: [
          { id: "restart-user", role: "user", content: "Agenda dos reuniones" },
          {
            id: "restart-tool-stub",
            role: "assistant",
            content: "",
            finish_reason: "tool_calls",
          },
        ],
        sessionStatus: "interrupted",
        activeOperation: null,
      });
    const resume = vi.spyOn(api, "resumeSession").mockResolvedValue({
      id: "session-restart",
      gatewayId: "gateway-a",
      profileName: "jarvis",
      profileId: "profile-jarvis",
      storedSessionId: "stored-restart",
      title: "Jarvis",
      preview: "",
      updatedAt: "2026-08-31T13:02:00Z",
      archived: false,
    });
    const submit = vi.spyOn(api, "submitPrompt");

    await rehydrateSession("session-restart");

    expect(resume).toHaveBeenCalledTimes(1);
    expect(resume).toHaveBeenCalledWith("session-restart", "csrf-background");
    expect(history).toHaveBeenCalledTimes(2);
    expect(submit).not.toHaveBeenCalled();
    expect(useAppStore.getState().messages).toEqual(expect.arrayContaining([
      expect.objectContaining({
        sessionId: "session-restart",
        role: "assistant",
        content: expect.stringContaining("se interrumpió antes"),
      }),
    ]));
  });

  it("restores an approval emitted before the websocket reconnects", async () => {
    const history = vi.spyOn(api, "sessionHistory")
      .mockResolvedValueOnce({
        items: [{ id: "approval-user", role: "user", content: "Hazlo" }],
        sessionStatus: "streaming",
        activeOperation: {
          operationId: "operation-approval",
          status: "streaming",
          recoveryRequired: true,
        },
        pendingInteractions: [],
      })
      .mockResolvedValueOnce({
        items: [{ id: "approval-user", role: "user", content: "Hazlo" }],
        sessionStatus: "waiting",
        activeOperation: {
          operationId: "operation-approval",
          status: "streaming",
          recoveryRequired: false,
        },
        pendingInteractions: [
          {
            type: "approval.request",
            data: {
              request_id: "approval-after-resume",
              command: "calendar.create --safe",
              choices: ["once", "deny"],
            },
          },
        ],
      });
    const resume = vi.spyOn(api, "resumeSession").mockResolvedValue({
      id: "session-approval",
      gatewayId: "gateway-a",
      profileName: "jarvis",
      profileId: "profile-jarvis",
      storedSessionId: "stored-approval",
      title: "Jarvis",
      preview: "",
      updatedAt: "2026-08-31T13:03:00Z",
      archived: false,
    });
    const submit = vi.spyOn(api, "submitPrompt");

    await rehydrateSession("session-approval");

    expect(resume).toHaveBeenCalledTimes(1);
    expect(history).toHaveBeenCalledTimes(2);
    expect(submit).not.toHaveBeenCalled();
    expect(useAppStore.getState().approvalsBySession["session-approval"]).toEqual([
      expect.objectContaining({
        requestId: "approval-after-resume",
        command: "calendar.create --safe",
        choices: ["once", "deny"],
        state: "pending",
      }),
    ]);
  });

  it("does not erase an approval while its response outcome is unknown", async () => {
    useAppStore.setState({
      profiles: [{
        id: "profile-approval",
        gatewayId: "gateway-a",
        technicalName: "jarvis",
        displayName: "Jarvis",
        status: "ready",
        model: "test-model",
        mutable: true,
        capabilities: {
          realtime: true,
          sessions: true,
          prompts: true,
          interrupt: true,
          approvals: true,
          cron: false,
          profiles: true,
          config: false,
          memory: false,
        },
        capabilitySet: {
          protocol: "dashboard-rpc",
          version: "test",
          sourceSha: "test",
          methods: ["approval.respond"],
          features: [],
        },
      }],
      sessions: [{
        id: "session-mutation-race",
        gatewayId: "gateway-a",
        profileName: "jarvis",
        profileId: "profile-approval",
        storedSessionId: "stored-mutation-race",
        title: "Jarvis",
        preview: "",
        updatedAt: "2026-08-31T13:04:00Z",
      }],
      approvalsBySession: {
        "session-mutation-race": [{
          requestId: "approval-mutation-race",
          sessionId: "session-mutation-race",
          command: "calendar.create --safe",
          description: "",
          choices: ["once", "deny"],
          patternKeys: [],
          allowSession: true,
          allowPermanent: false,
          smartDenied: false,
          state: "pending",
        }],
      },
    });
    let rejectResponse!: (error: Error) => void;
    vi.spyOn(api, "respondApproval").mockImplementation(() => new Promise((_resolve, reject) => {
      rejectResponse = reject;
    }));
    const history = vi.spyOn(api, "sessionHistory").mockResolvedValue({
      items: [{ id: "race-user", role: "user", content: "Hazlo" }],
      sessionStatus: "waiting",
      activeOperation: null,
      // The backend temporarily removes the route-bound claim while dispatching
      // the approval response, so this absence is not yet proof of resolution.
      pendingInteractions: [],
    });

    const mutation = respondToApproval(
      "session-mutation-race",
      "approval-mutation-race",
      "once",
    );
    expect(useAppStore.getState().approvalsBySession["session-mutation-race"][0].state).toBe("submitting");

    await rehydrateSession("session-mutation-race");
    expect(useAppStore.getState().approvalsBySession["session-mutation-race"][0].state).toBe("submitting");

    rejectResponse(new ApiError(
      409,
      "Mutation outcome is unknown; reconcile before retrying",
      "MUTATION_DELIVERY_UNKNOWN",
    ));
    await expect(mutation).rejects.toMatchObject({ code: "MUTATION_DELIVERY_UNKNOWN" });
    expect(useAppStore.getState().approvalsBySession["session-mutation-race"][0]).toMatchObject({
      state: "ambiguous",
      error: expect.stringContaining("confirmado"),
    });

    history.mockResolvedValueOnce({
      items: [
        { id: "race-user", role: "user", content: "Hazlo" },
        { id: "race-final", role: "assistant", content: "Acción terminada" },
      ],
      sessionStatus: "ready",
      activeOperation: null,
      pendingInteractions: [],
    });
    await rehydrateSession("session-mutation-race");
    expect(useAppStore.getState().approvalsBySession["session-mutation-race"]).toBeUndefined();
  });

  it("does not revive a gate resolved while an older history read was pending", async () => {
    type SessionHistory = Awaited<ReturnType<typeof api.sessionHistory>>;
    let resolveHistory!: (value: SessionHistory) => void;
    vi.spyOn(api, "sessionHistory").mockImplementation(
      () => new Promise((resolve) => { resolveHistory = resolve; }),
    );
    const request = {
      type: "approval.request",
      controlSessionId: "session-interaction-revision",
      data: {
        request_id: "approval-interaction-revision",
        command: "calendar.create --safe",
        choices: ["once", "deny"],
      },
    };
    expect(applyRealtimeEvent(request)).toBe(true);
    const rehydration = rehydrateSession("session-interaction-revision");

    expect(applyRealtimeEvent({
      type: "approval.resolved",
      controlSessionId: "session-interaction-revision",
      data: { request_id: "approval-interaction-revision" },
    })).toBe(true);
    resolveHistory({
      items: [],
      sessionStatus: "waiting",
      activeOperation: null,
      pendingInteractions: [{
        type: "approval.request",
        data: request.data,
      }],
    });
    await rehydration;

    expect(useAppStore.getState().approvalsBySession["session-interaction-revision"]).toBeUndefined();
  });
});
