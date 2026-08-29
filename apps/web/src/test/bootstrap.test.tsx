import { render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useBootstrapData } from "../hooks";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";
import type { BootstrapData, Gateway, Profile, SessionSummary } from "../types";

const gateway = (status: Gateway["status"]): Gateway => ({
  id: "gateway-a", name: "Gateway A", location: "Túnel privado", status, version: "0.20.5", sha: "abc1234",
  capabilities: { realtime: true, sessions: true, prompts: true, interrupt: true, cron: true, profiles: true, config: true, memory: true },
});

const profile: Profile = {
  id: "profile-default", gatewayId: "gateway-a", technicalName: "default", displayName: "Newton",
  model: "gpt-test", status: "ready", mutable: false,
};

const synchronizedSession: SessionSummary = {
  id: "control-session", storedSessionId: "stored-upstream", profileId: profile.id,
  title: "Sesión descubierta", preview: "", updatedAt: "2026-08-28T10:00:00Z",
};

function data(status: Gateway["status"], sessions: SessionSummary[] = []): BootstrapData {
  return { gateways: [gateway(status)], profiles: [profile], workspaces: [], sessions, automations: [] };
}

function BootstrapProbe() {
  useBootstrapData();
  return null;
}

describe("fresh bootstrap", () => {
  beforeEach(() => {
    useAppStore.setState({
      authState: "authenticated", demoMode: false, csrfToken: "csrf-memory-only", bootstrapLoaded: false,
      selectedGatewayId: "gateway-a", selectedProfileId: "profile-default", selectedWorkspaceId: "", selectedSessionId: "",
      connection: "offline", gateways: [], profiles: [], workspaces: [], sessions: [], automations: [],
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("refreshes profiles, syncs every discovered profile, then rehydrates sessions", async () => {
    const bootstrap = vi.spyOn(api, "bootstrap")
      .mockResolvedValueOnce(data("connected"))
      .mockResolvedValueOnce(data("connected"))
      .mockResolvedValueOnce(data("connected", [synchronizedSession]));
    const refresh = vi.spyOn(api, "refreshProfiles").mockResolvedValue([]);
    const sync = vi.spyOn(api, "syncSessions").mockResolvedValue([synchronizedSession]);

    render(<BootstrapProbe />);

    await waitFor(() => expect(useAppStore.getState().bootstrapLoaded).toBe(true));
    expect(refresh).toHaveBeenCalledWith("gateway-a", "csrf-memory-only");
    expect(sync).toHaveBeenCalledWith("gateway-a", "default", "csrf-memory-only");
    expect(useAppStore.getState().sessions).toEqual([synchronizedSession]);
    expect(refresh.mock.invocationCallOrder[0]).toBeLessThan(sync.mock.invocationCallOrder[0]);
    expect(sync.mock.invocationCallOrder[0]).toBeLessThan(bootstrap.mock.invocationCallOrder[2]);
  });

  it("derives Hermes health from the final bootstrap instead of refresh HTTP success", async () => {
    vi.spyOn(api, "bootstrap")
      .mockResolvedValueOnce(data("offline"))
      .mockResolvedValueOnce(data("offline"))
      .mockResolvedValueOnce(data("offline", [synchronizedSession]));
    vi.spyOn(api, "refreshProfiles").mockResolvedValue([]);
    vi.spyOn(api, "syncSessions").mockResolvedValue([synchronizedSession]);

    render(<BootstrapProbe />);

    await waitFor(() => expect(useAppStore.getState().bootstrapLoaded).toBe(true));
    expect(useAppStore.getState().connection).toBe("degraded");
  });
});
