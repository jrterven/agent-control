import type { ApprovalChoice, Automation, AutomationRun, BootstrapData, Gateway, Profile, RealtimeEvent, SearchResult, SessionSummary, Workspace } from "../types";

export type AdminResourceName = "models" | "config" | "soul" | "skills" | "toolsets" | "mcp" | "channels" | "usage" | "secrets";

export type AdminResourceView = {
  gatewayId: string;
  profileName: string;
  resource: AdminResourceName;
  data: Record<string, unknown>;
};

export type McpServerCreateInput = {
  name: string;
  url?: string;
  command?: string;
  args?: string[];
  env?: Record<string, string>;
  auth?: string;
  bearerToken?: string;
  enabled?: boolean;
};

export type GatewayCreateInput = {
  name: string;
  restUrl: string;
  wsUrl: string;
  apiUrl?: string;
  dashboardToken?: string;
  apiKey?: string;
  trustedSourceSha?: string;
  connectionMode: "private" | "tunnel";
};

export type GatewayUpdateInput = {
  trustedSourceSha?: string | null;
};

export type ProfileCreateInput = {
  gatewayId: string;
  technicalName: string;
  displayName: string;
  description: string;
};

export type ElevenLabsIntegrationView = {
  configured: boolean;
  provider: "elevenlabs";
  modelId: "scribe_v2_realtime";
  ttsModelId?: ElevenLabsTtsModelId;
  voiceId?: string | null;
  voiceName?: string | null;
  speechAvailable?: boolean;
};

export type ElevenLabsTtsModelId = "eleven_flash_v2_5" | "eleven_multilingual_v2";

export type ElevenLabsVoice = {
  id: string;
  name: string;
  category?: string | null;
  labels: Record<string, string>;
  previewAvailable: boolean;
};

export type TranscriptionTokenView = {
  token: string;
  expiresAt: string;
  modelId: "scribe_v2_realtime";
};

export type SpeechTokenView = {
  token: string;
  expiresAt: string;
  modelId: ElevenLabsTtsModelId;
  voiceId: string;
  voiceName: string;
};

export type AutomationCreateInput = {
  gatewayId: string;
  profileName: string;
  workspaceId: string | null;
  name: string;
  schedule: string;
  timezone: string;
  prompt: string;
  enabled: boolean;
};

export type AutomationUpdateInput = Partial<Pick<AutomationCreateInput, "workspaceId" | "name" | "schedule" | "timezone" | "prompt" | "enabled">>;

export type ApprovalResponseReceipt = {
  requestId: string;
  resolved: number;
  status: string;
};

export type ClarificationResponseReceipt = {
  requestId: string;
  status: string;
  remaining: string[];
};

export type AuditEvent = {
  id: string;
  actorUserId?: string | null;
  action: string;
  targetType?: string | null;
  targetId?: string | null;
  outcome: string;
  requestId?: string | null;
  createdAt: string;
};

export type ReadinessView = {
  status: "ready" | "degraded" | "not_ready";
  database: "ready" | "unavailable";
  upstream: "online" | "degraded" | "offline" | "stale" | "unknown";
  enabledGateways?: number;
  staleGateways?: number;
  lastUpstreamCheckAt?: string | null;
  automationRoutes?: "starting" | "healthy" | "failed" | "stale" | "unknown";
  capabilityRefresh?: "starting" | "healthy" | "failed" | "stale" | "unknown";
  time: string;
};

type SessionWire = {
  id: string;
  gatewayId: string;
  workspaceId?: string | null;
  profileName: string;
  profileId?: string | null;
  storedSessionId: string;
  runtimeSessionId?: string | null;
  title?: string | null;
  status: string;
  archivedAt?: string | null;
  updatedAt: string;
};

type AutomationWire = {
  id: string;
  gatewayId: string;
  workspaceId?: string | null;
  profileName: string;
  hermesAutomationId?: string | null;
  name: string;
  schedule: string;
  timezone: string;
  prompt: string;
  enabled: boolean;
  nextRuns: string[];
  updatedAt: string;
};

