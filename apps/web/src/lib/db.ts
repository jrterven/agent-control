import Dexie, { type EntityTable } from "dexie";
import type { BootstrapData, ChatMessage } from "../types";

export type DraftRecord = { sessionId: string; content: string; updatedAt: number };
export type PreferenceRecord = { key: string; value: string; updatedAt: number };
export type TranscriptRecord = { id: string; workspaceId: string; cipherText: string; bytes: number; itemCount: number; expiresAt: number; updatedAt: number };
export type OfflineSnapshotRecord = { id: "latest"; cipherText: string; bytes?: number; expiresAt: number; updatedAt: number };
export type ShellSnapshotRecord = { id: "latest"; cipherText: string; expiresAt: number; updatedAt: number };
export type DeviceKeyRecord = { id: "offline-cache" | "offline-shell"; key: CryptoKey; createdAt: number };

class ControlDatabase extends Dexie {
  drafts!: EntityTable<DraftRecord, "sessionId">;
  preferences!: EntityTable<PreferenceRecord, "key">;
  transcripts!: EntityTable<TranscriptRecord, "id">;
  offlineSnapshots!: EntityTable<OfflineSnapshotRecord, "id">;
  shellSnapshots!: EntityTable<ShellSnapshotRecord, "id">;
  deviceKeys!: EntityTable<DeviceKeyRecord, "id">;

  constructor() {
    super("hermes-control-client");
    this.version(1).stores({
      drafts: "&sessionId, updatedAt",
      preferences: "&key, updatedAt",
      transcripts: "&id, workspaceId, expiresAt, updatedAt",
    });
    this.version(2).stores({
      drafts: "&sessionId, updatedAt",
      preferences: "&key, updatedAt",
      transcripts: "&id, workspaceId, expiresAt, updatedAt",
      offlineSnapshots: "&id, expiresAt, updatedAt",
      deviceKeys: "&id, createdAt",
    });
    this.version(3).stores({
      drafts: "&sessionId, updatedAt",
      preferences: "&key, updatedAt",
      transcripts: "&id, workspaceId, expiresAt, updatedAt",
      offlineSnapshots: "&id, expiresAt, updatedAt",
      shellSnapshots: "&id, expiresAt, updatedAt",
      deviceKeys: "&id, createdAt",
    });
  }
}

export const db = new ControlDatabase();

export async function saveDraft(sessionId: string, content: string) {
  await db.drafts.put({ sessionId, content, updatedAt: Date.now() });
}

export async function loadDraft(sessionId: string) {
  return (await db.drafts.get(sessionId))?.content ?? "";
}

export async function clearDraft(sessionId: string) {
  await db.drafts.delete(sessionId);
}

/** Invalidate route-bearing offline projections without removing chat drafts or transcripts. */
export async function invalidatePrivateSnapshots() {
  await db.transaction("rw", db.offlineSnapshots, db.shellSnapshots, async () => {
    await db.offlineSnapshots.delete("latest");
    await db.shellSnapshots.delete("latest");
  });
}

/** Remove every browser-owned copy of sessions deleted by a profile lifecycle operation. */
export async function clearSessionLocalData(sessionIds: string[]) {
  const ids = [...new Set(sessionIds.filter(Boolean))];
  await db.transaction("rw", db.drafts, db.transcripts, db.offlineSnapshots, db.shellSnapshots, async () => {
    if (ids.length) {
      await db.drafts.bulkDelete(ids);
      await db.transcripts.bulkDelete(ids);
    }
    // Both snapshots embed profile/session metadata. Dropping them closes the
    // window in which an offline PWA could resurrect a just-deleted agent.
    await db.offlineSnapshots.delete("latest");
    await db.shellSnapshots.delete("latest");
  });
}

export async function savePreference(key: string, value: string) {
  await db.preferences.put({ key, value, updatedAt: Date.now() });
}

export async function loadPreference(key: string) {
  return (await db.preferences.get(key))?.value;
}

export async function clearPrivateCache() {
  await db.transaction("rw", db.drafts, db.transcripts, db.offlineSnapshots, db.shellSnapshots, db.deviceKeys, async () => {
    await db.drafts.clear();
    await db.transcripts.clear();
    await db.offlineSnapshots.clear();
    await db.shellSnapshots.clear();
    await db.deviceKeys.clear();
  });
}

export async function clearTranscriptCache() {
  await db.transaction("rw", db.transcripts, db.offlineSnapshots, db.deviceKeys, async () => {
    await db.transcripts.clear();
    await db.offlineSnapshots.clear();
    await db.deviceKeys.delete("offline-cache");
  });
}

const transcriptTtlMs = 7 * 24 * 60 * 60 * 1_000;
const offlineCacheBudgetBytes = 10 * 1024 * 1024;

