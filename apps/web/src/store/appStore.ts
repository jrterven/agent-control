import { create } from "zustand";
import type {
  ApprovalRequest,
  Automation,
  BackgroundTaskSnapshot,
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
import { detectedTimeZone } from "../lib/dateTime";
import { readDesktopSidebarOpen, saveDesktopSidebarOpen } from "../lib/sidebarPreference";
import { readAgentPreference, saveAgentPreference } from "../lib/agentPreference";

type AuthState = "checking" | "authenticated" | "offline" | "unauthenticated";

type AppState = {
  authState: AuthState;
  authGeneration: number;
  userName: string;
  userId?: string;
  csrfToken?: string;
  demoMode: boolean;
  leftDrawerOpen: boolean;
  desktopSidebarOpen: boolean;
  activityOpen: boolean;
  desktopContextOpen: boolean;
  notificationsOpen: boolean;
  commandOpen: boolean;
  gatewayMenuOpen: boolean;
  selectedGatewayId: string;
  selectedProfileId: string;
  selectedWorkspaceId: string;
  selectedSessionId: string;
  connection: ConnectionState;
  theme: ThemePreference;
  timeZone: string;
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
  backgroundTasksBySession: Record<string, BackgroundTaskSnapshot>;
  approvalsBySession: Record<string, ApprovalRequest[]>;
  clarificationsBySession: Record<string, ClarificationRequest[]>;
  streamingBySession: Record<string, string>;
  runtimeTurnBySession: Record<string, string>;
  pendingOperations: Record<string, string>;
  messages: ChatMessage[];
  setAuth: (state: AuthState, userName?: string, csrfToken?: string, demoMode?: boolean, userId?: string) => void;
  setLeftDrawerOpen: (open: boolean) => void;
  setDesktopSidebarOpen: (open: boolean) => void;
  setActivityOpen: (open: boolean) => void;
  setDesktopContextOpen: (open: boolean) => void;
  setNotificationsOpen: (open: boolean) => void;
  setCommandOpen: (open: boolean) => void;
  setGatewayMenuOpen: (open: boolean) => void;
  selectGateway: (id: string) => void;
  selectProfile: (id: string) => void;
  selectWorkspace: (id: string) => void;
  selectSession: (id: string) => void;
  setConnection: (state: ConnectionState) => void;
  setTheme: (theme: ThemePreference) => void;
  setTimeZone: (timeZone: string) => void;
  setAdvancedMode: (enabled: boolean) => void;
  setOfflineCacheEnabled: (enabled: boolean) => void;
  hydrateBootstrap: (data: BootstrapData) => void;
  addSession: (session: SessionSummary) => void;
  updateSession: (sessionId: string, update: Partial<SessionSummary>) => void;
  removeSession: (sessionId: string) => void;
  removeSessions: (sessionIds: string[]) => void;
  setSessionUsage: (sessionId: string, usage?: SessionUsage) => void;
  setBackgroundTasks: (sessionId: string, snapshot: BackgroundTaskSnapshot) => void;
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
  setRuntimeTurn: (sessionId: string, id?: string) => void;
  bindOperation: (operationId: string, messageId: string) => void;
  clearOperation: (operationId: string) => void;
  resetPrivateState: (retainAuthState?: boolean) => void;
};

const emptyPrivateState = {
  userName: "Administrador",
  userId: undefined as string | undefined,
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
  backgroundTasksBySession: {} as Record<string, BackgroundTaskSnapshot>,
  approvalsBySession: {} as Record<string, ApprovalRequest[]>,
  clarificationsBySession: {} as Record<string, ClarificationRequest[]>,
  pendingOperations: {} as Record<string, string>,
  streamingBySession: {} as Record<string, string>,
  runtimeTurnBySession: {} as Record<string, string>,
  messages: [] as ChatMessage[],
};

function agentPreferenceOwner(state: AppState) {
  return !state.demoMode && (state.authState === "authenticated" || state.authState === "offline")
    ? state.userId : undefined;
}

function rememberAgent(state: AppState, profile: Profile | undefined) {
  const owner = agentPreferenceOwner(state);
  if (owner && profile && state.gateways.some((gateway) => gateway.id === profile.gatewayId)) {
    saveAgentPreference(owner, { gatewayId: profile.gatewayId, profileId: profile.id });
  }
}

function withoutSessions(state: AppState, sessionIds: Set<string>): Partial<AppState> {
  if (!sessionIds.size) return {};
  const removed = state.sessions.filter((session) => sessionIds.has(session.id));
  const sessions = state.sessions.filter((session) => !sessionIds.has(session.id));
  const removedMessageIds = new Set(
    state.messages.filter((message) => sessionIds.has(message.sessionId)).map((message) => message.id),
  );
  const streamingBySession = { ...state.streamingBySession };
  const runtimeTurnBySession = { ...state.runtimeTurnBySession };
  const sessionUsageById = { ...state.sessionUsageById };
  const backgroundTasksBySession = { ...state.backgroundTasksBySession };
  const approvalsBySession = { ...state.approvalsBySession };
  const clarificationsBySession = { ...state.clarificationsBySession };
  sessionIds.forEach((sessionId) => {
    delete streamingBySession[sessionId];
    delete runtimeTurnBySession[sessionId];
    delete sessionUsageById[sessionId];
    delete backgroundTasksBySession[sessionId];
    delete approvalsBySession[sessionId];
    delete clarificationsBySession[sessionId];
  });
  const pendingOperations = Object.fromEntries(
    Object.entries(state.pendingOperations).filter(([, messageId]) => !removedMessageIds.has(messageId)),
  );
  const selectedSessionId = sessionIds.has(state.selectedSessionId)
    ? sessions.find((session) => (
      session.profileId === state.selectedProfileId
      && (session.workspaceId ?? "") === state.selectedWorkspaceId
    ))?.id ?? ""
    : state.selectedSessionId;
  const removedByWorkspace = new Map<string, number>();
  removed.forEach((session) => {
    if (!session.workspaceId) return;
    removedByWorkspace.set(session.workspaceId, (removedByWorkspace.get(session.workspaceId) ?? 0) + 1);
  });
  return {
    sessions,
    selectedSessionId,
    messages: state.messages.filter((message) => !sessionIds.has(message.sessionId)),
    sessionUsageById,
    backgroundTasksBySession,
    approvalsBySession,
    clarificationsBySession,
    streamingBySession,
    runtimeTurnBySession,
    pendingOperations,
    workspaces: state.workspaces.map((workspace) => {
      const count = removedByWorkspace.get(workspace.id) ?? 0;
      return count ? { ...workspace, sessionCount: Math.max(0, workspace.sessionCount - count) } : workspace;
    }),
  };
}

export const useAppStore = create<AppState>((set) => ({
  authState: "checking",
  authGeneration: 0,
  leftDrawerOpen: false,
  desktopSidebarOpen: readDesktopSidebarOpen(),
  activityOpen: false,
  desktopContextOpen: true,
  notificationsOpen: false,
  commandOpen: false,
  gatewayMenuOpen: false,
  theme: "dark",
  timeZone: detectedTimeZone(),
  advancedMode: false,
  offlineCacheEnabled: false,
  ...emptyPrivateState,
  setAuth: (authState, userName = "Administrador", csrfToken, demoMode = false, userId) => set((state) => {
    const nextUserId = authState === "unauthenticated" ? undefined : userId ?? state.userId;
    const nextToken = authState === "unauthenticated" ? undefined : csrfToken;
    // Authentication transitions invalidate pending work, including a logout
    // followed by login as the same owner. A verified CSRF refresh updates only
    // csrfToken directly and deliberately retains this lifetime identifier.
    const authGeneration = state.authGeneration + Number(
      state.authState !== authState || state.userId !== nextUserId
      || state.csrfToken !== nextToken || state.demoMode !== demoMode,
    );
    if (authState === "unauthenticated") {
      return {
        ...emptyPrivateState,
        authState,
        authGeneration,
        theme: state.theme,
        timeZone: state.timeZone,
        advancedMode: state.advancedMode,
        offlineCacheEnabled: state.offlineCacheEnabled,
        leftDrawerOpen: false,
        activityOpen: false,
        desktopContextOpen: true,
        notificationsOpen: false,
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
    const changedOwner = !!userId && !!state.userId && userId !== state.userId;
    return {
      ...(changedOwner ? emptyPrivateState : {}),
      authState,
      authGeneration,
      userId: nextUserId,
      userName,
      csrfToken,
      demoMode,
      ...(recovering ? { bootstrapLoaded: false } : {}),
    };
  }),
  setLeftDrawerOpen: (leftDrawerOpen) => set({ leftDrawerOpen }),
  setDesktopSidebarOpen: (desktopSidebarOpen) => {
    saveDesktopSidebarOpen(desktopSidebarOpen);
    set({ desktopSidebarOpen, gatewayMenuOpen: false });
  },
  setActivityOpen: (activityOpen) => set({ activityOpen }),
  setDesktopContextOpen: (desktopContextOpen) => set({ desktopContextOpen }),
  setNotificationsOpen: (notificationsOpen) => set({ notificationsOpen }),
  setCommandOpen: (commandOpen) => set({ commandOpen }),
  setGatewayMenuOpen: (gatewayMenuOpen) => set({ gatewayMenuOpen }),
  selectGateway: (selectedGatewayId) => set((state) => {
    const profile = state.profiles.find((item) => item.gatewayId === selectedGatewayId);
    const selectedProfileId = profile?.id ?? "";
    const selectedSessionId = state.sessions.find((item) => item.profileId === selectedProfileId && (item.workspaceId ?? "") === state.selectedWorkspaceId)?.id ?? "";
    rememberAgent(state, profile);
    return { selectedGatewayId, selectedProfileId, selectedSessionId, gatewayMenuOpen: false };
  }),
  selectProfile: (selectedProfileId) => set((state) => {
    const profile = state.profiles.find((item) => item.id === selectedProfileId);
    const selectedSessionId = state.sessions.find((item) => item.profileId === selectedProfileId && (item.workspaceId ?? "") === state.selectedWorkspaceId)?.id ?? "";
    rememberAgent(state, profile);
    return { selectedProfileId, selectedGatewayId: profile?.gatewayId ?? state.selectedGatewayId, selectedSessionId };
  }),
  selectWorkspace: (selectedWorkspaceId) => set((state) => ({ selectedWorkspaceId, selectedSessionId: state.sessions.find((item) => (item.workspaceId ?? "") === selectedWorkspaceId && item.profileId === state.selectedProfileId)?.id ?? "" })),
  selectSession: (selectedSessionId) => set((state) => {
    const session = state.sessions.find((item) => item.id === selectedSessionId);
    const profile = session ? state.profiles.find((item) => item.id === session.profileId) : undefined;
    rememberAgent(state, profile);
    return {
      selectedSessionId: session?.id ?? "",
      selectedProfileId: profile?.id ?? state.selectedProfileId,
      selectedGatewayId: profile?.gatewayId ?? state.selectedGatewayId,
      selectedWorkspaceId: session ? session.workspaceId ?? "" : state.selectedWorkspaceId,
      leftDrawerOpen: false,
      notificationsOpen: false,
    };
  }),
  setConnection: (connection) => set({ connection }),
  setTheme: (theme) => set({ theme }),
  setTimeZone: (timeZone) => set({ timeZone }),
  setAdvancedMode: (advancedMode) => set({ advancedMode }),
  setOfflineCacheEnabled: (offlineCacheEnabled) => set({ offlineCacheEnabled }),
  hydrateBootstrap: (data) => set((state) => {
    const owner = agentPreferenceOwner(state);
    const remembered = !state.bootstrapLoaded && owner ? readAgentPreference(owner) : undefined;
    const availableProfiles = data.profiles.filter((profile) => data.gateways.some((gateway) => gateway.id === profile.gatewayId));
    // Keep a current selection (including the PWA update return context). Only
    // a cold start may restore the account's last explicitly chosen agent.
    const preferredProfile = availableProfiles.find((profile) => profile.id === state.selectedProfileId)
      ?? availableProfiles.find((profile) => profile.id === remembered?.profileId && profile.gatewayId === remembered.gatewayId);
    const fallbackGatewayId = data.gateways.some((item) => item.id === state.selectedGatewayId) ? state.selectedGatewayId : data.gateways[0]?.id ?? "";
    const selectedProfile = preferredProfile ?? availableProfiles.find((item) => item.gatewayId === fallbackGatewayId) ?? availableProfiles[0];
    const selectedGatewayId = selectedProfile?.gatewayId ?? fallbackGatewayId;
    const selectedProfileId = selectedProfile?.id ?? "";
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
    const selectedSessionId = data.sessions.some((item) => item.id === state.selectedSessionId && item.profileId === selectedProfileId && (item.workspaceId ?? "") === selectedWorkspaceId) ? state.selectedSessionId : data.sessions.find((item) => item.profileId === selectedProfileId && (item.workspaceId ?? "") === selectedWorkspaceId)?.id ?? "";
    return { ...data, bootstrapLoaded: true, selectedGatewayId, selectedProfileId, selectedWorkspaceId, selectedSessionId };
  }),
  addSession: (session) => set((state) => ({ sessions: [session, ...state.sessions.filter((item) => item.id !== session.id)], selectedSessionId: session.id, leftDrawerOpen: false })),
  updateSession: (sessionId, update) => set((state) => {
    const current = state.sessions.find((session) => session.id === sessionId);
    const previousWorkspaceId = current?.workspaceId ?? "";
    const nextWorkspaceId = Object.prototype.hasOwnProperty.call(update, "workspaceId")
      ? update.workspaceId ?? ""
      : previousWorkspaceId;
    const workspaceChanged = Boolean(current) && previousWorkspaceId !== nextWorkspaceId;
    return {
      sessions: state.sessions.map((session) => session.id === sessionId ? { ...session, ...update, id: session.id } : session),
      workspaces: workspaceChanged
        ? state.workspaces.map((workspace) => {
          if (workspace.id === previousWorkspaceId) return { ...workspace, sessionCount: Math.max(0, workspace.sessionCount - 1) };
          if (workspace.id === nextWorkspaceId) return { ...workspace, sessionCount: workspace.sessionCount + 1 };
          return workspace;
        })
        : state.workspaces,
      selectedWorkspaceId: workspaceChanged && state.selectedSessionId === sessionId
        ? nextWorkspaceId
        : state.selectedWorkspaceId,
    };
  }),
  removeSession: (sessionId) => set((state) => withoutSessions(state, new Set([sessionId]))),
  removeSessions: (sessionIds) => set((state) => withoutSessions(state, new Set(sessionIds))),
  setSessionUsage: (sessionId, usage) => set((state) => {
    const sessionUsageById = { ...state.sessionUsageById };
    if (usage) sessionUsageById[sessionId] = usage;
    else delete sessionUsageById[sessionId];
    return { sessionUsageById };
  }),
  setBackgroundTasks: (sessionId, snapshot) => set((state) => {
    const current = state.backgroundTasksBySession[sessionId];
    if (current && Date.parse(current.observedAt) > Date.parse(snapshot.observedAt)) return {};
    // An incomplete inventory cannot make a previously observed task disappear.
    const items = snapshot.complete ? snapshot.items : [...new Map([
      ...(current?.items ?? []), ...snapshot.items,
    ].map((item) => [item.id, item])).values()].slice(-200);
    return { backgroundTasksBySession: { ...state.backgroundTasksBySession, [sessionId]: { ...snapshot, items } } };
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
      || message.delivery === "queued"
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
  setRuntimeTurn: (sessionId, id) => set((state) => {
    const runtimeTurnBySession = { ...state.runtimeTurnBySession };
    if (id) runtimeTurnBySession[sessionId] = id;
    else delete runtimeTurnBySession[sessionId];
    return { runtimeTurnBySession };
  }),
  bindOperation: (operationId, messageId) => set((state) => ({ pendingOperations: { ...state.pendingOperations, [operationId]: messageId } })),
  clearOperation: (operationId) => set((state) => {
    const pendingOperations = { ...state.pendingOperations };
    delete pendingOperations[operationId];
    return { pendingOperations };
  }),
  resetPrivateState: (retainAuthState = false) => set((state) => ({
    ...emptyPrivateState,
    authState: retainAuthState ? state.authState : "unauthenticated",
    authGeneration: state.authGeneration + 1,
    theme: state.theme,
    timeZone: state.timeZone,
    advancedMode: state.advancedMode,
    offlineCacheEnabled: state.offlineCacheEnabled,
    leftDrawerOpen: false,
    activityOpen: false,
    desktopContextOpen: true,
    notificationsOpen: false,
    commandOpen: false,
    gatewayMenuOpen: false,
  })),
}));

/** A native coordinator turn is foreground work; its delegated tasks are not. */
export function activeResponseId(state: Pick<AppState, "runtimeTurnBySession" | "streamingBySession">, sessionId: string) {
  const turnId = state.runtimeTurnBySession[sessionId];
  return turnId ? `control-turn-${sessionId}-${turnId}` : state.streamingBySession[sessionId];
}