type WorkspaceWire = {
  id: string;
  name: string;
  description?: string | null;
  color?: string | null;
  archivedAt?: string | null;
  createdAt: string;
  updatedAt: string;
};

function workspaceFromWire(row: WorkspaceWire): Workspace {
  return {
    id: row.id,
    name: row.name,
    description: row.description ?? "",
    sessionCount: 0,
    updatedAt: row.updatedAt,
  };
}

function sessionFromWire(row: SessionWire, fallbackProfileId = ""): SessionSummary {
  return {
    id: row.id,
    gatewayId: row.gatewayId,
    profileName: row.profileName,
    storedSessionId: row.storedSessionId,
    runtimeSessionId: row.runtimeSessionId,
    workspaceId: row.workspaceId ?? undefined,
    profileId: row.profileId ?? fallbackProfileId,
    title: row.title || "Conversación",
    preview: "",
    updatedAt: row.updatedAt,
    archived: Boolean(row.archivedAt),
  };
}

function automationFromWire(row: AutomationWire): Automation {
  return {
    ...row,
    workspaceId: row.workspaceId ?? undefined,
    profileId: "",
    nextRun: row.nextRuns[0] ?? "",
    lastStatus: "idle",
  };
}

export class ApiError extends Error {
  constructor(public status: number, message: string, public code?: string) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const { headers: suppliedHeaders, ...rest } = init ?? {};
  const response = await fetch(`/api/v1${path}`, {
    credentials: "same-origin",
    ...rest,
    headers: {
      "Accept": "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...suppliedHeaders,
    },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ message: "No se pudo completar la solicitud" }));
    if (response.status === 401 && path !== "/auth/me" && path !== "/auth/login") {
      window.dispatchEvent(new Event("hermes-control:unauthorized"));
    }
    throw new ApiError(response.status, String(body.message ?? body.detail ?? "No se pudo completar la solicitud"), typeof body.code === "string" ? body.code : undefined);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

async function requestBlob(path: string): Promise<{ blob: Blob; filename?: string }> {
  const response = await fetch(`/api/v1${path}`, {
    credentials: "same-origin",
    headers: { "Accept": "application/json" },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ message: "No se pudo completar la descarga" }));
    if (response.status === 401) window.dispatchEvent(new Event("hermes-control:unauthorized"));
    throw new ApiError(response.status, String(body.message ?? body.detail ?? "No se pudo completar la descarga"));
  }
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const filename = /filename="([^"\\/]+)"/i.exec(disposition)?.[1];
  return { blob: await response.blob(), filename };
}

async function requestSpeechStream(text: string, csrfToken?: string, signal?: AbortSignal): Promise<Response> {
  const response = await fetch("/api/v1/integrations/elevenlabs/speech", {
    method: "POST",
    credentials: "same-origin",
    signal,
    headers: {
      "Accept": "audio/mpeg",
      "Content-Type": "application/json",
      ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
    },
    body: JSON.stringify({ text }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ message: "Speech playback failed" }));
    if (response.status === 401) window.dispatchEvent(new Event("hermes-control:unauthorized"));
    throw new ApiError(response.status, String(body.message ?? "Speech playback failed"), typeof body.code === "string" ? body.code : undefined);
  }
  return response;
}

function adminProfilePath(gatewayId: string, profileName: string, suffix: string) {
  return `/admin/gateways/${encodeURIComponent(gatewayId)}/profiles/${encodeURIComponent(profileName)}/${suffix}`;
}

function mutationHeaders(csrfToken?: string): HeadersInit {
  return {
    "Idempotency-Key": crypto.randomUUID(),
    ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}),
  };
}

function adminMutation<T>(path: string, method: "PATCH" | "POST" | "DELETE", payload: unknown, csrfToken?: string) {
  return request<T>(path, {
    method,
    headers: mutationHeaders(csrfToken),
    ...(payload === undefined ? {} : { body: JSON.stringify(payload) }),
  });
}

