import { useEffect, useMemo, useRef } from "react";
import { api, ApiError, connectRealtime } from "./lib/api";
import {
  clearPrivateCache,
  clearDraft,
  loadDraft,
  loadEncryptedTranscript,
  loadOfflineSnapshot,
  loadShellSnapshot,
  loadPreference,
  saveDraft,
  saveEncryptedTranscript,
  saveOfflineSnapshot,
  saveShellSnapshot,
  savePreference,
} from "./lib/db";
import { useAppStore } from "./store/appStore";
import i18n, { getCurrentLanguage } from "./i18n";
import { subscribeToMediaQuery } from "./lib/mediaQuery";
import { recordSessionCompletion } from "./lib/chatNotifications";
import { detectedTimeZone, isValidTimeZone, TIME_ZONE_PREFERENCE_KEY } from "./lib/dateTime";
import type {
  AgentActivityItem,
  ApprovalChoice,
  ApprovalRequest,
  ChatMessage,
  ClarificationQuestion,
  ClarificationRequest,
  EmailReference,
  Profile,
  RealtimeEvent,
  SessionUsage,
  ToolRun,
} from "./types";

const unmatchedEvents = new Map<string, RealtimeEvent[]>();
const unmatchedSessionEvents = new Map<string, RealtimeEvent[]>();
const rehydrationGenerations = new Map<string, number>();
const activeSessionRecoveries = new Map<string, Promise<void>>();
const interactionRevisions = new Map<string, number>();
type StreamingMarkerState = {
  sessionId: string;
  mode: "outside" | "candidate" | "marker";
  pending: string;
  leadingWhitespace: string;
};
const streamingMarkerStates = new Map<string, StreamingMarkerState>();
const streamingMarkerTombstones = new Map<string, string>();
const streamingMarkerQuarantinedSessions = new Set<string>();
let streamingMarkerGlobalQuarantine = false;
let nextRehydrationGeneration = 0;
const CONTROL_BOOT_READ_TIMEOUT_MS = 8_000;
const CONTROL_BOOT_RETRY_INITIAL_MS = 1_000;
const ACTIVE_SESSION_STATUSES = new Set([
  "pending", "queued", "accepted", "starting", "streaming", "running",
  "working", "waiting", "delivery_unknown",
]);
const TERMINAL_STREAM_EVENTS = new Set([
  "message.complete", "message.completed", "message.done", "run.completed",
  "message.error", "message.failed", "message.interrupted", "message.cancelled",
  "run.error", "run.failed", "run.interrupted", "run.cancelled",
  "error", "interrupted",
]);

function interactionRevision(sessionId: string) {
  return interactionRevisions.get(sessionId) ?? 0;
}

function bumpInteractionRevision(sessionId: string) {
  const nextRevision = interactionRevision(sessionId) + 1;
  interactionRevisions.delete(sessionId);
  interactionRevisions.set(sessionId, nextRevision);
  while (interactionRevisions.size > 256) {
    const oldest = interactionRevisions.keys().next().value;
    if (typeof oldest !== "string") break;
    interactionRevisions.delete(oldest);
  }
}

async function boundedControlRead<T>(operation: Promise<T>, timeoutMs = CONTROL_BOOT_READ_TIMEOUT_MS): Promise<T> {
  let timer: number | undefined;
  try {
    return await Promise.race([
      operation,
      new Promise<T>((_resolve, reject) => {
        timer = window.setTimeout(() => reject(new Error("Control read timed out")), timeoutMs);
      }),
    ]);
  } finally {
    window.clearTimeout(timer);
  }
}

function bufferUnmatchedEvent(operationId: string, event: RealtimeEvent) {
  const existing = unmatchedEvents.get(operationId) ?? [];
  // Refresh insertion order so the map behaves as a bounded LRU.
  unmatchedEvents.delete(operationId);
  unmatchedEvents.set(operationId, [...existing, event].slice(-100));
  while (unmatchedEvents.size > 256) {
    const oldest = unmatchedEvents.keys().next().value;
    if (typeof oldest !== "string") break;
    unmatchedEvents.delete(oldest);
  }
}

function bufferUnmatchedSessionEvent(sessionId: string, event: RealtimeEvent) {
  const existing = unmatchedSessionEvents.get(sessionId) ?? [];
  unmatchedSessionEvents.delete(sessionId);
  unmatchedSessionEvents.set(sessionId, [...existing, event].slice(-100));
  while (unmatchedSessionEvents.size > 256) {
    const oldest = unmatchedSessionEvents.keys().next().value;
    if (typeof oldest !== "string") break;
    unmatchedSessionEvents.delete(oldest);
  }
}

function eventData(event: RealtimeEvent) {
  return event.data ?? event.payload ?? {};
}

function eventSessionId(event: RealtimeEvent) {
  return event.controlSessionId ?? event.sessionId;
}

const approvalChoices = new Set<ApprovalChoice>(["once", "session", "always", "deny"]);

function sessionSupportsInteraction(
  state: ReturnType<typeof useAppStore.getState>,
  sessionId: string,
  flag: "approvals" | "clarifications",
  method: "approval.respond" | "clarify.respond",
  profileOverride?: Profile,
) {
  const session = state.sessions.find((item) => item.id === sessionId);
  const profile = profileOverride ?? (session && state.profiles.find((item) => (
    item.id === session.profileId
    || (item.gatewayId === session.gatewayId && item.technicalName === session.profileName)
  )));
  return profile?.mutable === true
    && profile.capabilities?.[flag] === true
    && profile.capabilitySet?.methods.includes(method) === true;
}

function boundedString(value: unknown, maximum = 4_000) {
  return typeof value === "string" ? value.slice(0, maximum) : "";
}

function stringList(value: unknown, maximum = 16) {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is string => typeof item === "string")
    .map((item) => item.slice(0, 240))
    .slice(0, maximum);
}

function payloadValue(data: Record<string, unknown>, snakeCase: string, camelCase: string) {
  return data[snakeCase] ?? data[camelCase];
}

function parseApprovalRequest(sessionId: string, data: Record<string, unknown>): ApprovalRequest | null {
  const requestId = boundedString(payloadValue(data, "request_id", "requestId"), 128);
  if (!requestId) return null;
  const allowSession = payloadValue(data, "allow_session", "allowSession") !== false;
  const allowPermanent = payloadValue(data, "allow_permanent", "allowPermanent") !== false;
  const smartDenied = payloadValue(data, "smart_denied", "smartDenied") === true;
  const advertised = stringList(data.choices).filter((choice): choice is ApprovalChoice => (
    approvalChoices.has(choice as ApprovalChoice)
  ));
  const choices = advertised.length
    ? [...new Set(advertised)]
    : [
        "once" as const,
        ...(!smartDenied && allowSession ? ["session" as const] : []),
        ...(!smartDenied && allowSession && allowPermanent ? ["always" as const] : []),
        "deny" as const,
      ];
  return {
    requestId,
    sessionId,
    command: boundedString(data.command, 8_000),
    description: boundedString(data.description),
    choices,
    patternKeys: stringList(payloadValue(data, "pattern_keys", "patternKeys"), 32),
    allowSession,
    allowPermanent,
    smartDenied,
    state: "pending",
  };
}

function clarificationQuestion(value: unknown, batch: boolean): ClarificationQuestion | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const question = boundedString(row.question);
  const questionId = boundedString(payloadValue(row, "qid", "questionId"), 128);
  if (!question || (batch && !questionId)) return null;
  return {
    ...(questionId ? { questionId } : {}),
    question,
    choices: stringList(row.choices, 32),
    multiSelect: payloadValue(row, "multi_select", "multiSelect") === true,
  };
}

function parseClarificationRequest(sessionId: string, data: Record<string, unknown>): ClarificationRequest | null {
  const requestId = boundedString(payloadValue(data, "request_id", "requestId"), 128);
  if (!requestId) return null;
  const rawQuestions = data.questions;
  const batch = Array.isArray(rawQuestions) && rawQuestions.length > 0;
  const questions = batch
    ? rawQuestions.slice(0, 16).map((row) => clarificationQuestion(row, true)).filter((row): row is ClarificationQuestion => row !== null)
    : [clarificationQuestion(data, false)].filter((row): row is ClarificationQuestion => row !== null);
  if (!questions.length) return null;
  const rawAnswers = data.answers;
  const answers = rawAnswers && typeof rawAnswers === "object" && !Array.isArray(rawAnswers)
    ? Object.fromEntries(Object.entries(rawAnswers).flatMap(([questionId, answer]) => {
        if (!questions.some((question) => question.questionId === questionId)) return [];
        if (typeof answer === "string") return [[questionId, answer.slice(0, 4_000)]];
        if (Array.isArray(answer)) return [[questionId, JSON.stringify(stringList(answer, 32))]];
        return [];
      }))
    : {};
  return {
    requestId,
    sessionId,
    batch,
    questions,
    answers,
    remainingQuestionIds: batch
      ? questions.flatMap((question) => question.questionId && !(question.questionId in answers) ? [question.questionId] : [])
      : undefined,
    state: "pending",
  };
}

