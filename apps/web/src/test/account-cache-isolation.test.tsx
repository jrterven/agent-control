import { webcrypto } from "node:crypto";
import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { acceptAuthenticatedIdentity, useAuthBootstrap, useBootstrapData } from "../hooks";
import { api } from "../lib/api";
import {
  bindPrivateCacheOwner, clearPrivateCache, db, loadDraft, loadEncryptedTranscript,
  loadShellSnapshot, privateCacheOwner, saveDraft, saveEncryptedTranscript, saveShellSnapshot,
} from "../lib/db";
import { useAppStore } from "../store/appStore";
import type { BootstrapData } from "../types";

const user = (id: string, name = "Same display name") => ({ id, name, csrfToken: `${id}-csrf` });
function projection(owner: string): BootstrapData {
  return {
    userId: owner,
    gateways: [{ id: `${owner}-gateway`, name: "Hermes", location: "", status: "offline", version: "", sha: "", capabilities: { realtime: false, sessions: false, prompts: false, interrupt: false, cron: false, profiles: false, config: false, memory: false } }],
    profiles: [{ id: `${owner}-profile`, gatewayId: `${owner}-gateway`, technicalName: "default", displayName: "Default", model: "", mutable: false, status: "offline" }],
    workspaces: [{ id: `${owner}-workspace`, name: "Private", description: "", sessionCount: 1, updatedAt: "now" }],
    sessions: [{ id: `${owner}-session`, storedSessionId: `${owner}-stored`, profileId: `${owner}-profile`, workspaceId: `${owner}-workspace`, title: `${owner}'s private session`, preview: "", updatedAt: "now" }],
    automations: [],
  };
}
const legacyProjection = () => { const { userId: _id, ...data } = projection("alice"); return data; };
function BootstrapProbe() { useBootstrapData(); return null; }
function AuthAndBootstrapProbe() { useAuthBootstrap(); useBootstrapData(); return null; }
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

async function seedAlice() {
  await bindPrivateCacheOwner("alice");
  await saveShellSnapshot(projection("alice"), user("alice").name, undefined, undefined, "alice");
  await saveDraft("alice-session", "Alice's draft", "alice");
  await saveEncryptedTranscript("alice-session", "alice-workspace", [{ id: "alice-message", sessionId: "alice-session", role: "assistant", content: "Alice's secret", createdAt: "now" }], "alice");
  useAppStore.getState().setAuth("offline", user("alice").name, undefined, false, "alice");
  useAppStore.getState().hydrateBootstrap(projection("alice"));
  useAppStore.setState({ messages: [{ id: "alice-message", sessionId: "alice-session", role: "assistant", content: "Alice's secret", createdAt: "now" }] });
}