export const api = {
  me: () => request<{ id: string; name: string; csrfToken?: string }>("/auth/me"),
  login: (username: string, password: string) => request<{ id: string; name: string; csrfToken?: string }>("/auth/login", { method: "POST", body: JSON.stringify({ username, password }) }),
  logout: (csrfToken?: string) => request<void>("/auth/logout", { method: "POST", headers: csrfToken ? { "X-CSRF-Token": csrfToken } : undefined }),
  bootstrap: () => request<BootstrapData>("/bootstrap"),
  readiness: () => request<ReadinessView>("/ready"),
  audit: (limit = 50) => request<AuditEvent[]>(`/audit?limit=${encodeURIComponent(String(limit))}`),
  search: (
    query: string,
    kind: "all" | SearchResult["kind"] = "all",
    limit = 100,
    signal?: AbortSignal,
  ) => request<{ items: SearchResult[]; partial: boolean }>(
    `/search?q=${encodeURIComponent(query)}&kind=${encodeURIComponent(kind)}&limit=${encodeURIComponent(String(limit))}`,
    { signal },
  ),
  createWorkspace: (payload: { name: string; description?: string }, csrfToken?: string) => request<WorkspaceWire>("/workspaces", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) },
    body: JSON.stringify(payload),
  }).then(workspaceFromWire),
  updateWorkspace: (workspaceId: string, payload: { name?: string; description?: string; archived?: boolean }, csrfToken?: string) => request<WorkspaceWire>(`/workspaces/${encodeURIComponent(workspaceId)}`, {
    method: "PATCH",
    headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) },
    body: JSON.stringify(payload),
  }).then(workspaceFromWire),
  refreshProfiles: (gatewayId: string, csrfToken?: string) => request(`/profiles/refresh?gatewayId=${encodeURIComponent(gatewayId)}`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) },
  }),
  createProfile: (payload: ProfileCreateInput, csrfToken?: string) => request<Profile>("/profiles", {
    method: "POST",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify(payload),
  }),
  setProfileAvatar: (profileId: string, avatar: Blob | null, csrfToken?: string) => request<{ avatarUrl?: string | null }>(`/profiles/${encodeURIComponent(profileId)}/avatar`, {
    method: avatar ? "PUT" : "DELETE",
    headers: {
      ...mutationHeaders(csrfToken),
      ...(avatar ? { "Content-Type": avatar.type } : {}),
    },
    ...(avatar ? { body: avatar } : {}),
  }),
  syncSessions: (gatewayId: string, profileName: string, csrfToken?: string) => request<SessionWire[]>("/sessions/sync", {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) },
    body: JSON.stringify({ gatewayId, profileName }),
  }).then((rows) => rows.map((row) => sessionFromWire(row))),
  createSession: (profileId: string, workspaceId: string | undefined, csrfToken?: string) => request<SessionWire>("/sessions", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) }, body: JSON.stringify({ profileId, workspaceId }) }).then((row) => sessionFromWire(row, profileId)),
  archiveSession: (sessionId: string, csrfToken?: string) => request<SessionWire>(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify({ archived: true }),
  }).then((row) => sessionFromWire(row)),
  renameSession: (sessionId: string, displayTitle: string, csrfToken?: string) => request<SessionWire>(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify({ displayTitle }),
  }).then((row) => sessionFromWire(row)),
  moveSession: (sessionId: string, workspaceId: string | null, csrfToken?: string) => request<SessionWire>(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "PATCH",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify({ workspaceId }),
  }).then((row) => sessionFromWire(row)),
  deleteSessionFromHermes: (sessionId: string, storedSessionId: string, csrfToken?: string) => request<void>(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
    headers: {
      ...mutationHeaders(csrfToken),
      "X-Confirm-Delete": storedSessionId,
    },
  }),
  sessionHistory: (sessionId: string) => request<{
    items: Array<Record<string, unknown>>;
    sessionStatus: string;
    activeOperation: { operationId: string; status: string; acceptedAt?: string | null } | null;
  }>(`/sessions/${encodeURIComponent(sessionId)}/messages`),
  sessionMediaUrl: (sessionId: string, mediaId: string) => `/api/v1/sessions/${encodeURIComponent(sessionId)}/media/${encodeURIComponent(mediaId)}`,
  exportSession: (sessionId: string) => requestBlob(`/sessions/${encodeURIComponent(sessionId)}/export`),
  submitPrompt: (sessionId: string, content: string, idempotencyKey: string, csrfToken?: string) => request<{ operationId: string; status: string }>(`/sessions/${encodeURIComponent(sessionId)}/prompts`, { method: "POST", headers: { "Idempotency-Key": idempotencyKey, ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) }, body: JSON.stringify({ content }) }),
  promptOperation: (sessionId: string, operationId: string) => request<{ operationId: string; status: string; acceptedAt?: string }>(`/sessions/${encodeURIComponent(sessionId)}/operations/${encodeURIComponent(operationId)}`),
  interrupt: (sessionId: string, csrfToken?: string) => request<void>(`/sessions/${encodeURIComponent(sessionId)}/interrupt`, { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) } }),
  respondApproval: (sessionId: string, requestId: string, choice: ApprovalChoice, csrfToken?: string) => request<ApprovalResponseReceipt>(
    `/sessions/${encodeURIComponent(sessionId)}/approvals/${encodeURIComponent(requestId)}/respond`,
    {
      method: "POST",
      headers: mutationHeaders(csrfToken),
      body: JSON.stringify({ choice }),
    },
  ),
  respondClarification: (sessionId: string, requestId: string, answer: string | string[], questionId?: string, csrfToken?: string) => request<ClarificationResponseReceipt>(
    `/sessions/${encodeURIComponent(sessionId)}/clarifications/${encodeURIComponent(requestId)}/respond`,
    {
      method: "POST",
      headers: mutationHeaders(csrfToken),
      body: JSON.stringify({ answer, ...(questionId ? { questionId } : {}) }),
    },
  ),
  createRealtimeTicket: (csrfToken?: string) => request<{ ticket: string; expiresAt: string }>("/realtime/tickets", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) } }),
  elevenLabsIntegration: () => request<ElevenLabsIntegrationView>("/integrations/elevenlabs"),
  saveElevenLabsKey: (apiKey: string, csrfToken?: string) => request<ElevenLabsIntegrationView>("/integrations/elevenlabs/key", {
    method: "PUT",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify({ apiKey }),
  }),
  testElevenLabsIntegration: (csrfToken?: string) => request<{ ok: true; provider: "elevenlabs"; modelId: "scribe_v2_realtime" }>("/integrations/elevenlabs/test", {
    method: "POST",
    headers: mutationHeaders(csrfToken),
  }),
  deleteElevenLabsKey: (csrfToken?: string) => request<void>("/integrations/elevenlabs/key", {
    method: "DELETE",
    headers: mutationHeaders(csrfToken),
  }),
  createTranscriptionToken: (payload: { sessionId?: string | null; languageCode?: string | null } = {}, csrfToken?: string) => request<TranscriptionTokenView>("/realtime/transcription-token", {
    method: "POST",
    headers: csrfToken ? { "X-CSRF-Token": csrfToken } : undefined,
    body: JSON.stringify(payload),
  }),
  elevenLabsVoices: () => request<{ items: ElevenLabsVoice[] }>("/integrations/elevenlabs/voices"),
  elevenLabsVoicePreviewUrl: (voiceId: string) => `/api/v1/integrations/elevenlabs/voice-preview/${encodeURIComponent(voiceId)}`,
  saveElevenLabsVoice: (voiceId: string, ttsModelId: ElevenLabsTtsModelId, csrfToken?: string) => request<ElevenLabsIntegrationView>("/integrations/elevenlabs/voice", {
    method: "PUT",
    headers: mutationHeaders(csrfToken),
    body: JSON.stringify({ voiceId, ttsModelId }),
  }),
  createSpeechToken: (payload: { sessionId?: string | null } = {}, csrfToken?: string) => request<SpeechTokenView>("/realtime/speech-token", {
    method: "POST",
    headers: csrfToken ? { "X-CSRF-Token": csrfToken } : undefined,
    body: JSON.stringify(payload),
  }),
  streamSpeech: (text: string, csrfToken?: string, signal?: AbortSignal) => requestSpeechStream(text, csrfToken, signal),
  createAutomation: (payload: AutomationCreateInput, csrfToken?: string) => request<AutomationWire>("/automations", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) }, body: JSON.stringify(payload) }).then(automationFromWire),
  syncAutomations: (gatewayId: string, profileName: string, csrfToken?: string) => request<AutomationWire[]>(`/automations/sync?gatewayId=${encodeURIComponent(gatewayId)}&profileName=${encodeURIComponent(profileName)}`, {
    method: "POST",
    headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) },
  }).then((rows) => rows.map(automationFromWire)),
  updateAutomation: (automationId: string, payload: AutomationUpdateInput, csrfToken?: string) => request<AutomationWire>(`/automations/${encodeURIComponent(automationId)}`, { method: "PATCH", headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) }, body: JSON.stringify(payload) }).then(automationFromWire),
  setAutomationEnabled: (automationId: string, enabled: boolean, csrfToken?: string) => request<AutomationWire>(`/automations/${encodeURIComponent(automationId)}/${enabled ? "resume" : "pause"}`, { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) } }).then(automationFromWire),
  deleteAutomation: (automationId: string, csrfToken?: string) => request<void>(`/automations/${encodeURIComponent(automationId)}`, { method: "DELETE", headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) } }),
  triggerAutomation: (automationId: string, csrfToken?: string) => request<{ operationId: string; status: string; acceptedAt?: string }>(`/automations/${encodeURIComponent(automationId)}/trigger`, { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) } }),
  automationRuns: (automationId: string) => request<AutomationRun[]>(`/automations/${encodeURIComponent(automationId)}/runs`),
  markAutomationRunRead: (automationRunId: string, csrfToken?: string) => request<AutomationRun>(`/automation-runs/${encodeURIComponent(automationRunId)}/read`, { method: "POST", headers: mutationHeaders(csrfToken) }),
  createGateway: (payload: GatewayCreateInput, csrfToken?: string) => request<Pick<Gateway, "id">>("/gateways", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID(), ...(csrfToken ? { "X-CSRF-Token": csrfToken } : {}) }, body: JSON.stringify(payload) }),
  updateGateway: (gatewayId: string, payload: GatewayUpdateInput, csrfToken?: string) => request<Gateway>(`/gateways/${encodeURIComponent(gatewayId)}`, { method: "PATCH", headers: mutationHeaders(csrfToken), body: JSON.stringify(payload) }),
  adminModels: (gatewayId: string, profileName: string) => request<AdminResourceView>(adminProfilePath(gatewayId, profileName, "models")),
  adminSetModel: (gatewayId: string, profileName: string, payload: { provider: string; model: string; confirmExpensiveModel?: boolean }, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, "models"), "PATCH", payload, csrfToken),
  adminConfig: (gatewayId: string, profileName: string) => request<AdminResourceView>(adminProfilePath(gatewayId, profileName, "config")),
  adminUpdateConfig: (gatewayId: string, profileName: string, config: Record<string, unknown>, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, "config"), "PATCH", { config }, csrfToken),
  adminSoul: (gatewayId: string, profileName: string) => request<AdminResourceView>(adminProfilePath(gatewayId, profileName, "soul")),
  adminUpdateSoul: (gatewayId: string, profileName: string, content: string, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, "soul"), "PATCH", { content }, csrfToken),
  adminSkills: (gatewayId: string, profileName: string) => request<AdminResourceView>(adminProfilePath(gatewayId, profileName, "skills")),
  adminToggleSkill: (gatewayId: string, profileName: string, name: string, enabled: boolean, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, `skills/${encodeURIComponent(name)}`), "PATCH", { enabled }, csrfToken),
  adminToolsets: (gatewayId: string, profileName: string) => request<AdminResourceView>(adminProfilePath(gatewayId, profileName, "toolsets")),
  adminToggleToolset: (gatewayId: string, profileName: string, name: string, enabled: boolean, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, `toolsets/${encodeURIComponent(name)}`), "PATCH", { enabled }, csrfToken),
  adminMcpServers: (gatewayId: string, profileName: string) => request<AdminResourceView>(adminProfilePath(gatewayId, profileName, "mcp/servers")),
  adminCreateMcpServer: (gatewayId: string, profileName: string, payload: McpServerCreateInput, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, "mcp/servers"), "POST", payload, csrfToken),
  adminToggleMcpServer: (gatewayId: string, profileName: string, name: string, enabled: boolean, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, `mcp/servers/${encodeURIComponent(name)}`), "PATCH", { enabled }, csrfToken),
  adminDeleteMcpServer: (gatewayId: string, profileName: string, name: string, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, `mcp/servers/${encodeURIComponent(name)}`), "DELETE", undefined, csrfToken),
  adminTestMcpServer: (gatewayId: string, profileName: string, name: string, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, `mcp/servers/${encodeURIComponent(name)}/test`), "POST", undefined, csrfToken),
  adminChannels: (gatewayId: string, profileName: string) => request<AdminResourceView>(adminProfilePath(gatewayId, profileName, "channels")),
  adminUpdateChannel: (gatewayId: string, profileName: string, name: string, payload: { enabled?: boolean; env?: Record<string, string>; clearEnv?: string[] }, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, `channels/${encodeURIComponent(name)}`), "PATCH", payload, csrfToken),
  adminTestChannel: (gatewayId: string, profileName: string, name: string, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, `channels/${encodeURIComponent(name)}/test`), "POST", undefined, csrfToken),
  adminUsage: (gatewayId: string, profileName: string, days = 30) => request<AdminResourceView>(`${adminProfilePath(gatewayId, profileName, "usage")}?days=${encodeURIComponent(String(days))}`),
  adminSecrets: (gatewayId: string, profileName: string) => request<AdminResourceView>(adminProfilePath(gatewayId, profileName, "secrets")),
  adminSetSecret: (gatewayId: string, profileName: string, name: string, value: string, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, `secrets/${encodeURIComponent(name)}`), "PATCH", { value }, csrfToken),
  adminDeleteSecret: (gatewayId: string, profileName: string, name: string, csrfToken?: string) => adminMutation<AdminResourceView>(adminProfilePath(gatewayId, profileName, `secrets/${encodeURIComponent(name)}`), "DELETE", undefined, csrfToken),
};