const usageFieldMap = {
  input: "inputTokens",
  output: "outputTokens",
  prompt: "promptTokens",
  completion: "completionTokens",
  total: "totalTokens",
  calls: "apiCalls",
  context_used: "contextUsed",
  context_max: "contextMax",
  context_percent: "contextPercent",
  compressions: "compressions",
  active_subagents: "activeSubagents",
} as const satisfies Record<string, Exclude<keyof SessionUsage, "reportedAt">>;

function normalizeSessionUsage(value: unknown, reportedAt?: string): SessionUsage | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const usage: SessionUsage = {};
  for (const [source, target] of Object.entries(usageFieldMap)) {
    const candidate = (value as Record<string, unknown>)[source];
    if (typeof candidate !== "number" || !Number.isFinite(candidate) || candidate < 0) continue;
    const maximum = target === "contextPercent" ? 100 : Number.MAX_SAFE_INTEGER;
    usage[target] = target === "contextPercent"
      ? Math.round(Math.min(candidate, maximum) * 1000) / 1000
      : Math.trunc(Math.min(candidate, maximum));
  }
  if (!Object.keys(usage).length) return undefined;
  if (reportedAt) usage.reportedAt = reportedAt;
  return usage;
}

const MAX_AGENT_ACTIVITY_ITEMS = 80;
const MAX_AGENT_ACTIVITY_TEXT = 240;

function boundedAgentActivityText(value: unknown, fallback = "") {
  if (typeof value !== "string") return fallback;
  const compact = value.replace(/\s+/g, " ").trim();
  return compact ? compact.slice(0, MAX_AGENT_ACTIVITY_TEXT) : fallback;
}

function nextAgentActivity(current: ChatMessage | undefined, item: AgentActivityItem) {
  const existing = (current?.activity ?? []).filter((entry) => entry.id !== item.id);
  return [...existing, item].slice(-MAX_AGENT_ACTIVITY_ITEMS);
}

const MAX_EMAIL_REFERENCES = 8;
const HTML_COMMENT_START = "<!--";
const EMAIL_REFERENCE_MARKER_NAME = "hermes-control-email-reference-v1";
const EMAIL_REFERENCE_MARKER_END = "-->";
const MAX_STREAMING_MARKER_STATES = 32;
const MAX_STREAMING_MARKER_TOMBSTONES = 256;
const MAX_STREAMING_MARKER_QUARANTINED_SESSIONS = 64;
const MAX_MARKER_LEADING_WHITESPACE = 64;

function boundedOptionalText(value: unknown, maximum: number) {
  if (typeof value !== "string") return undefined;
  const compact = value.replace(/\s+/g, " ").trim();
  return compact ? compact.slice(0, maximum) : undefined;
}

function exactControlEmailUrl(value: unknown, sessionId: string, referenceId: string, suffix = "") {
  if (typeof value !== "string") return undefined;
  const expectedPath = `/api/v1/sessions/${encodeURIComponent(sessionId)}/email-references/${encodeURIComponent(referenceId)}${suffix}`;
  try {
    const parsed = new URL(value, window.location.origin);
    if (
      parsed.origin !== window.location.origin
      || parsed.pathname !== expectedPath
      || parsed.search
      || parsed.hash
    ) return undefined;
    return expectedPath;
  } catch {
    return undefined;
  }
}

function emailReferencesFromValue(sessionId: string, value: unknown): EmailReference[] {
  if (!Array.isArray(value)) return [];
  const references = value.slice(0, MAX_EMAIL_REFERENCES).flatMap((candidate) => {
    if (!candidate || typeof candidate !== "object" || Array.isArray(candidate)) return [];
    const row = candidate as Record<string, unknown>;
    if (
      row.schemaVersion !== 1
      || (row.provider !== "gmail" && row.provider !== "outlook" && row.provider !== "imap")
    ) return [];
    const provider: EmailReference["provider"] = row.provider;
    const id = boundedOptionalText(row.id, 160);
    const subject = boundedOptionalText(row.subject, 500);
    if (!id || !subject || !/^[0-9a-f]{32}$/.test(id)) return [];
    const previewUrl = exactControlEmailUrl(row.previewUrl, sessionId, id);
    if (!previewUrl) return [];
    const openUrl = exactControlEmailUrl(row.openUrl, sessionId, id, "/open");
    const openMode: EmailReference["openMode"] = row.openMode === "direct" || row.openMode === "search"
      ? row.openMode
      : undefined;
    return [{
      schemaVersion: 1 as const,
      id,
      provider,
      subject,
      previewUrl,
      ...(boundedOptionalText(row.senderName, 200) ? { senderName: boundedOptionalText(row.senderName, 200) } : {}),
      ...(boundedOptionalText(row.senderAddress, 320) ? { senderAddress: boundedOptionalText(row.senderAddress, 320) } : {}),
      ...(boundedOptionalText(row.receivedAt, 80) ? { receivedAt: boundedOptionalText(row.receivedAt, 80) } : {}),
      ...(boundedOptionalText(row.snippet, 1_000) ? { snippet: boundedOptionalText(row.snippet, 1_000) } : {}),
      ...(openUrl ? { openUrl } : {}),
      ...(openUrl && openMode ? { openMode } : {}),
    }];
  });
  return [...new Map(references.map((reference) => [reference.id, reference])).values()];
}

function longestTokenPrefixSuffix(value: string, token: string) {
  const maximum = Math.min(token.length - 1, value.length);
  for (let length = maximum; length >= 1; length -= 1) {
    if (value.endsWith(token.slice(0, length))) return length;
  }
  return 0;
}

function rememberStreamingMarkerState(messageId: string, state?: StreamingMarkerState) {
  streamingMarkerStates.delete(messageId);
  if (state) streamingMarkerStates.set(messageId, state);
  while (streamingMarkerStates.size > MAX_STREAMING_MARKER_STATES) {
    const oldest = streamingMarkerStates.keys().next().value;
    if (typeof oldest !== "string") break;
    const evicted = streamingMarkerStates.get(oldest);
    streamingMarkerStates.delete(oldest);
    if (!evicted) continue;
    streamingMarkerTombstones.delete(oldest);
    streamingMarkerTombstones.set(oldest, evicted.sessionId);
    while (streamingMarkerTombstones.size > MAX_STREAMING_MARKER_TOMBSTONES) {
      const oldestTombstone = streamingMarkerTombstones.keys().next().value;
      if (typeof oldestTombstone !== "string") break;
      const quarantinedSession = streamingMarkerTombstones.get(oldestTombstone);
      streamingMarkerTombstones.delete(oldestTombstone);
      if (quarantinedSession) streamingMarkerQuarantinedSessions.add(quarantinedSession);
      if (streamingMarkerQuarantinedSessions.size > MAX_STREAMING_MARKER_QUARANTINED_SESSIONS) {
        streamingMarkerGlobalQuarantine = true;
        streamingMarkerQuarantinedSessions.clear();
        streamingMarkerTombstones.clear();
      }
    }
  }
}

function clearStreamingMarkerStatesForSession(sessionId: string) {
  streamingMarkerStates.forEach((state, messageId) => {
    if (state.sessionId === sessionId) streamingMarkerStates.delete(messageId);
  });
  streamingMarkerTombstones.forEach((tombstoneSessionId, messageId) => {
    if (tombstoneSessionId === sessionId) streamingMarkerTombstones.delete(messageId);
  });
  streamingMarkerQuarantinedSessions.delete(sessionId);
}

export function clearStreamingContentFilterState() {
  streamingMarkerStates.clear();
  streamingMarkerTombstones.clear();
  streamingMarkerQuarantinedSessions.clear();
  streamingMarkerGlobalQuarantine = false;
}

export function streamingContentFilterStatsForTests() {
  const pendingLengths = [...streamingMarkerStates.values()]
    .map((state) => state.pending.length + state.leadingWhitespace.length);
  return {
    entries: streamingMarkerStates.size,
    tombstones: streamingMarkerTombstones.size,
    quarantinedSessions: streamingMarkerQuarantinedSessions.size,
    globalQuarantine: streamingMarkerGlobalQuarantine,
    retainedCharacters: pendingLengths.reduce((total, length) => total + length, 0),
    largestPendingCharacters: Math.max(0, ...pendingLengths),
  };
}

/**
 * Transport markers may cross arbitrary realtime chunk boundaries. Retain only
 * the possible opening/closing-token suffix (never the response or marker
 * payload), and discard marker bytes incrementally. Hermes/Control remains the
 * security boundary; this filter prevents a distracting browser-side flash.
 */
