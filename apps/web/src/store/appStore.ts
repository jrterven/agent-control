import { create } from "zustand";
import type {
  ApprovalRequest,
  Automation,
  BootstrapData,
  ChatMessage,
  ClarificationRequest,
  ConnectionState,
  ControlFeatures,
  Gateway,
  Profile,
  SessionSummary,
  SessionUsage,
  ThemePreference,
  Workspace,
} from "../types";

type AuthState = "checking" | "authenticated" | "offline" | "unauthenticated";

type AppState = {
  authState: AuthState;
  userName: string;
  csrfToken?: string;
  demoMode: boolean;
  leftDrawerOpen: boolean;
  activityOpen: boolean;
  commandOpen: boolean;
  gatewayMenuOpen: boolean;
  selectedGatewayId: string;
  selectedProfileId: string;
  selectedWorkspaceId: string;
  selectedSessionId: string;
  connection: ConnectionState;
  theme: ThemePreference;
  advancedMode: boolean;
  offlineCacheEnabled: boolean;
  bootstrapLoaded: boolean;
  gateways: Gateway[];
  profiles: Profile[];
  workspaces: Workspace[];
  sessions: SessionSummary[];
  automations: Automation[];
  features?: ControlFeatures;
  sessionUsageById: Record<string, SessionUsage>;
  approvalsBySession: Record<string, ApprovalRequest[]>;
  clarificationsBySession: Record<string, ClarificationRequest[]>;
  streamingBySession: Record<string, string>;
  pendingOperations: Record<string, string>;
  messages: ChatMessage[];
  setAuth: (state: AuthState, userName?: string, csrfToken?: string, demoMode?: boolean) => void;
  setLeftDrawerOpen: (open: boolean) => void;
  setActivityOpen: (open: boolean) => void;
  setCommandOpen: (open: boolean) => void;
  setGatewayMenuOpen: (open: boolean) => void;
  selectGateway: (id: string) => void;
  selectProfile: (id: string) => void;
  selectWorkspace: (id: string) => void;
  selectSession: (id: string) => void;
  setConnection: (state: ConnectionState) => void;
  setTheme: (theme: ThemePreference) => void;
  setAdvancedMode: (enabled: boolean) => void;
  setOfflineCacheEnabled: (enabled: boolean) => void;
  hydrateBootstrap: (data: BootstrapData) => void;
  addSession: (session: SessionSummary) => void;
  updateSession: (sessionId: string, update: Partial<SessionSummary>) => void;
  removeSession: (sessionId: string) => void;
  setSessionUsage: (sessionId: string, usage?: SessionUsage) => void;
  upsertApproval: (request: ApprovalRequest) => void;
  updateApproval: (sessionId: string, requestId: string, update: Partial<ApprovalRequest>) => void;
  removeApproval: (sessionId: string, requestId: string) => void;
  upsertClarification: (request: ClarificationRequest) => void;
  updateClarification: (sessionId: string, requestId: string, update: Partial<ClarificationRequest>) => void;
  removeClarification: (sessionId: string, requestId: string) => void;
  clearSessionInteractions: (sessionId: string) => void;
  setMessagesForSession: (sessionId: string, messages: ChatMessage[]) => void;
  appendMessage: (message: ChatMessage) => void;
  updateMessage: (id: string, update: Partial<ChatMessage>) => void;
  setStreamingMessageId: (sessionId: string, id?: string) => void;
  bindOperation: (operationId: string, messageId: string) => void;
  clearOperation: (operationId: string) => void;
  resetPrivateState: () => void;
};

const emptyPrivateState = {
  userName: "Administrador",
  csrfToken: undefined,
  demoMode: false,
  bootstrapLoaded: false,
  selectedGatewayId: "",
  selectedProfileId: "",
  selectedWorkspaceId: "",
  selectedSessionId: "",
  connection: "offline" as ConnectionState,
  gateways: [] as Gateway[],
  profiles: [] as Profile[],
  workspaces: [] as Workspace[],
  sessions: [] as SessionSummary[],
  automations: [] as Automation[],
  features: undefined as ControlFeatures | undefined,
  sessionUsageById: {} as Record<string, SessionUsage>,
  approvalsBySession: {} as Record<string, ApprovalRequest[]>,
  clarificationsBySession: {} as Record<string, ClarificationRequest[]>,
  pendingOperations: {} as Record<string, string>,
  streamingBySession: {} as Record<string, string>,
  messages: [] as ChatMessage[],
};

