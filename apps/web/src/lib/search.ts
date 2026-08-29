import type { Automation, ChatMessage, Profile, SearchResult, SessionSummary, Workspace } from "../types";
import i18n from "../i18n";

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

type SearchTranslator = (key: string, options?: Record<string, string | number>) => string;

export function buildSearchResults(
  { sessions, workspaces, automations, messages, profiles }: SearchSources,
  translate: SearchTranslator = (key, options) => String(i18n.t(key, options)),
): SearchResult[] {
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
        meta: `${profile?.displayName ?? translate("searchMeta.agent")} · ${message.createdAt || translate("searchMeta.history")}`,
      };
    });

  const sessionResults: SearchResult[] = sessions.map((session) => ({
    id: `session:${session.id}`,
    targetId: session.id,
    kind: "session",
    title: session.title,
    excerpt: compact(session.preview || translate("searchMeta.conversation")),
    meta: `${profileById.get(session.profileId)?.displayName ?? translate("searchMeta.agent")} · ${session.updatedAt}`,
  }));

  const workspaceResults: SearchResult[] = workspaces.map((workspace) => ({
    id: `workspace:${workspace.id}`,
    targetId: workspace.id,
    kind: "workspace",
    title: workspace.name,
    excerpt: compact(workspace.description || translate("searchMeta.sessionCount", { count: workspace.sessionCount })),
    meta: translate("searchMeta.workspace", { count: workspace.sessionCount }),
  }));

  const automationResults: SearchResult[] = automations.map((automation) => ({
    id: `automation:${automation.id}`,
    targetId: automation.id,
    kind: "automation",
    title: automation.name,
    excerpt: `${automation.schedule} · ${automation.timezone}`,
    meta: automation.enabled ? translate("searchMeta.activeAutomation") : translate("searchMeta.pausedAutomation"),
  }));

  return [...messageResults, ...sessionResults, ...workspaceResults, ...automationResults];
}