function visibleStreamingDelta(messageId: string, sessionId: string, delta: string) {
  if (
    streamingMarkerGlobalQuarantine
    || streamingMarkerTombstones.has(messageId)
    || streamingMarkerQuarantinedSessions.has(sessionId)
  ) return "";
  const remembered = streamingMarkerStates.get(messageId);
  let mode = remembered?.mode ?? "outside";
  let pending = remembered?.pending ?? "";
  let leadingWhitespace = remembered?.leadingWhitespace ?? "";
  let remaining = delta;
  let visible = "";

  while (remaining) {
    const source = `${pending}${remaining}`;
    pending = "";
    remaining = "";

    if (mode === "marker") {
      const markerEnd = source.indexOf(EMAIL_REFERENCE_MARKER_END);
      if (markerEnd >= 0) {
        mode = "outside";
        remaining = source.slice(markerEnd + EMAIL_REFERENCE_MARKER_END.length);
        continue;
      }
      const suffixLength = longestTokenPrefixSuffix(source, EMAIL_REFERENCE_MARKER_END);
      pending = suffixLength ? source.slice(-suffixLength) : "";
      continue;
    }

    if (mode === "candidate") {
      const firstNonWhitespace = source.search(/\S/);
      if (firstNonWhitespace < 0) {
        if (source.length > MAX_MARKER_LEADING_WHITESPACE) {
          // An oversized comment prelude is not retained on constrained
          // clients. Suppress it until the comment closes, then rehydrate.
          leadingWhitespace = "";
          mode = "marker";
        } else {
          pending = source;
        }
        continue;
      }
      const markerCandidate = source.slice(firstNonWhitespace);
      const lowerCandidate = markerCandidate.toLowerCase();
      if (EMAIL_REFERENCE_MARKER_NAME.startsWith(lowerCandidate)) {
        pending = source;
        continue;
      }
      if (lowerCandidate.startsWith(EMAIL_REFERENCE_MARKER_NAME)) {
        leadingWhitespace = "";
        mode = "marker";
        remaining = markerCandidate.slice(EMAIL_REFERENCE_MARKER_NAME.length);
        continue;
      }
      visible += `${leadingWhitespace}${HTML_COMMENT_START}`;
      leadingWhitespace = "";
      mode = "outside";
      remaining = source;
      continue;
    }

    const commentStart = source.indexOf(HTML_COMMENT_START);
    if (commentStart >= 0) {
      const beforeComment = `${leadingWhitespace}${source.slice(0, commentStart)}`;
      const whitespace = beforeComment.match(/\s+$/)?.[0] ?? "";
      leadingWhitespace = whitespace.slice(-MAX_MARKER_LEADING_WHITESPACE);
      visible += beforeComment.slice(0, beforeComment.length - leadingWhitespace.length);
      mode = "candidate";
      remaining = source.slice(commentStart + HTML_COMMENT_START.length);
      continue;
    }

    const suffixLength = longestTokenPrefixSuffix(source, HTML_COMMENT_START);
    if (!suffixLength) {
      visible += `${leadingWhitespace}${source}`;
      leadingWhitespace = "";
      continue;
    }
    const beforeSuffix = `${leadingWhitespace}${source.slice(0, -suffixLength)}`;
    const whitespace = beforeSuffix.match(/\s+$/)?.[0] ?? "";
    leadingWhitespace = whitespace.slice(-MAX_MARKER_LEADING_WHITESPACE);
    visible += beforeSuffix.slice(0, beforeSuffix.length - leadingWhitespace.length);
    pending = source.slice(-suffixLength);
  }

  rememberStreamingMarkerState(messageId, mode !== "outside" || pending || leadingWhitespace
    ? { sessionId, mode, pending, leadingWhitespace }
    : undefined);
  return visible;
}

function mergeEmailReferences(current: EmailReference[] | undefined, incoming: EmailReference[]) {
  const references = new Map((current ?? []).map((reference) => [reference.id, reference]));
  incoming.forEach((reference) => references.set(reference.id, reference));
  return [...references.values()].slice(-MAX_EMAIL_REFERENCES);
}

export function applyRealtimeEvent(event: RealtimeEvent): boolean {
  const state = useAppStore.getState();
  const data = eventData(event);
  const operationId = event.correlationId;
  const routeSessionId = eventSessionId(event);
  const terminalStreamEvent = TERMINAL_STREAM_EVENTS.has(event.type);
  const messageId = (operationId ? state.pendingOperations[operationId] : undefined)
    ?? (routeSessionId ? state.streamingBySession[routeSessionId] : undefined);

  if (event.type === "control.connection") {
    const selectedProfile = state.profiles.find((profile) => profile.id === state.selectedProfileId);
    const matchesSelectedRoute = event.gatewayId === state.selectedGatewayId
      && event.profileName === selectedProfile?.technicalName;
    const connectionState = data.state;
    if (matchesSelectedRoute && (
      connectionState === "connected"
      || connectionState === "reconnecting"
      || connectionState === "offline"
    )) {
      state.setConnection(connectionState);
    }
    return true;
  }

  if (event.type.startsWith("run.") || event.type.startsWith("cron.")) {
    // Automation runs are not chat messages. Notify the administration screen
    // independently so queued/running rows converge when terminal events land.
    window.dispatchEvent(new CustomEvent("hermes-control:automation-event", { detail: event }));
  }

  if (event.reconciliationRequired || event.type === "control.stream.overflow") {
    if (routeSessionId) void rehydrateSession(routeSessionId);
    return true;
  }

  if (event.type === "approval.request" && routeSessionId) {
    const request = parseApprovalRequest(routeSessionId, data);
    if (!request) return false;
    bumpInteractionRevision(routeSessionId);
    state.upsertApproval(request);
    return true;
  }
  if (["approval.expire", "approval.resolved"].includes(event.type) && routeSessionId) {
    const requestId = boundedString(payloadValue(data, "request_id", "requestId"), 128);
    if (!requestId) return false;
    bumpInteractionRevision(routeSessionId);
    state.removeApproval(routeSessionId, requestId);
    return true;
  }
  if (event.type === "clarify.request" && routeSessionId) {
    const request = parseClarificationRequest(routeSessionId, data);
    if (!request) return false;
    bumpInteractionRevision(routeSessionId);
    state.upsertClarification(request);
    return true;
  }
  if (["clarify.expire", "clarify.resolved"].includes(event.type) && routeSessionId) {
    const requestId = boundedString(payloadValue(data, "request_id", "requestId"), 128);
    if (!requestId) return false;
    bumpInteractionRevision(routeSessionId);
    state.removeClarification(routeSessionId, requestId);
    return true;
  }

  if (terminalStreamEvent && routeSessionId) {
    bumpInteractionRevision(routeSessionId);
    state.clearSessionInteractions(routeSessionId);
    if (event.type.startsWith("message.")) {
      recordSessionCompletion(routeSessionId, event.occurredAt);
    }
  }

  const usageSessionId = routeSessionId
    ?? (messageId ? state.messages.find((message) => message.id === messageId)?.sessionId : undefined);
  const hasUsageSnapshot = Boolean(data.usage && typeof data.usage === "object" && !Array.isArray(data.usage));
  const usage = hasUsageSnapshot ? normalizeSessionUsage(data.usage, event.occurredAt) : undefined;
  if (usageSessionId && hasUsageSnapshot) state.setSessionUsage(usageSessionId, usage);
  if (event.type === "session.usage") return Boolean(usageSessionId && hasUsageSnapshot);
  if (!messageId) {
    if (terminalStreamEvent && routeSessionId) clearStreamingMarkerStatesForSession(routeSessionId);
    if (operationId) bufferUnmatchedEvent(operationId, event);
    else if (
      routeSessionId
      && (
        event.type.startsWith("message.")
        || event.type.startsWith("tool.")
        || event.type === "reasoning.omitted"
        || event.type.startsWith("delegation.")
        || event.type.startsWith("subagent.")
        || terminalStreamEvent
      )
    ) {
      // A mobile OS may evict the PWA process while Hermes continues. Keep
      // route-scoped frames until history reconstructs the active response.
      bufferUnmatchedSessionEvent(routeSessionId, event);
    }
    if (terminalStreamEvent && routeSessionId) void rehydrateSession(routeSessionId);
    return terminalStreamEvent || Boolean(usageSessionId && hasUsageSnapshot);
  }
  if (event.type === "reasoning.omitted") {
    // Hermes can emit many private-reasoning deltas. Their contents never
    // cross the public boundary; the UI represents them with one generic,
    // truthful "analyzing" state instead of exposing or fabricating text.
    return true;
  }
  const currentMessage = state.messages.find((message) => message.id === messageId);
  const realtimeEmailReferences = emailReferencesFromValue(
    routeSessionId ?? currentMessage?.sessionId ?? "",
    data.controlEmailReferences,
  );
  if (realtimeEmailReferences.length) {
    state.updateMessage(messageId, {
      emailReferences: mergeEmailReferences(currentMessage?.emailReferences, realtimeEmailReferences),
    });
  }
  if (event.type === "message.delta") {
    const current = state.messages.find((message) => message.id === messageId);
    const sessionId = routeSessionId ?? current?.sessionId ?? "";
    const visibleDelta = visibleStreamingDelta(
      messageId,
      sessionId,
      String(data.delta ?? data.text ?? data.content ?? ""),
    );
    state.updateMessage(messageId, { content: `${current?.content ?? ""}${visibleDelta}`, streaming: true });
    return true;
  }
  if (terminalStreamEvent) {
    const completedSessionId = routeSessionId ?? state.messages.find((message) => message.id === messageId)?.sessionId;
    if (completedSessionId) clearStreamingMarkerStatesForSession(completedSessionId);
    else {
      streamingMarkerStates.delete(messageId);
      streamingMarkerTombstones.delete(messageId);
    }
    state.updateMessage(messageId, { streaming: false });
    if (completedSessionId) state.setStreamingMessageId(completedSessionId, undefined);
    if (operationId) state.clearOperation(operationId);
    else {
      Object.entries(state.pendingOperations)
        .filter(([, pendingMessageId]) => pendingMessageId === messageId)
        .forEach(([pendingOperationId]) => state.clearOperation(pendingOperationId));
    }
    if (completedSessionId) void rehydrateSession(completedSessionId);
    return true;
  }
  if (event.type.startsWith("tool.")) {
    const current = state.messages.find((message) => message.id === messageId);
    const requestedToolName = typeof data.name === "string" ? data.name : undefined;
    const activeTool = [...(current?.tools ?? [])].reverse().find((tool) => tool.status === "running" && (!requestedToolName || tool.name === requestedToolName));
    const toolName = requestedToolName ?? activeTool?.name ?? "tool";
    const toolId = String(payloadValue(data, "tool_id", "toolId") ?? data.id ?? activeTool?.id ?? event.eventId ?? event.id ?? crypto.randomUUID());
    const status = ["tool.error", "tool.failed"].includes(event.type) || event.type.endsWith(".error") || event.type.endsWith(".failed")
      ? "failed"
      : ["tool.complete", "tool.completed"].includes(event.type) || event.type.endsWith(".complete") || event.type.endsWith(".completed")
        ? "completed"
        : "running";
    const nextTool = {
      id: toolId,
      name: toolName,
      label: boundedAgentActivityText(data.label ?? data.name ?? activeTool?.label, "Herramienta"),
      status: status as "running" | "completed" | "failed",
      summary: boundedAgentActivityText(data.summary ?? activeTool?.summary, "Actividad de Hermes"),
    };
    const tools = [...(current?.tools ?? []).filter((tool) => tool.id !== toolId), nextTool];
    const activityId = event.eventId ?? event.id ?? crypto.randomUUID();
    const activity = nextAgentActivity(current, {
      id: activityId,
      kind: "tool",
      label: nextTool.label,
      summary: nextTool.summary,
      status: nextTool.status,
    });
    state.updateMessage(messageId, { tools, activity });
    return true;
  }
  if (event.type.startsWith("delegation.") || event.type.startsWith("subagent.")) {
    const current = state.messages.find((message) => message.id === messageId);
    const failed = event.type.endsWith(".error") || event.type.endsWith(".failed");
    const completed = event.type.endsWith(".complete") || event.type.endsWith(".completed") || event.type.endsWith(".done");
    const status = failed ? "failed" : completed ? "completed" : "running";
    const activity = nextAgentActivity(current, {
      id: event.eventId ?? event.id ?? crypto.randomUUID(),
      kind: "delegation",
      label: boundedAgentActivityText(data.label ?? data.name ?? data.title, "Subagente"),
      summary: boundedAgentActivityText(data.summary ?? data.status ?? data.reason, "Trabajo delegado"),
      status,
    });
    state.updateMessage(messageId, { activity });
    return true;
  }
  return false;
}