export type RealtimeHandlers = {
  onEvent: (event: RealtimeEvent) => void;
  onState: (state: "connected" | "reconnecting" | "offline") => void;
};

export type ReplayCursor = { seq: number; epoch: string };

// Mobile operating systems throttle JavaScript timers while a PWA is in the
// background. A healthy socket can therefore look old immediately after the
// app becomes visible again. Keep a wider liveness window, but proactively
// replace a genuinely stale socket on resume instead of waiting through the
// exponential retry delay.
export const REALTIME_HEARTBEAT_INTERVAL_MS = 15_000;
export const REALTIME_BACKGROUND_RECONNECT_MS = 60_000;
export const REALTIME_STALE_AFTER_MS = 120_000;

export function shouldReconnectRealtimeAfterBackground(
  lastSeenAt: number,
  now: number,
  visibilityState: DocumentVisibilityState,
) {
  return visibilityState === "visible"
    && now - lastSeenAt > REALTIME_BACKGROUND_RECONNECT_MS;
}

export function advanceReplayCursor(previous: ReplayCursor | undefined, sequence: number, replayEpoch?: string): ReplayCursor {
  const epoch = replayEpoch ?? previous?.epoch ?? "";
  return {
    seq: previous && previous.epoch === epoch ? Math.max(previous.seq, sequence) : sequence,
    epoch,
  };
}

