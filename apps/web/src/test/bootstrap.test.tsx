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

  it("renders the stored projection without waiting for a sleeping gateway", async () => {
    vi.spyOn(api, "bootstrap").mockResolvedValue(data("offline", [synchronizedSession]));
    const refresh = vi.spyOn(api, "refreshProfiles").mockImplementation(
      () => new Promise(() => undefined),
    );
    const sync = vi.spyOn(api, "syncSessions").mockResolvedValue([]);

    render(<BootstrapProbe />);

    await waitFor(() => expect(useAppStore.getState().bootstrapLoaded).toBe(true));
    expect(useAppStore.getState().sessions).toEqual([synchronizedSession]);
    expect(useAppStore.getState().connection).toBe("degraded");
    await waitFor(() => expect(refresh).toHaveBeenCalledWith("gateway-a", "csrf-memory-only"));
    expect(sync).not.toHaveBeenCalled();
  });

  it("retries the first projection when a new installation has no cached shell", async () => {
    useAppStore.setState({ userName: "retry-user-without-cache" });
    const bootstrap = vi.spyOn(api, "bootstrap")
      .mockRejectedValueOnce(new Error("Control temporarily unavailable"))
      .mockResolvedValue(data("connected", [synchronizedSession]));
    vi.spyOn(api, "refreshProfiles").mockImplementation(
      () => new Promise(() => undefined),
    );

    render(<BootstrapProbe />);

    await waitFor(() => expect(bootstrap).toHaveBeenCalledTimes(2), { timeout: 2_500 });
    await waitFor(() => expect(useAppStore.getState().bootstrapLoaded).toBe(true));
    expect(useAppStore.getState().sessions).toEqual([synchronizedSession]);
  });

  it("refreshes profiles, syncs every discovered profile, then rehydrates sessions", async () => {
    const bootstrap = vi.spyOn(api, "bootstrap")
      .mockResolvedValueOnce(data("connected"))
      .mockResolvedValueOnce(data("connected"))
      .mockResolvedValueOnce(data("connected", [synchronizedSession]));
    const refresh = vi.spyOn(api, "refreshProfiles").mockResolvedValue([]);
    const sync = vi.spyOn(api, "syncSessions").mockResolvedValue([synchronizedSession]);

    render(<BootstrapProbe />);

    await waitFor(() => expect(useAppStore.getState().sessions).toEqual([synchronizedSession]));
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

  it("renews the capability projection when the mobile app returns to the foreground", async () => {
    useAppStore.setState({
      bootstrapLoaded: true,
      gateways: [gateway("connected")],
      profiles: [profile],
    });
    const refreshedProfile: Profile = {
      ...profile,
      mutable: true,
      capabilities: {
        realtime: true, sessions: true, prompts: true, interrupt: true,
        cron: false, profiles: true, config: false, memory: false,
      },
    };
    const bootstrap = vi.spyOn(api, "bootstrap").mockResolvedValue({
      ...data("connected"),
      profiles: [refreshedProfile],
    });
    vi.spyOn(api, "refreshProfiles").mockImplementation(() => new Promise(() => undefined));

    render(<BootstrapProbe />);
    window.dispatchEvent(new Event("focus"));

    await waitFor(() => expect(bootstrap).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(useAppStore.getState().profiles[0].mutable).toBe(true));
    expect(useAppStore.getState().connection).toBe("connected");
  });

  it("reconciles the selected transcript whenever the PWA resumes", async () => {
    useAppStore.setState({
      bootstrapLoaded: true,
      gateways: [gateway("connected")],
      profiles: [profile],
      sessions: [synchronizedSession],
      selectedSessionId: synchronizedSession.id,
      messages: [],
    });
    vi.spyOn(api, "refreshProfiles").mockImplementation(() => new Promise(() => undefined));
    vi.spyOn(api, "bootstrap").mockResolvedValue(data("connected", [synchronizedSession]));
    const history = vi.spyOn(api, "sessionHistory").mockResolvedValue({
      items: [
        { id: "resume-user", role: "user", content: "Termina esto" },
        { id: "resume-answer", role: "assistant", content: "Listo desde Hermes" },
      ],
      sessionStatus: "ready",
      activeOperation: null,
    });

    render(<BootstrapProbe />);
    window.dispatchEvent(new Event("agent-control:resume"));

    await waitFor(() => expect(history).toHaveBeenCalledWith(synchronizedSession.id));
    await waitFor(() => expect(useAppStore.getState().messages).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: "resume-answer", content: "Listo desde Hermes" }),
    ])));
  });
});