type HistoryToolItem = {
  id: string;
  sessionId: string;
  role: "tool";
  createdAt: string;
  tool: ToolRun;
  emailReferences: EmailReference[];
};

type MappedHistoryItem = ChatMessage | HistoryToolItem;

function historyTime(item: Record<string, unknown>) {
  const raw = item.createdAt ?? item.created_at ?? item.timestamp;
  const date = typeof raw === "number"
    ? new Date(raw > 10_000_000_000 ? raw : raw * 1_000)
    : typeof raw === "string" ? new Date(raw) : null;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleTimeString("es-MX", { hour: "2-digit", minute: "2-digit" })
    : "";
}

function boundedToolSummary(value: unknown) {
  if (typeof value !== "string") return "Resultado registrado por Hermes";
  const compact = value.replace(/\s+/g, " ").trim();
  if (!compact) return "Resultado registrado por Hermes";
  return compact.length > 180 ? `${compact.slice(0, 177)}…` : compact;
}

function toolFromHistory(item: Record<string, unknown>, fallbackId: string): ToolRun {
  const name = String(item.name ?? item.tool_name ?? item.toolName ?? "tool");
  const rawStatus = String(item.status ?? item.state ?? "completed").toLowerCase();
  const failed = Boolean(item.error ?? item.is_error ?? item.isError)
    || rawStatus === "error"
    || rawStatus === "failed";
  const duration = item.durationMs ?? item.duration_ms;
  return {
    id: String(item.tool_call_id ?? item.toolCallId ?? item.id ?? fallbackId),
    name,
    label: String(item.label ?? name.replace(/[_-]+/g, " ")),
    status: failed ? "failed" : rawStatus === "running" ? "running" : "completed",
    durationMs: typeof duration === "number" && Number.isFinite(duration) ? duration : undefined,
    summary: boundedToolSummary(item.context ?? item.summary ?? item.content ?? item.text),
  };
}

function mediaFromHistory(item: Record<string, unknown>) {
  if (!Array.isArray(item.controlMedia)) return [];
  return item.controlMedia.slice(0, 16).flatMap((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const media = value as Record<string, unknown>;
    if (
      media.kind !== "audio"
      || typeof media.id !== "string"
      || !/^[0-9a-f]{32}$/.test(media.id)
      || typeof media.mediaType !== "string"
      || !media.mediaType.startsWith("audio/")
    ) return [];
    return [{ id: media.id, kind: "audio" as const, mediaType: media.mediaType }];
  });
}

function attachmentsFromHistory(item: Record<string, unknown>) {
  if (!Array.isArray(item.controlAttachments)) return [];
  return item.controlAttachments.slice(0, 5).flatMap((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const attachment = value as Record<string, unknown>;
    if (
      (attachment.kind !== "image" && attachment.kind !== "file")
      || typeof attachment.name !== "string"
      || !attachment.name
      || typeof attachment.mediaType !== "string"
      || typeof attachment.size !== "number"
      || !Number.isFinite(attachment.size)
      || attachment.size <= 0
      || attachment.size > 8 * 1024 * 1024
    ) return [];
    return [{
      kind: attachment.kind as "image" | "file",
      name: attachment.name,
      mediaType: attachment.mediaType,
      size: attachment.size,
    }];
  });
}

function mapHistoryItem(sessionId: string, item: Record<string, unknown>, index: number): MappedHistoryItem | null {
  const role = item.role;
  const createdAt = historyTime(item);
  if (role === "tool") {
    const id = String(item.id ?? item.tool_call_id ?? item.toolCallId ?? `${sessionId}-history-tool-${index}`);
    return {
      id,
      sessionId,
      role: "tool",
      createdAt,
      tool: toolFromHistory(item, id),
      emailReferences: emailReferencesFromValue(sessionId, item.controlEmailReferences),
    };
  }
  if (role !== "user" && role !== "assistant" && role !== "system") return null;
  const media = mediaFromHistory(item);
  const attachments = attachmentsFromHistory(item);
  const emailReferences = emailReferencesFromValue(sessionId, item.controlEmailReferences);
  const content = typeof item.content === "string" ? item.content : typeof item.text === "string" ? item.text : "";
  if (!content && !media.length && !attachments.length && !emailReferences.length) return null;
  return {
    id: String(item.id ?? `${sessionId}-history-${index}`),
    sessionId,
    role,
    content,
    createdAt,
    delivery: role === "user" ? "sent" : undefined,
    ...(media.length ? { media } : {}),
    ...(attachments.length ? { attachments } : {}),
    ...(emailReferences.length ? { emailReferences } : {}),
  };
}

function historyMessages(sessionId: string, items: Record<string, unknown>[]): ChatMessage[] {
  const mapped = items
    .map((item, index) => mapHistoryItem(sessionId, item, index))
    .filter((item): item is MappedHistoryItem => item !== null);
  const messages: ChatMessage[] = [];
  let pendingTools: ToolRun[] = [];
  let pendingEmailReferences: EmailReference[] = [];

  const attachPendingEvidence = () => {
    if (!pendingTools.length && !pendingEmailReferences.length) return;
    let assistantIndex = -1;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === "user") break;
      if (messages[index].role === "assistant") {
        assistantIndex = index;
        break;
      }
    }
    if (assistantIndex < 0) {
      messages.push({
        id: `${sessionId}-history-tools-${messages.length}`,
        sessionId,
        role: "assistant",
        content: "",
        createdAt: "",
        ...(pendingTools.length ? { tools: pendingTools } : {}),
        ...(pendingEmailReferences.length ? { emailReferences: pendingEmailReferences } : {}),
      });
    } else {
      const assistant = messages[assistantIndex];
      const byId = new Map((assistant.tools ?? []).map((tool) => [tool.id, tool]));
      pendingTools.forEach((tool) => byId.set(tool.id, tool));
      messages[assistantIndex] = {
        ...assistant,
        ...(byId.size ? { tools: [...byId.values()] } : {}),
        ...(pendingEmailReferences.length ? {
          emailReferences: mergeEmailReferences(assistant.emailReferences, pendingEmailReferences),
        } : {}),
      };
    }
    pendingTools = [];
    pendingEmailReferences = [];
  };

  mapped.forEach((item) => {
    if (item.role === "tool") {
      const existing = pendingTools.findIndex((tool) => tool.id === item.tool.id);
      if (existing >= 0) pendingTools[existing] = item.tool;
      else pendingTools.push(item.tool);
      pendingEmailReferences = mergeEmailReferences(pendingEmailReferences, item.emailReferences);
      return;
    }
    if (item.role === "user") attachPendingEvidence();
    if (item.role === "assistant" && (pendingTools.length || pendingEmailReferences.length)) {
      const byId = new Map((item.tools ?? []).map((tool) => [tool.id, tool]));
      pendingTools.forEach((tool) => byId.set(tool.id, tool));
      messages.push({
        ...item,
        ...(byId.size ? { tools: [...byId.values()] } : {}),
        ...(pendingEmailReferences.length ? {
          emailReferences: mergeEmailReferences(item.emailReferences, pendingEmailReferences),
        } : {}),
      });
      pendingTools = [];
      pendingEmailReferences = [];
      return;
    }
    messages.push(item);
  });
  attachPendingEvidence();
  return messages;
}