export const useAppStore = create<AppState>((set) => ({
  authState: "checking",
  leftDrawerOpen: false,
  activityOpen: false,
  commandOpen: false,
  gatewayMenuOpen: false,
  theme: "dark",
  advancedMode: false,
  offlineCacheEnabled: false,
  ...emptyPrivateState,
  setAuth: (authState, userName = "Administrador", csrfToken, demoMode = false) => set((state) => {
    if (authState === "unauthenticated") {
      return {
        ...emptyPrivateState,
        authState,
        theme: state.theme,
        advancedMode: state.advancedMode,
        offlineCacheEnabled: state.offlineCacheEnabled,
        leftDrawerOpen: false,
        activityOpen: false,
        commandOpen: false,
        gatewayMenuOpen: false,
      };
    }
    // An offline snapshot is deliberately stale. Recovery must force a fresh
    // bootstrap even when the browser never emitted an `online` event (for
    // example, when only the SSH tunnel or Control API restarted).
    const recovering = state.authState === "offline" && (
      authState === "checking" || authState === "authenticated"
    );
    return {
      authState,
      userName,
      csrfToken,
      demoMode,
      ...(recovering ? { bootstrapLoaded: false } : {}),
    };
  }),
  setLeftDrawerOpen: (leftDrawerOpen) => set({ leftDrawerOpen }),
  setActivityOpen: (activityOpen) => set({ activityOpen }),
  setCommandOpen: (commandOpen) => set({ commandOpen }),
  setGatewayMenuOpen: (gatewayMenuOpen) => set({ gatewayMenuOpen }),
  selectGateway: (selectedGatewayId) => set((state) => {
    const selectedProfileId = state.profiles.find((item) => item.gatewayId === selectedGatewayId)?.id ?? "";
    const selectedSessionId = state.sessions.find((item) => item.profileId === selectedProfileId && (!state.selectedWorkspaceId || item.workspaceId === state.selectedWorkspaceId))?.id ?? "";
    return { selectedGatewayId, selectedProfileId, selectedSessionId, gatewayMenuOpen: false };
  }),
  selectProfile: (selectedProfileId) => set((state) => {
    const profile = state.profiles.find((item) => item.id === selectedProfileId);
    const selectedSessionId = state.sessions.find((item) => item.profileId === selectedProfileId && (!state.selectedWorkspaceId || item.workspaceId === state.selectedWorkspaceId))?.id ?? "";
    return { selectedProfileId, selectedGatewayId: profile?.gatewayId ?? state.selectedGatewayId, selectedSessionId };
  }),
  selectWorkspace: (selectedWorkspaceId) => set((state) => ({ selectedWorkspaceId, selectedSessionId: state.sessions.find((item) => item.workspaceId === selectedWorkspaceId && item.profileId === state.selectedProfileId)?.id ?? "" })),
  selectSession: (selectedSessionId) => set((state) => {
    const session = state.sessions.find((item) => item.id === selectedSessionId);
    const profile = session ? state.profiles.find((item) => item.id === session.profileId) : undefined;
    return {
      selectedSessionId: session?.id ?? "",
      selectedProfileId: profile?.id ?? state.selectedProfileId,
      selectedGatewayId: profile?.gatewayId ?? state.selectedGatewayId,
      selectedWorkspaceId: session ? session.workspaceId ?? "" : state.selectedWorkspaceId,
      leftDrawerOpen: false,
    };
  }),
  setConnection: (connection) => set({ connection }),
  setTheme: (theme) => set({ theme }),
  setAdvancedMode: (advancedMode) => set({ advancedMode }),
  setOfflineCacheEnabled: (offlineCacheEnabled) => set({ offlineCacheEnabled }),
  hydrateBootstrap: (data) => set((state) => {
    const selectedGatewayId = data.gateways.some((item) => item.id === state.selectedGatewayId) ? state.selectedGatewayId : data.gateways[0]?.id ?? "";
    const selectedProfileId = data.profiles.some((item) => item.id === state.selectedProfileId && item.gatewayId === selectedGatewayId) ? state.selectedProfileId : data.profiles.find((item) => item.gatewayId === selectedGatewayId)?.id ?? data.profiles[0]?.id ?? "";
    // An empty workspace id is the explicit "Sin workspace" filter once the
    // app has loaded. Preserve it during background refreshes so sessions
    // opened from automation runs are not silently replaced by a session in
    // the first workspace.
    const requestedSession = data.sessions.find((item) => item.id === state.selectedSessionId && item.profileId === selectedProfileId);
    const selectedWorkspaceId = requestedSession
      ? requestedSession.workspaceId ?? ""
      : state.bootstrapLoaded && state.selectedWorkspaceId === ""
        ? ""
        : data.workspaces.some((item) => item.id === state.selectedWorkspaceId)
          ? state.selectedWorkspaceId
          : data.workspaces[0]?.id ?? "";
    const selectedSessionId = data.sessions.some((item) => item.id === state.selectedSessionId && item.profileId === selectedProfileId && (!selectedWorkspaceId || item.workspaceId === selectedWorkspaceId)) ? state.selectedSessionId : data.sessions.find((item) => item.profileId === selectedProfileId && (!selectedWorkspaceId || item.workspaceId === selectedWorkspaceId))?.id ?? "";
    return { ...data, bootstrapLoaded: true, selectedGatewayId, selectedProfileId, selectedWorkspaceId, selectedSessionId };
  }),
  addSession: (session) => set((state) => ({ sessions: [session, ...state.sessions.filter((item) => item.id !== session.id)], selectedSessionId: session.id, leftDrawerOpen: false })),
  updateSession: (sessionId, update) => set((state) => ({
    sessions: state.sessions.map((session) => session.id === sessionId ? { ...session, ...update, id: session.id } : session),
  })),
  removeSession: (sessionId) => set((state) => {
    const removed = state.sessions.find((session) => session.id === sessionId);
    const sessions = state.sessions.filter((session) => session.id !== sessionId);
    const removedMessageIds = new Set(
      state.messages.filter((message) => message.sessionId === sessionId).map((message) => message.id),
    );
    const streamingBySession = { ...state.streamingBySession };
    delete streamingBySession[sessionId];
    const sessionUsageById = { ...state.sessionUsageById };
    delete sessionUsageById[sessionId];
    const approvalsBySession = { ...state.approvalsBySession };
    delete approvalsBySession[sessionId];
    const clarificationsBySession = { ...state.clarificationsBySession };
    delete clarificationsBySession[sessionId];
    const pendingOperations = Object.fromEntries(
      Object.entries(state.pendingOperations).filter(([, messageId]) => !removedMessageIds.has(messageId)),
    );
    const selectedSessionId = state.selectedSessionId === sessionId
      ? sessions.find((session) => (
        session.profileId === state.selectedProfileId
        && (session.workspaceId ?? "") === state.selectedWorkspaceId
      ))?.id ?? ""
      : state.selectedSessionId;
    return {
      sessions,
      selectedSessionId,
      messages: state.messages.filter((message) => message.sessionId !== sessionId),
      sessionUsageById,
      approvalsBySession,
      clarificationsBySession,
      streamingBySession,
      pendingOperations,
      workspaces: removed?.workspaceId
        ? state.workspaces.map((workspace) => workspace.id === removed.workspaceId
          ? { ...workspace, sessionCount: Math.max(0, workspace.sessionCount - 1) }
          : workspace)
        : state.workspaces,
    };
  }),
  setSessionUsage: (sessionId, usage) => set((state) => {
    const sessionUsageById = { ...state.sessionUsageById };
    if (usage) sessionUsageById[sessionId] = usage;
    else delete sessionUsageById[sessionId];
    return { sessionUsageById };
  }),
  upsertApproval: (request) => set((state) => {
    const current = state.approvalsBySession[request.sessionId] ?? [];
    const existing = current.find((item) => item.requestId === request.requestId);
    const next = existing
      ? current.map((item) => item.requestId === request.requestId
        ? item.state === "submitting"
          ? { ...request, state: item.state, error: item.error }
          : request
        : item)
      : [...current, request];
    return { approvalsBySession: { ...state.approvalsBySession, [request.sessionId]: next } };
  }),
  updateApproval: (sessionId, requestId, update) => set((state) => ({
    approvalsBySession: {
      ...state.approvalsBySession,
      [sessionId]: (state.approvalsBySession[sessionId] ?? []).map((request) => (
        request.requestId === requestId ? { ...request, ...update } : request
      )),
    },
  })),
  removeApproval: (sessionId, requestId) => set((state) => {
    const approvalsBySession = { ...state.approvalsBySession };
    const remaining = (approvalsBySession[sessionId] ?? []).filter((request) => request.requestId !== requestId);
    if (remaining.length) approvalsBySession[sessionId] = remaining;
    else delete approvalsBySession[sessionId];
    return { approvalsBySession };
  }),
  upsertClarification: (request) => set((state) => {
    const current = state.clarificationsBySession[request.sessionId] ?? [];
    const existing = current.find((item) => item.requestId === request.requestId);
    const next = existing
      ? current.map((item) => item.requestId === request.requestId
        ? {
            ...request,
            answers: { ...request.answers, ...item.answers },
            remainingQuestionIds: item.remainingQuestionIds ?? request.remainingQuestionIds,
            submittingQuestionId: item.state === "submitting" ? item.submittingQuestionId : undefined,
            state: (item.state === "submitting" ? item.state : "pending") as ClarificationRequest["state"],
            error: item.state === "submitting" ? item.error : undefined,
          }
        : item)
      : [...current, request];
    return { clarificationsBySession: { ...state.clarificationsBySession, [request.sessionId]: next } };
  }),
  updateClarification: (sessionId, requestId, update) => set((state) => ({
    clarificationsBySession: {
      ...state.clarificationsBySession,
      [sessionId]: (state.clarificationsBySession[sessionId] ?? []).map((request) => (
        request.requestId === requestId ? { ...request, ...update } : request
      )),
    },
  })),
  removeClarification: (sessionId, requestId) => set((state) => {
    const clarificationsBySession = { ...state.clarificationsBySession };
    const remaining = (clarificationsBySession[sessionId] ?? []).filter((request) => request.requestId !== requestId);
    if (remaining.length) clarificationsBySession[sessionId] = remaining;
    else delete clarificationsBySession[sessionId];
    return { clarificationsBySession };
  }),
  clearSessionInteractions: (sessionId) => set((state) => {
    const approvalsBySession = { ...state.approvalsBySession };
    const clarificationsBySession = { ...state.clarificationsBySession };
    delete approvalsBySession[sessionId];
    delete clarificationsBySession[sessionId];
    return { approvalsBySession, clarificationsBySession };
  }),
  setMessagesForSession: (sessionId, nextMessages) => set((state) => {
    const current = state.messages.filter((message) => message.sessionId === sessionId);
    const incomingIds = new Set(nextMessages.map((message) => message.id));
    const operationMessageIds = new Set(Object.values(state.pendingOperations));
    const streamingMessageId = state.streamingBySession[sessionId];
    const pending = current.filter((message) => !incomingIds.has(message.id) && (
      message.streaming === true
      || message.id === streamingMessageId
      || operationMessageIds.has(message.id)
      || message.delivery === "sending"
      || message.delivery === "ambiguous"
    ));
    return { messages: [...state.messages.filter((message) => message.sessionId !== sessionId), ...nextMessages, ...pending] };
  }),
  appendMessage: (message) => set((state) => ({ messages: [...state.messages, message] })),
  updateMessage: (id, update) => set((state) => ({ messages: state.messages.map((message) => message.id === id ? { ...message, ...update } : message) })),
  setStreamingMessageId: (sessionId, messageId) => set((state) => {
    const streamingBySession = { ...state.streamingBySession };
    if (messageId) streamingBySession[sessionId] = messageId;
    else delete streamingBySession[sessionId];
    return { streamingBySession };
  }),
  bindOperation: (operationId, messageId) => set((state) => ({ pendingOperations: { ...state.pendingOperations, [operationId]: messageId } })),
  clearOperation: (operationId) => set((state) => {
    const pendingOperations = { ...state.pendingOperations };
    delete pendingOperations[operationId];
    return { pendingOperations };
  }),
  resetPrivateState: () => set((state) => ({
    ...emptyPrivateState,
    authState: "unauthenticated",
    theme: state.theme,
    advancedMode: state.advancedMode,
    offlineCacheEnabled: state.offlineCacheEnabled,
    leftDrawerOpen: false,
    activityOpen: false,
    commandOpen: false,
    gatewayMenuOpen: false,
  })),
}));
