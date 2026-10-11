export type PanelKind = "pending" | "prepared" | "background" | "automations" | "pinned" | "mail" | "reports" | "custom";
export interface HomeSources { sessionIds: string[]; workspaceIds: string[]; automationIds: string[]; mailAccountIds: string[]; urls: string[] }
export interface PanelDefinition {
  id: string; kind: PanelKind; title: string; description: string; size: "compact" | "wide"; pinned: boolean;
  sources: HomeSources; profileId: string | null; refreshMinutes: 60 | 1440 | 10080;
  display: "summary" | "list" | "metrics" | "table" | "chart";
}
export interface ProactivePreference {
  enabled: boolean; profileId: string | null; sources: HomeSources; timezone: string;
  language: "en" | "es" | "fr" | "de" | "pt"; quietStart: string | null; quietEnd: string | null;
}
export interface HomeLayout {
  paused: boolean; theme: "light" | "dark" | "auto" | null;
  revision: number; background: "mist" | "dusk" | "plain"; reduceTransparency: boolean;
  panels: PanelDefinition[]; proactive: ProactivePreference;
}
export interface PanelItem { id: string; title: string; detail?: string; status?: string; url?: string; sessionId?: string; observedAt?: string }
export interface PanelBlock {
  type: "summary" | "list" | "metrics" | "table" | "chart";
  title?: string; text?: string; items?: string[];
  metrics?: { label: string; value: string }[]; columns?: string[]; rows?: string[][];
  points?: { label: string; value: number }[];
}
export interface PanelSnapshot {
  status: "ready" | "pending" | "stale"; items?: PanelItem[]; blocks?: PanelBlock[];
  updatedAt: string | null; errorCode?: string | null; sources?: { title: string; url?: string; sessionId?: string }[];
}
export interface Suggestion {
  id: string; title: string; reason: string; draft: string; status: string; dueAt?: string | null;
  updatedAt: string; unread: boolean; sourceKey: string; profileId: string;
  evidence: { title: string; quote: string; url?: string; sessionId?: string };
}
export interface HomeData {
  features: { customPanels: boolean; proactive: boolean };
  layout: HomeLayout; snapshots: Record<string, PanelSnapshot>; suggestions: Suggestion[];
  sources: Record<"sessions" | "workspaces" | "automations" | "mailAccounts" | "profiles", { id: string; label: string; status?: string; available?: boolean }[]>;
  review: { model: "gpt-6-luna"; intervalMinutes: 60; lastCheckedAt: string | null; lastAnalysisAt: string | null;
    nextReviewAt: string | null; errorCode: string | null;
    history: { id: string; status: string; startedAt: string; errorCode: string | null; inputTokens: number; outputTokens: number }[] };
}