function boundedText(value: string | undefined | null, maxLength = 512) {
  return typeof value === "string" ? value.slice(0, maxLength) : value;
}

function bytesToBase64(value: Uint8Array) {
  let binary = "";
  for (let offset = 0; offset < value.length; offset += 0x8000) {
    binary += String.fromCharCode(...value.subarray(offset, offset + 0x8000));
  }
  return btoa(binary);
}

function base64ToBytes(value: string) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

async function offlineCacheKey(create: boolean): Promise<CryptoKey | undefined> {
  const existing = await db.deviceKeys.get("offline-cache");
  if (existing?.key) return existing.key;
  if (!create) return undefined;
  const key = await globalThis.crypto.subtle.generateKey(
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
  await db.deviceKeys.put({ id: "offline-cache", key, createdAt: Date.now() });
  return key;
}

async function shellCacheKey(create: boolean): Promise<CryptoKey | undefined> {
  const existing = await db.deviceKeys.get("offline-shell");
  if (existing?.key) return existing.key;
  if (!create) return undefined;
  const key = await globalThis.crypto.subtle.generateKey(
    { name: "AES-GCM", length: 256 },
    false,
    ["encrypt", "decrypt"],
  );
  await db.deviceKeys.put({ id: "offline-shell", key, createdAt: Date.now() });
  return key;
}

function sanitizeTranscript(messages: ChatMessage[]): ChatMessage[] {
  return messages.slice(-200).map((message) => ({
    id: message.id,
    sessionId: message.sessionId,
    role: message.role,
    content: message.content,
    createdAt: message.createdAt,
    delivery: message.delivery,
    tools: message.tools?.map((tool) => ({
      id: tool.id,
      name: tool.name,
      label: tool.label,
      status: tool.status,
      durationMs: tool.durationMs,
      summary: tool.summary,
    })),
    streaming: false,
  }));
}

export async function saveEncryptedTranscript(
  sessionId: string,
  workspaceId: string,
  messages: ChatMessage[],
) {
  if (!sessionId || !workspaceId || !messages.length) return;
  const sanitized = sanitizeTranscript(messages);
  const plaintext = new TextEncoder().encode(JSON.stringify({ version: 1, sessionId, messages: sanitized }));
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
  const key = await offlineCacheKey(true);
  if (!key) return;
  const encrypted = new Uint8Array(await globalThis.crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: new TextEncoder().encode(`${workspaceId}\u0000${sessionId}`) },
    key,
    plaintext,
  ));
  const now = Date.now();
  await db.transaction("rw", db.transcripts, async () => {
    // The option deliberately keeps only the most recently used workspace.
    const staleWorkspaceIds = (await db.transcripts.toArray())
      .filter((record) => record.workspaceId !== workspaceId)
      .map((record) => record.id);
    await db.transcripts.bulkDelete(staleWorkspaceIds);
    await db.transcripts.put({
      id: sessionId,
      workspaceId,
      cipherText: JSON.stringify({ iv: bytesToBase64(iv), data: bytesToBase64(encrypted) }),
      bytes: encrypted.byteLength,
      itemCount: sanitized.length,
      expiresAt: now + transcriptTtlMs,
      updatedAt: now,
    });
  });
  await trimTranscriptCache();
}

export async function loadEncryptedTranscript(
  sessionId: string,
  workspaceId: string,
): Promise<ChatMessage[]> {
  const record = await db.transcripts.get(sessionId);
  if (!record || record.workspaceId !== workspaceId || record.expiresAt <= Date.now()) {
    if (record?.expiresAt && record.expiresAt <= Date.now()) await db.transcripts.delete(sessionId);
    return [];
  }
  try {
    const envelope = JSON.parse(record.cipherText) as { iv: string; data: string };
    const key = await offlineCacheKey(false);
    if (!key) return [];
    const plaintext = await globalThis.crypto.subtle.decrypt(
      {
        name: "AES-GCM",
        iv: base64ToBytes(envelope.iv),
        additionalData: new TextEncoder().encode(`${workspaceId}\u0000${sessionId}`),
      },
      key,
      base64ToBytes(envelope.data),
    );
    const payload = JSON.parse(new TextDecoder().decode(plaintext)) as { version: number; sessionId: string; messages: ChatMessage[] };
    if (payload.version !== 1 || payload.sessionId !== sessionId || !Array.isArray(payload.messages)) return [];
    return payload.messages.slice(-200);
  } catch {
    // Session rotation or tampering makes the record intentionally unreadable.
    await db.transcripts.delete(sessionId);
    return [];
  }
}