function appendTerminalHistoryNotice(
  sessionId: string,
  sessionStatus: string,
  history: Array<Record<string, unknown>>,
  messages: ChatMessage[],
) {
  let lastUserIndex = -1;
  for (let index = history.length - 1; index >= 0; index -= 1) {
    if (history[index]?.role !== "user") continue;
    lastUserIndex = index;
    break;
  }
  if (lastUserIndex < 0) return;
  const hasFinalResponse = history.slice(lastUserIndex + 1).some((item) => {
    if (item?.role !== "assistant") return false;
    if (item.tool_calls || item.toolCalls) return false;
    const finishReason = String(item.finish_reason ?? item.finishReason ?? "").trim().toLowerCase();
    if (["tool_calls", "tool_call", "function_call"].includes(finishReason)) return false;
    const content = item.content ?? item.text;
    const hasContent = typeof content === "string" && Boolean(content.trim());
    const hasMedia = Array.isArray(item.controlMedia) && item.controlMedia.length > 0;
    return hasContent
      || hasMedia
      || ["stop", "completed", "complete", "done", "end_turn"].includes(finishReason);
  });
  if (hasFinalResponse) return;
  const content = sessionStatus === "interrupted"
    ? i18n.t("runtimeMessages.interruptedNoResponse")
    : sessionStatus === "error"
      ? i18n.t("runtimeMessages.operationFailed")
      : ["ready", "completed", "idle"].includes(sessionStatus)
        ? i18n.t("runtimeMessages.noFinalResponse")
        : "";
  if (!content) return;
  messages.push({
    id: `control-terminal-${sessionId}-${lastUserIndex}`,
    sessionId,
    role: "assistant",
    content,
    createdAt: "",
  });
}

async function resumeActiveSession(sessionId: string) {
  const existing = activeSessionRecoveries.get(sessionId);
  if (existing) return existing;
  const state = useAppStore.getState();
  if (state.authState !== "authenticated") return;
  const recovery = api.resumeSession(sessionId, state.csrfToken).then(() => undefined);
  activeSessionRecoveries.set(sessionId, recovery);
  try {
    await recovery;
  } finally {
    if (activeSessionRecoveries.get(sessionId) === recovery) {
      activeSessionRecoveries.delete(sessionId);
    }
  }
}

export async function rehydrateSession(sessionId: string, recoveryAttempted = false) {
  nextRehydrationGeneration += 1;
  const generation = nextRehydrationGeneration;
  rehydrationGenerations.delete(sessionId);
  rehydrationGenerations.set(sessionId, generation);
  while (rehydrationGenerations.size > 256) {
    const oldest = rehydrationGenerations.keys().next().value;
    if (typeof oldest !== "string") break;
    rehydrationGenerations.delete(oldest);
  }
  try {
    const interactionRevisionAtStart = interactionRevision(sessionId);
    const history = await api.sessionHistory(sessionId);
    // Reconnect, replay and terminal events can all request history at nearly
    // the same time. Never let a slower, older active snapshot overwrite a
    // newer terminal transcript.
    if (rehydrationGenerations.get(sessionId) !== generation) return;
    const messages = historyMessages(sessionId, history.items);
    const state = useAppStore.getState();
    const activeOperation = history.activeOperation;
    const operationIsActive = Boolean(
      activeOperation
      && typeof activeOperation.operationId === "string"
      && activeOperation.operationId.length > 0
      && activeOperation.operationId.length <= 200
      && ["pending", "accepted", "streaming", "delivery_unknown"].includes(activeOperation.status),
    );
    if (
      Array.isArray(history.pendingInteractions)
      && interactionRevision(sessionId) === interactionRevisionAtStart
    ) {
      // History is authoritative for the current runtime, but an approval
      // response can be in flight while this read runs. Preserve local
      // submitting/unknown-delivery locks until that mutation settles; an
      // empty snapshot must never make the user able to send it twice.
      const approvalIds = new Set<string>();
      const clarificationIds = new Set<string>();
      const sessionMayBeActive = operationIsActive || ACTIVE_SESSION_STATUSES.has(
        String(history.sessionStatus || "").toLowerCase(),
      );
      if (sessionMayBeActive) {
        history.pendingInteractions.forEach((interaction) => {
          if (!interaction || typeof interaction.data !== "object" || Array.isArray(interaction.data)) return;
          if (interaction.type === "approval.request") {
            const request = parseApprovalRequest(sessionId, interaction.data);
            if (request) {
              approvalIds.add(request.requestId);
              state.upsertApproval(request);
            }
          } else if (interaction.type === "clarify.request") {
            const request = parseClarificationRequest(sessionId, interaction.data);
            if (request) {
              clarificationIds.add(request.requestId);
              state.upsertClarification(request);
            }
          }
        });
      }
      (state.approvalsBySession[sessionId] ?? []).forEach((request) => {
        if (
          !approvalIds.has(request.requestId)
          && (
            request.state !== "submitting"
            && !(request.state === "ambiguous" && sessionMayBeActive)
          )
        ) state.removeApproval(sessionId, request.requestId);
      });
      (state.clarificationsBySession[sessionId] ?? []).forEach((request) => {
        if (
          !clarificationIds.has(request.requestId)
          && (
            request.state !== "submitting"
            && !(request.state === "ambiguous" && sessionMayBeActive)
          )
        ) state.removeClarification(sessionId, request.requestId);
      });
    }

    if (operationIsActive && activeOperation) {
      const existingStreamId = state.streamingBySession[sessionId];
      const existingStream = existingStreamId
        ? state.messages.find((message) => (
          message.id === existingStreamId && message.sessionId === sessionId
        ))
        : undefined;
      const streamId = existingStream?.id
        ?? `recovered-${sessionId}-${activeOperation.operationId}`;
      if (!existingStream) {
        messages.push({
          id: streamId,
          sessionId,
          role: "assistant",
          content: "",
          createdAt: "",
          streaming: true,
        });
      }
      state.setMessagesForSession(sessionId, messages);
      state.setStreamingMessageId(sessionId, streamId);
      state.bindOperation(activeOperation.operationId, streamId);

      const buffered = [
        ...(unmatchedEvents.get(activeOperation.operationId) ?? []),
        ...(unmatchedSessionEvents.get(sessionId) ?? []),
      ];
      unmatchedEvents.delete(activeOperation.operationId);
      unmatchedSessionEvents.delete(sessionId);
      buffered.forEach(applyRealtimeEvent);
      if (activeOperation.recoveryRequired === true && !recoveryAttempted) {
        // A Control/gateway reconnect changed the runtime generation. Resume
        // the existing Hermes session through the guarded mutation endpoint;
        // never re-submit the user's prompt because tools may have side effects.
        await resumeActiveSession(sessionId);
        await rehydrateSession(sessionId, true);
      }
      return;
    }

    const staleStreamId = state.streamingBySession[sessionId];
    if (staleStreamId) {
      state.updateMessage(staleStreamId, { streaming: false });
      state.setStreamingMessageId(sessionId, undefined);
      Object.entries(state.pendingOperations)
        .filter(([, messageId]) => messageId === staleStreamId)
        .forEach(([operationId]) => {
          state.clearOperation(operationId);
          unmatchedEvents.delete(operationId);
        });
    }
    clearStreamingMarkerStatesForSession(sessionId);
    unmatchedSessionEvents.delete(sessionId);
    appendTerminalHistoryNotice(sessionId, history.sessionStatus, history.items, messages);
    useAppStore.getState().setMessagesForSession(sessionId, messages);
  } catch {
    if (rehydrationGenerations.get(sessionId) !== generation) return;
    const state = useAppStore.getState();
    const workspaceId = state.sessions.find((session) => session.id === sessionId)?.workspaceId;
    if (state.offlineCacheEnabled && workspaceId) {
      const cached = await loadEncryptedTranscript(sessionId, workspaceId).catch(() => []);
      if (cached.length) state.setMessagesForSession(sessionId, cached);
    }
    state.setConnection("degraded");
  }
}

