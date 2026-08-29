export type ConnectionState = "connected" | "connecting" | "reconnecting" | "degraded" | "offline";

export interface SessionRoute {
  gatewayId: string;
  profileName: string;
  storedSessionId: string;
  runtimeSessionId?: string | null;
}

export interface CapabilitySet {
  version?: string | null;
  sourceSha?: string | null;
  protocol?: string | null;
  methods: readonly string[];
  features: readonly string[];
  unknownFields?: Record<string, unknown>;
}

export interface NormalizedEvent<TData = Record<string, unknown>> {
  eventId: string;
  correlationId?: string | null;
  gatewayId: string;
  profileName: string;
  controlSessionId?: string | null;
  type: string;
  seq?: number | null;
  replayEpoch?: string | null;
  occurredAt: string;
  data: TData;
}

export interface GatewaySummary {
  id: string;
  name: string;
  state: ConnectionState;
  latencyMs?: number | null;
  capabilities: CapabilitySet;
}

export interface ProfileSummary {
  gatewayId: string;
  profileName: string;
  displayName: string;
  active: boolean;
  writable: boolean;
  state: ConnectionState;
}

export interface WorkspaceSummary {
  id: string;
  name: string;
  description?: string | null;
  sessionCount: number;
  updatedAt: string;
}

export interface SessionSummary extends SessionRoute {
  id: string;
  title: string;
  workspaceId?: string | null;
  status: "inactive" | "ready" | "streaming" | "interrupted" | "error";
  archived: boolean;
  pinned: boolean;
  updatedAt: string;
}

export interface AutomationSummary {
  id: string;
  gatewayId: string;
  profileName: string;
  workspaceId?: string | null;
  name: string;
  schedule: string;
  timezone: string;
  enabled: boolean;
  nextRuns: string[];
}

export interface ApiError {
  code: string;
  message: string;
  correlationId?: string;
  details?: Record<string, unknown>;
}