export async function saveOfflineSnapshot(
  data: BootstrapData,
  userName: string,
  selectedWorkspaceId?: string,
) {
  const workspaceId = selectedWorkspaceId || data.workspaces[0]?.id || "";
  const sessions = data.sessions
    .filter((session) => !workspaceId || session.workspaceId === workspaceId)
    .slice(0, 200)
    .map((session) => ({
      ...session,
      title: boundedText(session.title, 512) ?? "Conversación",
      preview: boundedText(session.preview, 1_024) ?? "",
    }));
  const sessionProfileIds = new Set(sessions.map((session) => session.profileId));
  const profiles = data.profiles
    .filter((profile) => sessionProfileIds.has(profile.id))
    .map((profile) => ({
      ...profile,
      displayName: boundedText(profile.displayName, 256) ?? "Hermes",
      model: boundedText(profile.model, 256) ?? "",
    }));
  const gatewayIds = new Set(profiles.map((profile) => profile.gatewayId));
  const snapshot: BootstrapData = {
    gateways: data.gateways.filter((gateway) => gatewayIds.has(gateway.id)).map((gateway) => ({
      ...gateway,
      name: boundedText(gateway.name, 256) ?? "Gateway",
      location: boundedText(gateway.location, 512) ?? "",
    })),
    profiles,
    workspaces: data.workspaces.filter((workspace) => workspace.id === workspaceId).map((workspace) => ({
      ...workspace,
      name: boundedText(workspace.name, 256) ?? "Workspace",
      description: boundedText(workspace.description, 1_024) ?? "",
    })),
    sessions,
    // Prompts are mutable command payloads and are unnecessary for the offline
    // overview. Excluding them both minimizes exposure and prevents a set of
    // 200 large prompts from bypassing the global cache budget.
    automations: data.automations.slice(0, 200).map((automation) => ({
      ...automation,
      name: boundedText(automation.name, 256) ?? "Automatización",
      schedule: boundedText(automation.schedule, 128) ?? "",
      timezone: boundedText(automation.timezone, 128) ?? "UTC",
      prompt: undefined,
      nextRun: boundedText(automation.nextRun, 256) ?? "",
      nextRuns: automation.nextRuns?.slice(0, 5).map((value) => boundedText(value, 256) ?? ""),
    })),
  };
  const key = await offlineCacheKey(true);
  if (!key) return;
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
  const plaintext = new TextEncoder().encode(JSON.stringify({ version: 1, userName, data: snapshot }));
  const encrypted = new Uint8Array(await globalThis.crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: new TextEncoder().encode("hermes-control/offline-snapshot/v1") },
    key,
    plaintext,
  ));
  if (encrypted.byteLength > offlineCacheBudgetBytes) return;
  const now = Date.now();
  await db.offlineSnapshots.put({
    id: "latest",
    cipherText: JSON.stringify({ iv: bytesToBase64(iv), data: bytesToBase64(encrypted) }),
    bytes: encrypted.byteLength,
    expiresAt: now + transcriptTtlMs,
    updatedAt: now,
  });
  await trimTranscriptCache();
}

export async function loadOfflineSnapshot(): Promise<{
  userName: string;
  data: BootstrapData;
} | null> {
  const record = await db.offlineSnapshots.get("latest");
  if (!record || record.expiresAt <= Date.now()) {
    if (record) await db.offlineSnapshots.delete("latest");
    return null;
  }
  const key = await offlineCacheKey(false);
  if (!key) return null;
  try {
    const envelope = JSON.parse(record.cipherText) as { iv: string; data: string };
    const plaintext = await globalThis.crypto.subtle.decrypt(
      {
        name: "AES-GCM",
        iv: base64ToBytes(envelope.iv),
        additionalData: new TextEncoder().encode("hermes-control/offline-snapshot/v1"),
      },
      key,
      base64ToBytes(envelope.data),
    );
    const parsed = JSON.parse(new TextDecoder().decode(plaintext)) as {
      version: number;
      userName: string;
      data: BootstrapData;
    };
    if (parsed.version !== 1 || !parsed.data || !Array.isArray(parsed.data.sessions)) return null;
    return { userName: String(parsed.userName || "Administrador"), data: parsed.data };
  } catch {
    await db.offlineSnapshots.delete("latest");
    return null;
  }
}

const disabledCapabilities = {
  realtime: false,
  sessions: false,
  prompts: false,
  interrupt: false,
  cron: false,
  cronCreate: false,
  cronUpdate: false,
  cronDelete: false,
  cronTrigger: false,
  profiles: false,
  config: false,
  memory: false,
};

/**
 * Persist just enough encrypted metadata to reopen a local draft when Control
 * is unreachable. This is independent from the optional transcript cache and
 * deliberately excludes messages, automation prompts, endpoint URLs and all
 * write capabilities.
 */
