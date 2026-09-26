export * from "./vision";

export type ConnectionState = "connected" | "connecting" | "reconnecting" | "degraded" | "offline";

export interface AuthMethods {
  mode: "private" | "cloud";
  googleEnabled: boolean;
  registrationMode?: "invite_only" | "open";
  betaMaxUsers?: number;
}

export interface ConnectorView {
  id: string;
  name: string;
  status: "online" | "offline" | "revoked";
  version: string | null;
  lastSeenAt: string | null;
  profiles: string[];
  gatewayId?: string | null;
  installationKind?: "managed" | "existing" | null;
  hermesVersion?: string | null;
  update?: {
    protocol: number;
    supported: boolean;
    release?: string | null;
    availableRelease?: string | null;
    state: "current" | "checking" | "available" | "waiting" | "downloading" | "installing" | "failed" | "manual" | "paused";
    reason?: string | null;
    checkedAt?: number | null;
    automatic: boolean;
    pausedUntil: number;
  };
}

export interface ConnectorList {
  items: ConnectorView[];
  installCommand: string;
}

export interface ConnectorPairing {
  code: string;
  name: string;
  profiles: string[];
  expiresAt: string;
}

export interface SessionRoute {
  gatewayId: string;
  profileName: string;
  storedSessionId: string;
  runtimeSessionId?: string | null;
}

export interface BackgroundTask {
  id: string;
  state: "queued" | "running" | "completed" | "failed" | "cancelled" | "unknown";
  deliveryState: "pending" | "delivered" | "dropped" | "unknown";
  /** A server-owned generic label; never the native goal or internal result. */
  title: string;
  createdAt: string;
  updatedAt: string;
  completedAt?: string | null;
}

export interface BackgroundTaskSnapshot {
  items: BackgroundTask[];
  complete: boolean;
  activeCount: number | null;
  pendingDeliveryCount: number | null;
  available: boolean;
  observedAt: string;
}

export interface ControlTurnOrigin {
  kind: "background_task";
  taskId?: string;
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
export type ChatMode = "memory_read_write" | "memory_read_only" | "temporary";
