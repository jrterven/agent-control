import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { saveAgentPreference } from "../lib/agentPreference";
import { useAppStore } from "../store/appStore";
import type { BootstrapData } from "../types";

const data: BootstrapData = {
  gateways: ["a", "b"].map((id) => ({
    id: `gateway-${id}`, name: id, location: "", status: "connected", version: "", sha: null,
    capabilities: { realtime: true, sessions: true, prompts: true, interrupt: true, cron: true, profiles: true, config: false, memory: false },
  })),
  profiles: ["a", "b"].map((id) => ({
    id: `profile-${id}`, gatewayId: `gateway-${id}`, technicalName: "default", displayName: id,
    model: "test", status: "ready", mutable: false,
  })),
  sessions: [{ id: "session-b", storedSessionId: "stored-b", profileId: "profile-b", title: "B", preview: "", updatedAt: "now" }],
  workspaces: [], automations: [],
};

function reopen(owner = "owner-a", projection = data) {
  useAppStore.getState().resetPrivateState();
  useAppStore.getState().setAuth("authenticated", "Owner", "csrf", false, owner);
  useAppStore.getState().hydrateBootstrap(projection);
}

beforeEach(() => {
  localStorage.clear();
  reopen();
});
afterEach(() => {
  vi.restoreAllMocks();
  localStorage.clear();
  useAppStore.getState().resetPrivateState();
});

describe("last agent preference", () => {
  it("restores the selected agent and its computer after reopening, even when that computer is offline", () => {
    useAppStore.getState().selectProfile("profile-b");
    reopen("owner-a", { ...data, gateways: data.gateways.map((gateway) => ({ ...gateway, status: "offline" })) });
    expect(useAppStore.getState()).toMatchObject({ selectedGatewayId: "gateway-b", selectedProfileId: "profile-b", selectedSessionId: "session-b" });
  });

  it.each(["gateway", "session"])("remembers an agent selected through a %s", (route) => {
    if (route === "gateway") useAppStore.getState().selectGateway("gateway-b");
    else useAppStore.getState().selectSession("session-b");
    reopen();
    expect(useAppStore.getState().selectedProfileId).toBe("profile-b");
  });

  it("keeps each account's preference separate across sign-out and sign-in", () => {
    useAppStore.getState().selectProfile("profile-b");
    reopen("owner-b");
    expect(useAppStore.getState().selectedProfileId).toBe("profile-a");
    useAppStore.getState().selectProfile("profile-a");
    reopen("owner-a");
    expect(useAppStore.getState().selectedProfileId).toBe("profile-b");
  });

  it("neither restores nor overwrites a real account's preference in demo mode", () => {
    useAppStore.getState().selectProfile("profile-b");
    useAppStore.getState().resetPrivateState();
    useAppStore.getState().setAuth("authenticated", "Demo", undefined, true, "owner-a");
    useAppStore.getState().hydrateBootstrap(data);
    expect(useAppStore.getState().selectedProfileId).toBe("profile-a");
    useAppStore.getState().selectProfile("profile-a");
    reopen();
    expect(useAppStore.getState().selectedProfileId).toBe("profile-b");
  });

  it("falls back to an available agent when the saved agent or computer is no longer in the account", () => {
    useAppStore.getState().selectProfile("profile-b");
    reopen("owner-a", { ...data, gateways: [data.gateways[0]], profiles: [data.profiles[0]], sessions: [] });
    expect(useAppStore.getState()).toMatchObject({ selectedGatewayId: "gateway-a", selectedProfileId: "profile-a" });
    // A profile id alone cannot restore a different computer's agent.
    saveAgentPreference("owner-a", { gatewayId: "gateway-a", profileId: "profile-b" });
    reopen();
    expect(useAppStore.getState().selectedProfileId).toBe("profile-a");
  });

  it("preserves an in-memory update return context and does not restore storage during background refresh", () => {
    useAppStore.getState().selectProfile("profile-b");
    useAppStore.getState().resetPrivateState();
    useAppStore.getState().setAuth("authenticated", "Owner", "csrf", false, "owner-a");
    useAppStore.setState({ selectedGatewayId: "gateway-a", selectedProfileId: "profile-a" });
    useAppStore.getState().hydrateBootstrap(data);
    expect(useAppStore.getState().selectedProfileId).toBe("profile-a");
    useAppStore.setState({ selectedProfileId: "" });
    useAppStore.getState().hydrateBootstrap(data);
    expect(useAppStore.getState().selectedProfileId).toBe("profile-a");
  });

  it("selects an available agent and its computer when the first computer has no profiles", () => {
    reopen("owner-a", { ...data, profiles: [data.profiles[1]] });
    expect(useAppStore.getState()).toMatchObject({ selectedGatewayId: "gateway-b", selectedProfileId: "profile-b" });
  });

  it("remembers a selection made while using the offline shell", () => {
    useAppStore.getState().setAuth("offline", "Owner", undefined, false, "owner-a");
    useAppStore.getState().selectProfile("profile-b");
    reopen();
    expect(useAppStore.getState().selectedProfileId).toBe("profile-b");
  });

  it("ignores malformed storage and keeps navigation usable when storage is blocked", () => {
    localStorage.setItem("agent-control.last-agent:owner-a", "null");
    reopen();
    expect(useAppStore.getState().selectedProfileId).toBe("profile-a");
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("Storage blocked"); });
    expect(() => useAppStore.getState().selectProfile("profile-b")).not.toThrow();
    expect(useAppStore.getState().selectedProfileId).toBe("profile-b");
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new Error("Storage blocked"); });
    expect(() => reopen()).not.toThrow();
    expect(useAppStore.getState().selectedProfileId).toBe("profile-a");
  });
});
