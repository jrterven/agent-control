import type {
  CapabilitySet,
  ConnectionState as SharedConnectionState,
  NormalizedEvent,
  SessionRoute,
} from "@hermes-control/shared-types";

export type ConnectionState = Exclude<SharedConnectionState, "connecting">;
export type ThemePreference = "dark" | "light" | "auto";

export type CapabilityFlags = {
  realtime: boolean;
  sessions: boolean;
  prompts: boolean;
  interrupt: boolean;
  approvals?: boolean;
  clarifications?: boolean;
  cron: boolean;
  cronCreate?: boolean;
  cronUpdate?: boolean;
  cronDelete?: boolean;
  cronTrigger?: boolean;
  profiles: boolean;
  config: boolean;
  memory: boolean;
};

export type Gateway = {
  id: string;
  name: string;
  location: string;
  status: ConnectionState;
  envManaged?: boolean;
  /** Presence only: the operator-supplied revision is never returned. */
  hasTrustedSourceSha?: boolean;
  latencyMs?: number;
  version: string;
  sha: string | null;
  capabilities: CapabilityFlags;
  /** Exact, profile-aware contract advertised by Agent Control. */
  capabilitySet?: CapabilitySet;
};

export type Profile = {
  id: string;
  gatewayId: string;
  technicalName: string;
  displayName: string;
  model: string;
  status: "ready" | "busy" | "offline";
  mutable: boolean;
  capabilities?: CapabilityFlags;
  /** The only source used to reveal profile-scoped administration controls. */
  capabilitySet?: CapabilitySet;
};

export type Workspace = {
  id: string;
  name: string;
  description: string;
  sessionCount: number;
  updatedAt: string;
};

export type SessionSummary = Pick<SessionRoute, "storedSessionId" | "runtimeSessionId"> & Partial<Pick<SessionRoute, "gatewayId" | "profileName">> & {
  id: string;
  workspaceId?: string;
  profileId: string;
  title: string;
  preview: string;
  updatedAt: string;
  unread?: boolean;
  archived?: boolean;
};

/** Public, bounded counters reported by Hermes for one Control session. */
export type SessionUsage = {
  inputTokens?: number;
  outputTokens?: number;
  promptTokens?: number;
  completionTokens?: number;
  totalTokens?: number;
  apiCalls?: number;
  contextUsed?: number;
  contextMax?: number;
  contextPercent?: number;
  compressions?: number;
  activeSubagents?: number;
  reportedAt?: string;
};

export type ToolRun = {
  id: string;
  name: string;
  label: string;
  status: "running" | "completed" | "failed";
  durationMs?: number;
  summary: string;
};

export type MessageMedia = {
  id: string;
  kind: "audio";
  mediaType: string;
};

export type ChatMessage = {
  id: string;
  sessionId: string;
  role: "user" | "assistant" | "system";
  content: string;
  createdAt: string;
  delivery?: "sending" | "sent" | "ambiguous" | "failed";
  tools?: ToolRun[];
  media?: MessageMedia[];
  streaming?: boolean;
};

export type ApprovalChoice = "once" | "session" | "always" | "deny";
export type InteractionRequestState = "pending" | "submitting" | "failed" | "ambiguous";

export type ApprovalRequest = {
  requestId: string;
  sessionId: string;
  command: string;
  description: string;
  choices: ApprovalChoice[];
  patternKeys: string[];
  allowSession: boolean;
  allowPermanent: boolean;
  smartDenied: boolean;
  state: InteractionRequestState;
  error?: string;
};

export type ClarificationQuestion = {
  questionId?: string;
  question: string;
  choices: string[];
  multiSelect: boolean;
};

export type ClarificationRequest = {
  requestId: string;
  sessionId: string;
  batch: boolean;
  questions: ClarificationQuestion[];
  answers: Record<string, string>;
  remainingQuestionIds?: string[];
  submittingQuestionId?: string;
  state: InteractionRequestState;
  error?: string;
};

export type Automation = {
  id: string;
  gatewayId?: string;
  profileName?: string;
  hermesAutomationId?: string | null;
  name: string;
  schedule: string;
  timezone: string;
  profileId: string;
  prompt?: string;
  enabled: boolean;
  nextRun: string;
  nextRuns?: string[];
  lastStatus: "success" | "failed" | "idle";
  updatedAt?: string;
};

export type AutomationRun = {
  id: string;
  automationId: string;
  hermesRunId?: string | null;
  sessionLinkId?: string | null;
  status: string;
  startedAt?: string | null;
  finishedAt?: string | null;
  errorSummary?: string | null;
  readAt?: string | null;
  createdAt: string;
  updatedAt: string;
};

export type SearchResult = {
  id: string;
  targetId?: string;
  kind: "session" | "message" | "automation" | "workspace";
  title: string;
  excerpt: string;
  meta: string;
};

export type RealtimeEvent = Partial<NormalizedEvent> & {
  eventId?: string;
  id?: string;
  type: string;
  occurredAt?: string;
  gatewayId?: string;
  profileName?: string;
  storedSessionId?: string;
  runtimeSessionId?: string;
  controlSessionId?: string;
  sessionId?: string;
  seq?: number;
  sequence?: number;
  replayEpoch?: string;
  correlationId?: string;
  reconciliationRequired?: boolean;
  data?: Record<string, unknown>;
  payload?: Record<string, unknown>;
};

export type BootstrapData = {
  gateways: Gateway[];
  profiles: Profile[];
  workspaces: Workspace[];
  sessions: SessionSummary[];
  automations: Automation[];
};
