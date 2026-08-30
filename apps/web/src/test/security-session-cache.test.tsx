import { webcrypto } from "node:crypto";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useAuthBootstrap } from "../hooks";
import { ApiError, api } from "../lib/api";
import {
  db,
  loadEncryptedTranscript,
  loadOfflineSnapshot,
  loadShellSnapshot,
  saveDraft,
  saveEncryptedTranscript,
  saveOfflineSnapshot,
  saveShellSnapshot,
  trimTranscriptCache,
} from "../lib/db";
import { SettingsScreen } from "../screens/Screens";
import { useAppStore } from "../store/appStore";
import type { ChatMessage } from "../types";

function AuthProbe() {
  useAuthBootstrap();
  return null;
}

describe("browser security state", () => {
  beforeEach(async () => {
    vi.stubGlobal("crypto", webcrypto);
    await db.delete();
    await db.open();
    useAppStore.setState({
      authState: "checking",
      demoMode: false,
      csrfToken: undefined,
      gateways: [],
      profiles: [],
      sessions: [],
      workspaces: [],
      automations: [],
      messages: [],
      bootstrapLoaded: false,
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("never turns an unavailable auth API into an authenticated demo", async () => {
    vi.spyOn(api, "me").mockRejectedValue(new ApiError(503, "unavailable"));
    render(<AuthProbe />);
    await waitFor(() => expect(useAppStore.getState().authState).toBe("unauthenticated"));
    expect(useAppStore.getState().demoMode).toBe(false);
    expect(useAppStore.getState().gateways).toEqual([]);
  });

  it("reopens the minimal encrypted shell and draft when transcript caching is disabled", async () => {
    const data = {
      gateways: [{ id: "gateway-1", name: "Hermes", location: "local", status: "connected" as const, version: "0.20.6", sha: "trusted", capabilities: { realtime: true, sessions: true, prompts: true, interrupt: true, cron: true, profiles: true, config: true, memory: false } }],
      profiles: [{ id: "profile-1", gatewayId: "gateway-1", technicalName: "control-dev", displayName: "Control Dev", model: "model", status: "ready" as const, mutable: true }],
      workspaces: [{ id: "workspace-1", name: "Trabajo", description: "private", sessionCount: 1, updatedAt: "now" }],
      sessions: [{ id: "session-1", profileId: "profile-1", workspaceId: "workspace-1", storedSessionId: "stored-1", runtimeSessionId: "runtime-1", title: "Borrador", preview: "private preview", updatedAt: "now" }],
      automations: [],
    };
    await saveShellSnapshot(data, "Admin", "workspace-1", "session-1");
    await saveDraft("session-1", "continuar cuando vuelva el túnel");
    vi.spyOn(api, "me").mockRejectedValue(new ApiError(503, "unavailable"));
    render(<AuthProbe />);

    await waitFor(() => expect(useAppStore.getState().authState).toBe("offline"));
    expect(useAppStore.getState().selectedSessionId).toBe("session-1");
    expect(useAppStore.getState().sessions[0].preview).toBe("");
    expect(useAppStore.getState().profiles[0].mutable).toBe(false);
    expect((await loadShellSnapshot())?.data.sessions[0].runtimeSessionId).toBeUndefined();
    expect((await db.drafts.get("session-1"))?.content).toBe("continuar cuando vuelva el túnel");
  });

  it("probes Control while offline and forces a fresh bootstrap after recovery", async () => {
    vi.useFakeTimers();
    useAppStore.setState({
      authState: "offline",
      bootstrapLoaded: true,
      userName: "Offline Admin",
      offlineCacheEnabled: true,
    });
    const me = vi.spyOn(api, "me").mockResolvedValue({ id: "admin", name: "Admin", csrfToken: "fresh-csrf" });
    render(<AuthProbe />);

    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });

    expect(me).toHaveBeenCalledTimes(1);
    expect(useAppStore.getState().authState).toBe("authenticated");
    expect(useAppStore.getState().bootstrapLoaded).toBe(false);
    expect(useAppStore.getState().csrfToken).toBe("fresh-csrf");
  });

  it("invalidates all private offline material after an authoritative 401", async () => {
    await saveEncryptedTranscript("session-1", "workspace-1", [{
      id: "message-1", sessionId: "session-1", role: "assistant", content: "privado", createdAt: "10:30",
    }]);
    await saveOfflineSnapshot({
      gateways: [], profiles: [], workspaces: [], sessions: [], automations: [],
    }, "Admin");
    await saveShellSnapshot({
      gateways: [{ id: "gateway", name: "Hermes", location: "", status: "offline", version: "", sha: "", capabilities: { realtime: false, sessions: false, prompts: false, interrupt: false, cron: false, profiles: false, config: false, memory: false } }],
      profiles: [{ id: "profile", gatewayId: "gateway", technicalName: "control-dev", displayName: "Control Dev", model: "", status: "offline", mutable: false }],
      workspaces: [], sessions: [], automations: [],
    }, "Admin");
    vi.spyOn(api, "me").mockRejectedValue(new ApiError(401, "expired"));
    render(<AuthProbe />);

    await waitFor(() => expect(useAppStore.getState().authState).toBe("unauthenticated"));
    await waitFor(async () => {
      expect(await db.transcripts.count()).toBe(0);
      expect(await db.offlineSnapshots.count()).toBe(0);
      expect(await db.shellSnapshots.count()).toBe(0);
      expect(await db.deviceKeys.count()).toBe(0);
    });
  });

  it("encrypts the optional transcript with a non-exportable device key", async () => {
    const messages: ChatMessage[] = [{
      id: "message-1",
      sessionId: "session-1",
      role: "assistant",
      content: "contenido extremadamente privado",
      createdAt: "10:30",
    }];
    await saveEncryptedTranscript("session-1", "workspace-1", messages);
    const record = await db.transcripts.get("session-1");
    expect(record?.cipherText).not.toContain(messages[0].content);
    expect(await loadEncryptedTranscript("session-1", "workspace-1")).toEqual(messages.map((message) => ({ ...message, streaming: false })));
    const keyRecord = await db.deviceKeys.get("offline-cache");
    expect(keyRecord?.key.extractable).toBe(false);
  });

  it("keeps the live agent activity trace ephemeral", async () => {
    const message: ChatMessage = {
      id: "message-activity",
      sessionId: "session-activity",
      role: "assistant",
      content: "respuesta pública",
      createdAt: "10:31",
      activity: [{
        id: "activity-1",
        kind: "tool",
        label: "Búsqueda",
        summary: "detalle transitorio que no debe persistir",
        status: "running",
      }],
      streaming: true,
    };

    await saveEncryptedTranscript("session-activity", "workspace-1", [message]);
    const record = await db.transcripts.get("session-activity");
    expect(record?.cipherText).not.toContain("detalle transitorio");
    expect(await loadEncryptedTranscript("session-activity", "workspace-1")).toEqual([{
      id: message.id,
      sessionId: message.sessionId,
      role: message.role,
      content: message.content,
      createdAt: message.createdAt,
      streaming: false,
    }]);
  });

  it("omits automation prompts and enforces one 10 MB budget across the offline cache", async () => {
    await saveOfflineSnapshot({
      gateways: [], profiles: [], workspaces: [], sessions: [],
      automations: [{
        id: "automation-1", name: "Private", schedule: "0 9 * * *", timezone: "UTC",
        profileId: "profile-1", prompt: "TOP SECRET ".repeat(20_000), enabled: false,
        nextRun: "Tomorrow", lastStatus: "idle",
      }],
    }, "Admin");
    expect((await loadOfflineSnapshot())?.data.automations[0]?.prompt).toBeUndefined();
    const snapshot = await db.offlineSnapshots.get("latest");
    expect(snapshot?.bytes).toBeLessThan(100_000);

    await db.transcripts.put({
      id: "too-large", workspaceId: "workspace-1", cipherText: "encrypted",
      bytes: 10 * 1024 * 1024, itemCount: 1, expiresAt: Date.now() + 60_000, updatedAt: Date.now(),
    });
    await trimTranscriptCache();
    expect(await db.transcripts.get("too-large")).toBeUndefined();
  });

  it("logs out through the API and clears in-memory private state", async () => {
    useAppStore.setState({
      authState: "authenticated",
      csrfToken: "csrf-memory-only",
      userName: "Admin",
      gateways: [{ id: "gateway", name: "Private", location: "Túnel", status: "connected", version: "1", sha: "abc", capabilities: { realtime: true, sessions: true, prompts: true, interrupt: true, cron: false, profiles: true, config: false, memory: false } }],
    });
    const logout = vi.spyOn(api, "logout").mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<SettingsScreen />);
    await user.click(screen.getByRole("button", { name: "Cerrar sesión en este dispositivo" }));
    await waitFor(() => expect(useAppStore.getState().authState).toBe("unauthenticated"));
    expect(logout).toHaveBeenCalledWith("csrf-memory-only");
    expect(useAppStore.getState().csrfToken).toBeUndefined();
    expect(useAppStore.getState().gateways).toEqual([]);
  });
});