export function useAuthBootstrap() {
  const authState = useAppStore((state) => state.authState);
  const setAuth = useAppStore((state) => state.setAuth);

  useEffect(() => {
    if (authState === "unauthenticated") clearStreamingContentFilterState();
  }, [authState]);

  useEffect(() => {
    if (authState !== "checking") return;
    let active = true;
    boundedControlRead(api.me())
      .then((user) => { if (active) setAuth("authenticated", user.name, user.csrfToken, false); })
      .catch(async (error) => {
        if (!active) return;
        if (error instanceof ApiError && error.status === 401) {
          await clearPrivateCache().catch(() => undefined);
          setAuth("unauthenticated");
          return;
        }
        const enabled = await loadPreference("offline-cache").catch(() => "false");
        const cached = (
          enabled === "true" ? await loadOfflineSnapshot().catch(() => null) : null
        ) ?? await loadShellSnapshot().catch(() => null);
        if (!active) return;
        if (cached) {
          setAuth("offline", cached.userName, undefined, false);
          useAppStore.getState().hydrateBootstrap(cached.data);
          useAppStore.getState().setConnection("offline");
        } else {
          setAuth("unauthenticated");
        }
      });
    return () => { active = false; };
  }, [authState, setAuth]);

  useEffect(() => {
    if (authState !== "offline") return;
    let active = true;
    let timer: number | undefined;
    let delayMs = 2_000;
    let probing = false;

    const schedule = () => {
      if (!active) return;
      timer = window.setTimeout(() => { void probe(); }, delayMs);
      delayMs = Math.min(delayMs * 2, 30_000);
    };
    const probe = async () => {
      if (probing) return;
      probing = true;
      window.clearTimeout(timer);
      try {
        const user = await boundedControlRead(api.me());
        if (!active || useAppStore.getState().authState !== "offline") return;
        setAuth("authenticated", user.name, user.csrfToken, false);
      } catch (error) {
        if (!active || useAppStore.getState().authState !== "offline") return;
        if (error instanceof ApiError && error.status === 401) {
          await clearPrivateCache().catch(() => undefined);
          setAuth("unauthenticated");
          return;
        }
        schedule();
      } finally {
        probing = false;
      }
    };
    const retryNow = () => {
      delayMs = 2_000;
      void probe();
    };

    // A tunnel/API outage does not change `navigator.onLine`, so periodically
    // probe the same-origin auth endpoint in addition to reacting immediately
    // when the browser does report network recovery.
    schedule();
    window.addEventListener("online", retryNow);
    window.addEventListener("focus", retryNow);
    window.addEventListener("pageshow", retryNow);
    window.addEventListener("agent-control:resume", retryNow);
    return () => {
      active = false;
      window.clearTimeout(timer);
      window.removeEventListener("online", retryNow);
      window.removeEventListener("focus", retryNow);
      window.removeEventListener("pageshow", retryNow);
      window.removeEventListener("agent-control:resume", retryNow);
    };
  }, [authState, setAuth]);

  useEffect(() => {
    const onUnauthorized = () => {
      // A server-authoritative 401 revokes offline access as well. Otherwise a
      // later API outage could resurrect a snapshot belonging to an expired or
      // revoked browser session.
      setAuth("unauthenticated");
      void clearPrivateCache().catch(() => undefined);
    };
    window.addEventListener("hermes-control:unauthorized", onUnauthorized);
    return () => window.removeEventListener("hermes-control:unauthorized", onUnauthorized);
  }, [setAuth]);
}