export async function saveShellSnapshot(
  data: BootstrapData,
  userName: string,
  selectedWorkspaceId?: string,
  selectedSessionId?: string,
) {
  const session = data.sessions.find((item) => item.id === selectedSessionId)
    ?? data.sessions.find((item) => !selectedWorkspaceId || item.workspaceId === selectedWorkspaceId)
    ?? data.sessions[0];
  const profile = data.profiles.find((item) => item.id === session?.profileId)
    ?? data.profiles[0];
  const gateway = data.gateways.find((item) => item.id === profile?.gatewayId)
    ?? data.gateways[0];
  const workspace = data.workspaces.find((item) => item.id === (session?.workspaceId || selectedWorkspaceId));
  if (!profile || !gateway) return;

  const shell: BootstrapData = {
    gateways: [{
      ...gateway,
      name: boundedText(gateway.name, 256) ?? "Gateway",
      location: "",
      status: "offline",
      latencyMs: undefined,
      version: "",
      sha: "",
      capabilities: disabledCapabilities,
      capabilitySet: undefined,
    }],
    profiles: [{
      ...profile,
      displayName: boundedText(profile.displayName, 256) ?? "Hermes",
      model: "",
      status: "offline",
      mutable: false,
      capabilities: disabledCapabilities,
      capabilitySet: undefined,
    }],
    workspaces: workspace ? [{
      ...workspace,
      name: boundedText(workspace.name, 256) ?? "Workspace",
      description: "",
      sessionCount: session ? 1 : 0,
    }] : [],
    sessions: session ? [{
      ...session,
      gatewayId: gateway.id,
      profileName: profile.technicalName,
      title: boundedText(session.title, 512) ?? "Conversación",
      preview: "",
      runtimeSessionId: undefined,
    }] : [],
    automations: [],
  };
  const key = await shellCacheKey(true);
  if (!key) return;
  const iv = globalThis.crypto.getRandomValues(new Uint8Array(12));
  const plaintext = new TextEncoder().encode(JSON.stringify({ version: 1, userName, data: shell }));
  const encrypted = new Uint8Array(await globalThis.crypto.subtle.encrypt(
    { name: "AES-GCM", iv, additionalData: new TextEncoder().encode("hermes-control/offline-shell/v1") },
    key,
    plaintext,
  ));
  const now = Date.now();
  await db.shellSnapshots.put({
    id: "latest",
    cipherText: JSON.stringify({ iv: bytesToBase64(iv), data: bytesToBase64(encrypted) }),
    expiresAt: now + transcriptTtlMs,
    updatedAt: now,
  });
}

export async function loadShellSnapshot(): Promise<{
  userName: string;
  data: BootstrapData;
} | null> {
  const record = await db.shellSnapshots.get("latest");
  if (!record || record.expiresAt <= Date.now()) {
    if (record) await db.shellSnapshots.delete("latest");
    return null;
  }
  const key = await shellCacheKey(false);
  if (!key) return null;
  try {
    const envelope = JSON.parse(record.cipherText) as { iv: string; data: string };
    const plaintext = await globalThis.crypto.subtle.decrypt(
      {
        name: "AES-GCM",
        iv: base64ToBytes(envelope.iv),
        additionalData: new TextEncoder().encode("hermes-control/offline-shell/v1"),
      },
      key,
      base64ToBytes(envelope.data),
    );
    const parsed = JSON.parse(new TextDecoder().decode(plaintext)) as {
      version: number;
      userName: string;
      data: BootstrapData;
    };
    if (parsed.version !== 1 || !parsed.data || !Array.isArray(parsed.data.sessions)) return null;
    return { userName: String(parsed.userName || "Administrador"), data: parsed.data };
  } catch {
    await db.shellSnapshots.delete("latest");
    return null;
  }
}

export async function trimTranscriptCache() {
  const now = Date.now();
  await db.transcripts.where("expiresAt").belowOrEqual(now).delete();
  const snapshot = await db.offlineSnapshots.get("latest");
  if (snapshot?.expiresAt && snapshot.expiresAt <= now) await db.offlineSnapshots.delete("latest");
  const currentSnapshot = snapshot?.expiresAt && snapshot.expiresAt > now ? snapshot : undefined;
  const records = await db.transcripts.orderBy("updatedAt").reverse().toArray();
  let bytes = currentSnapshot
    ? currentSnapshot.bytes ?? new TextEncoder().encode(currentSnapshot.cipherText).byteLength
    : 0;
  let items = 0;
  const removals: string[] = [];
  if (bytes > offlineCacheBudgetBytes) {
    await db.offlineSnapshots.delete("latest");
    bytes = 0;
  }
  records.forEach((record) => {
    const nextBytes = bytes + record.bytes;
    const nextItems = items + (record.itemCount ?? 1);
    if (nextItems > 200 || nextBytes > offlineCacheBudgetBytes) {
      removals.push(record.id);
      return;
    }
    bytes = nextBytes;
    items = nextItems;
  });
  await db.transcripts.bulkDelete(removals);
}