describe("stable account ownership of browser state", () => {
  beforeEach(async () => {
    vi.stubGlobal("crypto", webcrypto);
    await db.delete();
    await db.open();
    await clearPrivateCache();
    useAppStore.getState().resetPrivateState();
    useAppStore.setState({ authState: "checking", offlineCacheEnabled: false });
    vi.spyOn(api, "authMethods").mockResolvedValue({ mode: "cloud", googleEnabled: true });
    vi.spyOn(api, "refreshProfiles").mockImplementation(() => new Promise(() => undefined));
    vi.spyOn(api, "syncSessions").mockResolvedValue([]);
  });
  afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("erases offline Alice immediately when Google verifies Bob, even with identical names", async () => {
    await seedAlice();
    const accepting = acceptAuthenticatedIdentity(user("bob"));
    expect(useAppStore.getState().messages).toEqual([]);
    expect(useAppStore.getState().sessions).toEqual([]);
    await accepting;
    expect(useAppStore.getState().userId).toBe("bob");
    expect(useAppStore.getState().authState).toBe("authenticated");
    expect(useAppStore.getState().bootstrapLoaded).toBe(false);
    expect(await privateCacheOwner()).toBe("bob");
    expect(await loadDraft("alice-session", "bob")).toBe("");
    expect(await loadEncryptedTranscript("alice-session", "alice-workspace", "bob")).toEqual([]);
    expect(await loadShellSnapshot()).toBeNull();
  });

  it("preserves the same stable account's drafts, cache key and transcript across a recovery/name change", async () => {
    await seedAlice();
    const oldKey = await db.deviceKeys.get("offline-cache");
    await acceptAuthenticatedIdentity(user("alice", "Changed display name"));
    expect(await loadDraft("alice-session", "alice")).toBe("Alice's draft");
    expect((await loadEncryptedTranscript("alice-session", "alice-workspace", "alice"))[0].content).toBe("Alice's secret");
    expect((await db.deviceKeys.get("offline-cache"))?.createdAt).toBe(oldKey?.createdAt);
    expect(api.authMethods).not.toHaveBeenCalled();
  });

  it("migrates an existing private installation's legacy draft after server-confirmed mode and matching name", async () => {
    await saveShellSnapshot(legacyProjection(), "Existing Admin");
    await saveDraft("alice-session", "Keep my pre-upgrade draft");
    vi.mocked(api.authMethods).mockResolvedValue({ mode: "private", googleEnabled: false });
    await acceptAuthenticatedIdentity(user("private-admin", "Existing Admin"));
    expect(await privateCacheOwner()).toBe("private-admin");
    expect(await loadDraft("alice-session", "private-admin")).toBe("Keep my pre-upgrade draft");
    expect((await loadShellSnapshot())?.userId).toBe("private-admin");
    expect(api.authMethods).toHaveBeenCalledTimes(1);
  });

  it.each(["cloud", "private"] as const)("does not adopt unowned legacy material in %s without the explicit private-name match", async (mode) => {
    await saveShellSnapshot(legacyProjection(), mode === "cloud" ? "Same display name" : "Other private admin");
    await saveDraft("alice-session", "Not this user's draft");
    vi.mocked(api.authMethods).mockResolvedValue({ mode, googleEnabled: mode === "cloud" });
    await acceptAuthenticatedIdentity(user("bob"));
    expect(await loadDraft("alice-session", "bob")).toBe("");
    expect(await loadShellSnapshot()).toBeNull();
  });

  it("keeps legacy private drafts intact if mode verification is unavailable and does not authenticate", async () => {
    await saveShellSnapshot(legacyProjection(), "Existing Admin");
    await saveDraft("alice-session", "Preserve until server verification returns");
    vi.mocked(api.authMethods).mockRejectedValue(new Error("unavailable"));
    await acceptAuthenticatedIdentity(user("private-admin", "Existing Admin"));
    expect(useAppStore.getState().authState).toBe("unauthenticated");
    expect(await loadDraft("alice-session")).toBe("Preserve until server verification returns");
    expect(await privateCacheOwner()).toBeUndefined();
  });

  it("does not restore Alice's same-name shell when Bob's bootstrap fails", async () => {
    await seedAlice();
    // Simulate a stale tab/store observing a new authenticated cookie before
    // its cache binding has run. Stable IDs independently protect fallback.
    useAppStore.getState().setAuth("authenticated", user("bob").name, "bob-csrf", false, "bob");
    vi.spyOn(api, "bootstrap").mockRejectedValue(new Error("unavailable"));
    render(<BootstrapProbe />);
    await waitFor(() => expect(useAppStore.getState().connection).toBe("degraded"));
    expect(useAppStore.getState().sessions).toEqual([]);
    expect(useAppStore.getState().bootstrapLoaded).toBe(false);
  });

  it("discards an Alice bootstrap which completes after Bob's identity was accepted", async () => {
    await seedAlice();
    await acceptAuthenticatedIdentity(user("alice"));
    const lateAlice = deferred<BootstrapData>();
    const bob = projection("bob");
    vi.spyOn(api, "bootstrap").mockReturnValueOnce(lateAlice.promise).mockResolvedValue(bob);
    render(<BootstrapProbe />);
    await waitFor(() => expect(api.bootstrap).toHaveBeenCalledTimes(1));
    await act(async () => { await acceptAuthenticatedIdentity(user("bob")); });
    await waitFor(() => expect(useAppStore.getState().sessions[0]?.id).toBe("bob-session"));
    await act(async () => { lateAlice.resolve(projection("alice")); });
    expect(useAppStore.getState().sessions).toEqual(bob.sessions);
    expect(await privateCacheOwner()).toBe("bob");
    expect(await loadShellSnapshot()).toBeNull();
  });

  it("rechecks identity when another tab switches the cookie before bootstrap, without rendering Bob under Alice", async () => {
    await seedAlice();
    await acceptAuthenticatedIdentity(user("alice"));
    const verifiedBob = deferred<ReturnType<typeof user>>();
    vi.spyOn(api, "me").mockReturnValue(verifiedBob.promise);
    vi.spyOn(api, "bootstrap").mockResolvedValue(projection("bob"));
    render(<AuthAndBootstrapProbe />);
    await waitFor(() => expect(api.me).toHaveBeenCalledTimes(1));
    expect(useAppStore.getState().authState).toBe("checking");
    expect(useAppStore.getState().sessions).toEqual([]);
    expect(useAppStore.getState().messages).toEqual([]);
    await act(async () => { verifiedBob.resolve(user("bob")); });
    await waitFor(() => expect(useAppStore.getState().sessions[0]?.id).toBe("bob-session"));
    expect(useAppStore.getState().userId).toBe("bob");
    expect(await loadDraft("alice-session", "bob")).toBe("");
  });

  it("rejects stale-account writes even after its pending encryption completes", async () => {
    await seedAlice();
    const encryption = deferred<ArrayBuffer>();
    const started = deferred<void>();
    const actualEncrypt = crypto.subtle.encrypt.bind(crypto.subtle);
    vi.spyOn(crypto.subtle, "encrypt").mockImplementationOnce(async (...args) => {
      const encrypted = await actualEncrypt(...args);
      started.resolve();
      await encryption.promise;
      return encrypted;
    });
    const oldSave = saveShellSnapshot(projection("alice"), user("alice").name, undefined, undefined, "alice");
    await started.promise;
    await bindPrivateCacheOwner("bob");
    encryption.resolve(new ArrayBuffer(0));
    await oldSave;
    await saveDraft("bob-session", "Bob's draft", "bob");
    await saveDraft("alice-session", "Alice stale draft", "alice");
    expect(await loadShellSnapshot()).toBeNull();
    expect(await loadDraft("bob-session", "bob")).toBe("Bob's draft");
    expect(await loadDraft("bob-session", "alice")).toBe("");
    expect(await loadDraft("alice-session", "bob")).toBe("");
  });
});