export function useBootstrapData() {
  const authState = useAppStore((state) => state.authState);
  const demoMode = useAppStore((state) => state.demoMode);
  const bootstrapLoaded = useAppStore((state) => state.bootstrapLoaded);
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const setConnection = useAppStore((state) => state.setConnection);
  const csrfToken = useAppStore((state) => state.csrfToken);

  useEffect(() => {
    if (authState !== "authenticated" || demoMode || bootstrapLoaded) return;
    let active = true;
    let retryTimer: number | undefined;
    let retryDelayMs = CONTROL_BOOT_RETRY_INITIAL_MS;
    const loadInitialProjection = async () => {
      try {
        // The first projection is SQLite-backed and does not need a live
        // Hermes route. Render it immediately: a sleeping laptop/gateway must
        // degrade one agent, never hold the whole PWA on its dark boot screen.
        const projection = await boundedControlRead(api.bootstrap());
        if (!active) return;
        hydrateBootstrap(projection);
        const snapshot = useAppStore.getState();
        const selectedGateway = projection.gateways.find((gateway) => gateway.id === snapshot.selectedGatewayId);
        const hermesConnected = selectedGateway
          ? selectedGateway.status === "connected"
          : projection.gateways.some((gateway) => gateway.status === "connected");
        setConnection(hermesConnected ? "connected" : "degraded");
        if (snapshot.offlineCacheEnabled) {
          void saveOfflineSnapshot(
            projection,
            snapshot.userName,
            snapshot.selectedWorkspaceId,
          ).catch(() => undefined);
        }
        retryDelayMs = CONTROL_BOOT_RETRY_INITIAL_MS;
      } catch {
        if (!active) return;
        const cached = await loadShellSnapshot().catch(() => null);
        if (!active) return;
        if (cached && cached.userName === useAppStore.getState().userName) {
          hydrateBootstrap(cached.data);
        } else {
          // A first install has no shell snapshot. Keep the visible boot state
          // recoverable and retry the SQLite-backed projection with backoff;
          // one transient Control timeout must never strand the PWA forever.
          retryTimer = window.setTimeout(() => {
            void loadInitialProjection();
          }, retryDelayMs);
          retryDelayMs = Math.min(retryDelayMs * 2, 30_000);
        }
        setConnection("degraded");
      }
    };
    void loadInitialProjection();
    return () => {
      active = false;
      window.clearTimeout(retryTimer);
    };
  }, [authState, bootstrapLoaded, csrfToken, demoMode, hydrateBootstrap, setConnection]);

  useEffect(() => {
    if (authState !== "authenticated" || demoMode || !bootstrapLoaded) return;
    let active = true;
    let refreshing = false;
    let synchronizing = false;

    const refreshProjection = async () => {
      if (!active || refreshing) return;
      refreshing = true;
      try {
        const refreshed = await api.bootstrap();
        if (!active) return;
        hydrateBootstrap(refreshed);
        const snapshot = useAppStore.getState();
        const selectedGateway = refreshed.gateways.find(
          (gateway) => gateway.id === snapshot.selectedGatewayId,
        );
        const hermesConnected = selectedGateway
          ? selectedGateway.status === "connected"
          : refreshed.gateways.some((gateway) => gateway.status === "connected");
        setConnection(hermesConnected ? "connected" : "degraded");
        if (snapshot.offlineCacheEnabled) {
          await saveOfflineSnapshot(
            refreshed,
            snapshot.userName,
            snapshot.selectedWorkspaceId,
          );
        }
      } catch {
        if (active) setConnection("degraded");
      } finally {
        refreshing = false;
      }
    };

    const synchronizeUpstream = async () => {
      if (!active || synchronizing) return;
      synchronizing = true;
      try {
        const initial = useAppStore.getState();
        await Promise.allSettled(
          initial.gateways.map((gateway) => api.refreshProfiles(gateway.id, initial.csrfToken)),
        );
        if (!active) return;
        const refreshed = await api.bootstrap();
        if (!active) return;
        await Promise.allSettled(
          refreshed.profiles.map((profile) => api.syncSessions(
            profile.gatewayId,
            profile.technicalName,
            useAppStore.getState().csrfToken,
          )),
        );
        await Promise.allSettled(
          refreshed.profiles
            .filter((profile) => profile.capabilities?.cron)
            .map((profile) => api.syncAutomations(
              profile.gatewayId,
              profile.technicalName,
              useAppStore.getState().csrfToken,
            )),
        );
        if (!active) return;
        await refreshProjection();
      } catch {
        if (active) setConnection("degraded");
      } finally {
        synchronizing = false;
      }
    };

    const refreshWhenVisible = () => {
      if (document.visibilityState !== "visible") return;
      void refreshProjection();
      const selectedSessionId = useAppStore.getState().selectedSessionId;
      if (selectedSessionId) void rehydrateSession(selectedSessionId);
    };
    void synchronizeUpstream();
    const interval = window.setInterval(() => void refreshProjection(), 30_000);
    window.addEventListener("focus", refreshWhenVisible);
    window.addEventListener("online", refreshWhenVisible);
    window.addEventListener("pageshow", refreshWhenVisible);
    window.addEventListener("agent-control:resume", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      active = false;
      window.clearInterval(interval);
      window.removeEventListener("focus", refreshWhenVisible);
      window.removeEventListener("online", refreshWhenVisible);
      window.removeEventListener("pageshow", refreshWhenVisible);
      window.removeEventListener("agent-control:resume", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [authState, bootstrapLoaded, demoMode, hydrateBootstrap, setConnection]);
}

export function useSessionHistory() {
  const authState = useAppStore((state) => state.authState);
  const demoMode = useAppStore((state) => state.demoMode);
  const bootstrapLoaded = useAppStore((state) => state.bootstrapLoaded);
  const sessionId = useAppStore((state) => state.selectedSessionId);

  useEffect(() => {
    if ((authState !== "authenticated" && authState !== "offline") || demoMode || !bootstrapLoaded || !sessionId) return;
    void rehydrateSession(sessionId);
  }, [authState, bootstrapLoaded, demoMode, sessionId]);
}

export async function createChatForCurrentContext() {
  const state = useAppStore.getState();
  const profile = state.profiles.find((item) => item.id === state.selectedProfileId);
  if (state.demoMode) {
    const session = state.sessions.find((item) => (
      item.profileId === state.selectedProfileId
      && (item.workspaceId ?? "") === state.selectedWorkspaceId
    ));
    if (session) state.selectSession(session.id);
    return session;
  }
  if (
    state.authState !== "authenticated"
    || !profile?.mutable
    || !profile.capabilities?.sessions
  ) return undefined;

  try {
    const session = await api.createSession(
      profile.id,
      state.selectedWorkspaceId || undefined,
      state.csrfToken,
    );
    useAppStore.getState().addSession(session);
    return session;
  } catch (error) {
    useAppStore.getState().setConnection("degraded");
    throw error;
  }
}

export function useThemePreference() {
  const theme = useAppStore((state) => state.theme);
  const setTheme = useAppStore((state) => state.setTheme);
  const setTimeZone = useAppStore((state) => state.setTimeZone);
  const setOfflineCacheEnabled = useAppStore((state) => state.setOfflineCacheEnabled);
  const hydrated = useRef(false);

  useEffect(() => {
    loadPreference("theme").then((value) => {
      if (value === "dark" || value === "light" || value === "auto") setTheme(value);
      hydrated.current = true;
    }).catch(() => { hydrated.current = true; });
    loadPreference("offline-cache")
      .then((value) => setOfflineCacheEnabled(value === "true"))
      .catch(() => setOfflineCacheEnabled(false));
    loadPreference(TIME_ZONE_PREFERENCE_KEY)
      .then((value) => setTimeZone(isValidTimeZone(value) ? value : detectedTimeZone()))
      .catch(() => setTimeZone(detectedTimeZone()));
  }, [setOfflineCacheEnabled, setTheme, setTimeZone]);

  useEffect(() => {
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      const resolved = theme === "auto" ? (media.matches ? "dark" : "light") : theme;
      document.documentElement.dataset.theme = resolved;
      document.documentElement.style.colorScheme = resolved;
    };
    apply();
    const unsubscribe = subscribeToMediaQuery(media, apply);
    if (hydrated.current) void savePreference("theme", theme);
    return unsubscribe;
  }, [theme]);
}

export function useOfflineTranscriptCache() {
  const authState = useAppStore((state) => state.authState);
  const enabled = useAppStore((state) => state.offlineCacheEnabled);
  const sessionId = useAppStore((state) => state.selectedSessionId);
  const workspaceId = useAppStore((state) => state.sessions.find((session) => session.id === state.selectedSessionId)?.workspaceId);
  const allMessages = useAppStore((state) => state.messages);
  const bootstrapLoaded = useAppStore((state) => state.bootstrapLoaded);
  const userName = useAppStore((state) => state.userName);
  const selectedWorkspaceId = useAppStore((state) => state.selectedWorkspaceId);
  const selectedSessionId = useAppStore((state) => state.selectedSessionId);
  const gateways = useAppStore((state) => state.gateways);
  const profiles = useAppStore((state) => state.profiles);
  const workspaces = useAppStore((state) => state.workspaces);
  const sessions = useAppStore((state) => state.sessions);
  const automations = useAppStore((state) => state.automations);
  const messages = useMemo(() => allMessages.filter((message) => message.sessionId === sessionId), [allMessages, sessionId]);

  useEffect(() => {
    if (authState !== "authenticated" || !bootstrapLoaded) return;
    const timer = window.setTimeout(() => {
      void saveShellSnapshot(
        { gateways, profiles, workspaces, sessions, automations },
        userName,
        selectedWorkspaceId,
        selectedSessionId,
      ).catch(() => undefined);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [authState, automations, bootstrapLoaded, gateways, profiles, selectedSessionId, selectedWorkspaceId, sessions, userName, workspaces]);

  useEffect(() => {
    if (authState !== "authenticated" || !enabled || !bootstrapLoaded) return;
    const timer = window.setTimeout(() => {
      void saveOfflineSnapshot(
        { gateways, profiles, workspaces, sessions, automations },
        userName,
        selectedWorkspaceId,
      ).catch(() => undefined);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [authState, automations, bootstrapLoaded, enabled, gateways, profiles, selectedWorkspaceId, sessions, userName, workspaces]);

  useEffect(() => {
    if (authState !== "authenticated" || !enabled || !sessionId || !workspaceId || !messages.length) return;
    const timer = window.setTimeout(() => {
      void saveEncryptedTranscript(sessionId, workspaceId, messages).catch(() => undefined);
    }, 500);
    return () => window.clearTimeout(timer);
  }, [authState, enabled, messages, sessionId, workspaceId]);
}

export function useRealtimeConnection() {
  const authState = useAppStore((state) => state.authState);
  const demoMode = useAppStore((state) => state.demoMode);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const setConnection = useAppStore((state) => state.setConnection);

  useEffect(() => {
    if (authState !== "authenticated" || demoMode) return;
    const controller = new AbortController();
    void connectRealtime({
      onEvent: applyRealtimeEvent,
      onState: (state) => {
        // A healthy browser-to-Control socket does not prove the selected
        // Hermes route is healthy. `control.connection` owns the connected
        // state; transport failures can only lower confidence here.
        if (state === "reconnecting") setConnection("reconnecting");
        else if (state === "offline") setConnection("degraded");
        else {
          const snapshot = useAppStore.getState();
          const selectedGateway = snapshot.gateways.find((gateway) => gateway.id === snapshot.selectedGatewayId);
          const hermesConnected = selectedGateway
            ? selectedGateway.status === "connected"
            : snapshot.gateways.some((gateway) => gateway.status === "connected");
          setConnection(hermesConnected ? "connected" : "degraded");
          // A Control API restart loses its in-memory replay buffer. History is
          // authoritative, so every newly established browser socket performs
          // one safe, read-only reconciliation for the selected session.
          if (snapshot.selectedSessionId) void rehydrateSession(snapshot.selectedSessionId);
        }
      },
    }, controller.signal, csrfToken);
    return () => controller.abort();
  }, [authState, csrfToken, demoMode, setConnection]);
}

const activeDemoControllers = new Map<string, AbortController>();

async function reconcileAmbiguousPrompt(sessionId: string, operationId: string, assistantId: string) {
  const delays = [0, 1_000, 2_000, 4_000, 8_000, 16_000];
  for (const delay of delays) {
    if (delay) await new Promise((resolve) => window.setTimeout(resolve, delay));
    const snapshot = useAppStore.getState();
    if (snapshot.pendingOperations[operationId] !== assistantId) return;
    await rehydrateSession(sessionId);
    try {
      const operation = await api.promptOperation(sessionId, operationId);
      if (!["completed", "failed", "interrupted"].includes(operation.status)) continue;
      const current = useAppStore.getState();
      current.updateMessage(assistantId, {
        streaming: false,
        ...(operation.status === "failed" ? { content: i18n.t("runtimeMessages.operationFailed") } : {}),
      });
      current.setStreamingMessageId(sessionId, undefined);
      current.clearOperation(operationId);
      await rehydrateSession(sessionId);
      return;
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) return;
    }
  }
}

export async function submitPrompt(content: string, attachments: File[] = []) {
  const state = useAppStore.getState();
  if ((!content.trim() && !attachments.length) || state.streamingBySession[state.selectedSessionId] || !state.selectedSessionId) return;
  const now = new Date();
  const userMessage: ChatMessage = {
    id: crypto.randomUUID(), sessionId: state.selectedSessionId, role: "user", content: content.trim(),
    createdAt: now.toLocaleTimeString(getCurrentLanguage(), { hour: "2-digit", minute: "2-digit" }), delivery: "sending",
    ...(attachments.length ? { attachments: attachments.map((file) => ({ kind: file.type.startsWith("image/") || /\.(?:gif|jpe?g|png|webp)$/i.test(file.name) ? "image" as const : "file" as const, name: file.name, mediaType: file.type || "application/octet-stream", size: file.size })) } : {}),
  };
  const assistantId = crypto.randomUUID();
  state.appendMessage(userMessage);
  state.appendMessage({ id: assistantId, sessionId: state.selectedSessionId, role: "assistant", content: "", createdAt: userMessage.createdAt, streaming: true });
  state.setStreamingMessageId(state.selectedSessionId, assistantId);
  void clearDraft(state.selectedSessionId).catch(() => undefined);

  if (!state.demoMode) {
    const idempotencyKey = crypto.randomUUID();
    state.bindOperation(idempotencyKey, assistantId);
    try {
      const receipt = attachments.length
        ? await api.submitPrompt(state.selectedSessionId, content.trim(), idempotencyKey, state.csrfToken, attachments)
        : await api.submitPrompt(state.selectedSessionId, content.trim(), idempotencyKey, state.csrfToken);
      if (receipt.operationId !== idempotencyKey) {
        state.bindOperation(receipt.operationId, assistantId);
        state.clearOperation(idempotencyKey);
      }
      const buffered = unmatchedEvents.get(receipt.operationId) ?? [];
      unmatchedEvents.delete(receipt.operationId);
      buffered.forEach(applyRealtimeEvent);
      state.updateMessage(userMessage.id, { delivery: "sent" });
      if (["completed", "failed", "interrupted"].includes(receipt.status)) {
        const current = useAppStore.getState().messages.find((message) => message.id === assistantId);
        state.updateMessage(assistantId, {
          streaming: false,
          ...(
            receipt.status === "failed" && !current?.content
              ? { content: i18n.t("runtimeMessages.acceptedFailed") }
              : receipt.status === "interrupted" && !current?.content
                ? { content: i18n.t("runtimeMessages.interrupted") }
                : {}
          ),
        });
        state.setStreamingMessageId(state.selectedSessionId, undefined);
        state.clearOperation(receipt.operationId);
        if (receipt.status === "completed") {
          // REST fallback and very fast Hermes turns can finish while a mobile
          // WebView is backgrounded, before it can consume the terminal WS
          // frames. Durable history is authoritative; never leave the empty
          // optimistic assistant placeholder as the final response.
          await rehydrateSession(state.selectedSessionId);
        }
      }
      return;
    } catch (error) {
      const clientError = error instanceof ApiError && error.status >= 400 && error.status < 500;
      const unknownConflict = error instanceof ApiError
        && error.status === 409
        && /unknown|desconoc|delivery/i.test(`${error.code ?? ""} ${error.message}`);
      const ambiguous = !clientError || unknownConflict;
      state.updateMessage(userMessage.id, { delivery: ambiguous ? "ambiguous" : "failed" });
      state.updateMessage(assistantId, { content: ambiguous ? i18n.t("runtimeMessages.ambiguousPrompt") : i18n.t("runtimeMessages.rejectedPrompt"), streaming: false });
      state.setStreamingMessageId(state.selectedSessionId, undefined);
      if (ambiguous) void reconcileAmbiguousPrompt(state.selectedSessionId, idempotencyKey, assistantId);
      else state.clearOperation(idempotencyKey);
      if (!(error instanceof ApiError) || error.status >= 500) state.setConnection("degraded");
      return;
    }
  }

  state.updateMessage(userMessage.id, { delivery: "sent" });
  const demoController = new AbortController();
  activeDemoControllers.set(state.selectedSessionId, demoController);
  const words = i18n.t("runtimeMessages.demoResponse").split(" ");
  let rendered = "";
  for (const word of words) {
    if (demoController.signal.aborted) break;
    rendered += `${rendered ? " " : ""}${word}`;
    useAppStore.getState().updateMessage(assistantId, { content: rendered });
    await new Promise((resolve) => window.setTimeout(resolve, 34));
  }
  useAppStore.getState().updateMessage(assistantId, { streaming: false });
  useAppStore.getState().setStreamingMessageId(state.selectedSessionId, undefined);
  activeDemoControllers.delete(state.selectedSessionId);
}

function interactionErrorMessage(error: unknown) {
  if (error instanceof ApiError && error.code === "MUTATION_DELIVERY_UNKNOWN") {
    return i18n.t("runtimeMessages.deliveryUnknown");
  }
  if (error instanceof ApiError && error.status === 409) {
    return i18n.t("runtimeMessages.noLongerPending");
  }
  if (error instanceof ApiError && error.status >= 400 && error.status < 500) {
    return error.message || i18n.t("runtimeMessages.rejectedResponse");
  }
  return i18n.t("runtimeMessages.responseFailed");
}

export async function respondToApproval(sessionId: string, requestId: string, choice: ApprovalChoice, profileOverride?: Profile) {
  const state = useAppStore.getState();
  const request = state.approvalsBySession[sessionId]?.find((item) => item.requestId === requestId);
  if (
    !request
    || request.state === "submitting"
    || request.state === "ambiguous"
    || !approvalChoices.has(choice)
    || !sessionSupportsInteraction(state, sessionId, "approvals", "approval.respond", profileOverride)
  ) return;
  state.updateApproval(sessionId, requestId, { state: "submitting", error: undefined });
  bumpInteractionRevision(sessionId);
  try {
    const receipt = await api.respondApproval(sessionId, requestId, choice, state.csrfToken);
    const terminal = receipt.resolved > 0 || ["resolved", "expired", "not_found"].includes(receipt.status);
    if (terminal) {
      bumpInteractionRevision(sessionId);
      useAppStore.getState().removeApproval(sessionId, requestId);
      return receipt;
    }
    bumpInteractionRevision(sessionId);
    useAppStore.getState().updateApproval(sessionId, requestId, {
      state: "failed",
      error: i18n.t("runtimeMessages.noLongerPending"),
    });
    return receipt;
  } catch (error) {
    bumpInteractionRevision(sessionId);
    if (error instanceof ApiError && error.status === 409 && error.code === "CONFLICT") {
      useAppStore.getState().removeApproval(sessionId, requestId);
      throw error;
    }
    const ambiguous = error instanceof ApiError && error.code === "MUTATION_DELIVERY_UNKNOWN";
    useAppStore.getState().updateApproval(sessionId, requestId, {
      state: ambiguous ? "ambiguous" : "failed",
      error: interactionErrorMessage(error),
    });
    throw error;
  }
}

export async function respondToClarification(
  sessionId: string,
  requestId: string,
  answer: string | string[],
  questionId?: string,
  profileOverride?: Profile,
) {
  const state = useAppStore.getState();
  const request = state.clarificationsBySession[sessionId]?.find((item) => item.requestId === requestId);
  if (
    !request
    || request.state === "submitting"
    || request.state === "ambiguous"
    || (typeof answer === "string" ? !answer.trim() : answer.length === 0)
    || !sessionSupportsInteraction(state, sessionId, "clarifications", "clarify.respond", profileOverride)
  ) return;
  const submittingQuestionId = questionId ?? "single";
  state.updateClarification(sessionId, requestId, {
    state: "submitting",
    submittingQuestionId,
    error: undefined,
  });
  bumpInteractionRevision(sessionId);
  try {
    const receipt = await api.respondClarification(sessionId, requestId, answer, questionId, state.csrfToken);
    const remaining = Array.isArray(receipt.remaining) ? receipt.remaining : [];
    if (receipt.status === "expired" || remaining.length === 0) {
      bumpInteractionRevision(sessionId);
      useAppStore.getState().removeClarification(sessionId, requestId);
      return receipt;
    }
    const latest = useAppStore.getState().clarificationsBySession[sessionId]?.find((item) => item.requestId === requestId);
    const storedAnswer = Array.isArray(answer) ? JSON.stringify(answer) : answer;
    bumpInteractionRevision(sessionId);
    useAppStore.getState().updateClarification(sessionId, requestId, {
      answers: { ...(latest?.answers ?? {}), ...(questionId ? { [questionId]: storedAnswer } : {}) },
      remainingQuestionIds: remaining,
      submittingQuestionId: undefined,
      state: "pending",
      error: undefined,
    });
    return receipt;
  } catch (error) {
    bumpInteractionRevision(sessionId);
    if (error instanceof ApiError && error.status === 409 && error.code === "CONFLICT") {
      useAppStore.getState().removeClarification(sessionId, requestId);
      throw error;
    }
    const ambiguous = error instanceof ApiError && error.code === "MUTATION_DELIVERY_UNKNOWN";
    useAppStore.getState().updateClarification(sessionId, requestId, {
      submittingQuestionId: ambiguous ? submittingQuestionId : undefined,
      state: ambiguous ? "ambiguous" : "failed",
      error: interactionErrorMessage(error),
    });
    throw error;
  }
}

export async function stopPrompt() {
  const state = useAppStore.getState();
  const sessionId = state.selectedSessionId;
  const streamingId = state.streamingBySession[sessionId];
  if (!streamingId) return;
  activeDemoControllers.get(sessionId)?.abort();
  if (!state.demoMode) {
    try {
      await api.interrupt(sessionId, state.csrfToken);
    } catch {
      state.setConnection("degraded");
      const current = state.messages.find((message) => message.id === streamingId)?.content ?? "";
      state.updateMessage(streamingId, { content: `${current}${current ? "\n\n" : ""}*${i18n.t("runtimeMessages.stopUnknown")}*` });
      return;
    }
  }
  state.updateMessage(streamingId, { streaming: false, content: state.messages.find((message) => message.id === streamingId)?.content || i18n.t("runtimeMessages.stopped") });
  state.setStreamingMessageId(sessionId, undefined);
  state.clearSessionInteractions(sessionId);
}

export function useSessionDraft(sessionId: string) {
  const pendingWrites = useRef(new Map<string, Promise<void>>());

  const enqueue = (targetSessionId: string, operation: () => Promise<void>) => {
    const previous = pendingWrites.current.get(targetSessionId);
    const next = previous
      ? previous.catch(() => undefined).then(operation)
      : operation();
    pendingWrites.current.set(targetSessionId, next);
    void next.finally(() => {
      if (pendingWrites.current.get(targetSessionId) === next) {
        pendingWrites.current.delete(targetSessionId);
      }
    }).catch(() => undefined);
    return next;
  };

  return {
    load: async () => {
      await pendingWrites.current.get(sessionId)?.catch(() => undefined);
      return loadDraft(sessionId).catch(() => "");
    },
    save: (value: string) => {
      // Start the IndexedDB transaction in the input event itself. A debounce
      // could be discarded by a sudden reload/tunnel loss before its timer
      // fired, leaving the offline shell without the draft it was meant to
      // reopen. Writes for the same session remain ordered.
      void enqueue(sessionId, () => saveDraft(sessionId, value)).catch(() => undefined);
    },
    clear: async () => {
      await enqueue(sessionId, () => clearDraft(sessionId)).catch(() => undefined);
    },
  };
}
