import type { Automation, ChatMessage, Profile, SearchResult, SessionSummary, Workspace } from "../types";

export type SearchSources = {
  sessions: SessionSummary[];
  workspaces: Workspace[];
  automations: Automation[];
  messages: ChatMessage[];
  profiles: Profile[];
};

function compact(value: string, maximum = 120) {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length > maximum ? `${normalized.slice(0, maximum - 1)}…` : normalized;
}

export function buildSearchResults({ sessions, workspaces, automations, messages, profiles }: SearchSources): SearchResult[] {
  const sessionById = new Map(sessions.map((session) => [session.id, session]));
  const profileById = new Map(profiles.map((profile) => [profile.id, profile]));

  const messageResults: SearchResult[] = messages
    .filter((message) => message.role !== "system" && message.content.trim())
    .slice(-200)
    .reverse()
    .map((message) => {
      const session = sessionById.get(message.sessionId);
      const profile = session ? profileById.get(session.profileId) : undefined;
      return {
        id: `message:${message.id}`,
        targetId: message.sessionId,
        kind: "message",
        title: session?.title || compact(message.content, 56),
        excerpt: compact(message.content),
        meta: `${profile?.displayName ?? "Agente"} · ${message.createdAt || "historial"}`,
      };
    });

  const sessionResults: SearchResult[] = sessions.map((session) => ({
    id: `session:${session.id}`,
    targetId: session.id,
    kind: "session",
    title: session.title,
    excerpt: compact(session.preview || "Conversación de Hermes"),
    meta: `${profileById.get(session.profileId)?.displayName ?? "Agente"} · ${session.updatedAt}`,
  }));

  const workspaceResults: SearchResult[] = workspaces.map((workspace) => ({
    id: `workspace:${workspace.id}`,
    targetId: workspace.id,
    kind: "workspace",
    title: workspace.name,
    excerpt: compact(workspace.description || `${workspace.sessionCount} sesiones`),
    meta: `Workspace · ${workspace.sessionCount} sesiones`,
  }));

  const automationResults: SearchResult[] = automations.map((automation) => ({
    id: `automation:${automation.id}`,
    targetId: automation.id,
    kind: "automation",
    title: automation.name,
    excerpt: `${automation.schedule} · ${automation.timezone}`,
    meta: automation.enabled ? "Automatización activa" : "Automatización pausada",
  }));

  return [...messageResults, ...sessionResults, ...workspaceResults, ...automationResults];
}