export async function connectRealtime(handlers: RealtimeHandlers, signal: AbortSignal, csrfToken?: string) {
  let attempt = 0;
  let immediateRetry = false;
  const cursors = new Map<string, ReplayCursor>();
  while (!signal.aborted) {
    try {
      handlers.onState(attempt === 0 ? "reconnecting" : "reconnecting");
      const { ticket } = await api.createRealtimeTicket(csrfToken);
      const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
      const query = new URLSearchParams({ ticket });
      if (cursors.size) query.set("cursors", JSON.stringify(Object.fromEntries([...cursors.entries()].slice(-64))));
      const socket = new WebSocket(`${protocol}//${window.location.host}/api/v1/realtime?${query.toString()}`);
      await new Promise<void>((resolve, reject) => {
        const cleanup = () => {
          window.clearTimeout(timer);
          signal.removeEventListener("abort", onAbort);
        };
        const onAbort = () => {
          cleanup();
          socket.close();
          reject(new DOMException("Aborted", "AbortError"));
        };
        const timer = window.setTimeout(() => {
          cleanup();
          socket.close();
          reject(new Error("Realtime timeout"));
        }, 15_000);
        signal.addEventListener("abort", onAbort, { once: true });
        socket.onopen = () => { cleanup(); resolve(); };
        socket.onerror = () => { cleanup(); socket.close(); reject(new Error("Realtime unavailable")); };
        if (signal.aborted) onAbort();
      });
      attempt = 0;
      handlers.onState("connected");
      let lastSeen = Date.now();
      await new Promise<void>((resolve) => {
        let settled = false;
        const sendPing = () => {
          if (socket.readyState !== WebSocket.OPEN) return;
          try {
            socket.send(JSON.stringify({ type: "ping", at: Date.now() }));
          } catch {
            socket.close();
          }
        };
        const cleanup = () => {
          window.clearInterval(heartbeat);
          signal.removeEventListener("abort", onAbort);
          document.removeEventListener("visibilitychange", onVisibilityChange);
        };
        const finish = () => {
          if (settled) return;
          settled = true;
          cleanup();
          resolve();
        };
        const onAbort = () => {
          socket.close();
          finish();
        };
        const onVisibilityChange = () => {
          if (shouldReconnectRealtimeAfterBackground(
            lastSeen,
            Date.now(),
            document.visibilityState,
          )) {
            immediateRetry = true;
            socket.close();
            return;
          }
          if (document.visibilityState !== "visible") return;
          // A prompt ping confirms a short background transition without
          // waiting for the next throttled interval tick.
          sendPing();
        };
        const heartbeat = window.setInterval(() => {
          if (Date.now() - lastSeen > REALTIME_STALE_AFTER_MS) socket.close();
          else sendPing();
        }, REALTIME_HEARTBEAT_INTERVAL_MS);
        socket.onmessage = (message) => {
          lastSeen = Date.now();
          try {
            const event = JSON.parse(String(message.data)) as RealtimeEvent;
            const sequence = event.seq ?? event.sequence;
            const identity = event.runtimeSessionId ?? event.storedSessionId ?? "gateway";
            const routeKey = [event.gatewayId ?? "", event.profileName ?? "", identity].join("\u001f");
            if (typeof sequence === "number") {
              const previous = cursors.get(routeKey);
              cursors.delete(routeKey);
              cursors.set(routeKey, advanceReplayCursor(previous, sequence, event.replayEpoch));
              while (cursors.size > 64) {
                const oldest = cursors.keys().next().value;
                if (typeof oldest !== "string") break;
                cursors.delete(oldest);
              }
            }
            handlers.onEvent(event);
          } catch { /* Unknown frames are intentionally ignored. */ }
        };
        socket.onclose = finish;
        signal.addEventListener("abort", onAbort, { once: true });
        document.addEventListener("visibilitychange", onVisibilityChange);
        // AbortSignal does not replay an abort that raced with listener
        // registration. Close immediately instead of leaving this promise
        // waiting for a socket callback that may never arrive.
        if (signal.aborted) onAbort();
      });
    } catch {
      handlers.onState("offline");
    }
    if (signal.aborted) break;
    attempt += 1;
    const delay = immediateRetry
      ? 0
      : Math.min(30_000, 800 * 2 ** attempt) + Math.random() * 450;
    immediateRetry = false;
    await new Promise((resolve) => window.setTimeout(resolve, delay));
  }
}
