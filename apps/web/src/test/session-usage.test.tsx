import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";
import { ActivityPanel } from "../components/ActivityPanel";
import { applyRealtimeEvent } from "../hooks";
import { useAppStore } from "../store/appStore";
import type { Gateway, Profile, SessionSummary } from "../types";

const gateway: Gateway = {
  id: "gateway-a",
  name: "Hermes remoto",
  location: "Tailscale",
  status: "connected",
  version: "0.20.6",
  sha: "9978706e",
  capabilities: {
    realtime: true, sessions: true, prompts: true, interrupt: true, cron: false,
    profiles: true, config: false, memory: false,
  },
};

const profile: Profile = {
  id: "profile-a",
  gatewayId: gateway.id,
  technicalName: "default",
  displayName: "Newton",
  model: "Hermes",
  status: "ready",
  mutable: false,
};

const session: SessionSummary = {
  id: "session-a",
  gatewayId: gateway.id,
  profileName: profile.technicalName,
  profileId: profile.id,
  storedSessionId: "stored-a",
  title: "Telemetría",
  preview: "",
  updatedAt: "ahora",
};

describe("session usage panel", () => {
  beforeEach(() => {
    useAppStore.getState().resetPrivateState();
    useAppStore.setState({
      authState: "authenticated",
      activityOpen: true,
      demoMode: false,
      selectedGatewayId: gateway.id,
      selectedProfileId: profile.id,
      selectedSessionId: session.id,
      gateways: [gateway],
      profiles: [profile],
      sessions: [session],
      sessionUsageById: {},
      connection: "connected",
    });
  });

  it("renders the real Hermes context and token counters without private extensions", () => {
    applyRealtimeEvent({
      type: "session.usage",
      controlSessionId: session.id,
      data: {
        usage: {
          input: 12_000,
          output: 800,
          total: 12_800,
          calls: 4,
          context_used: 53_248,
          context_max: 128_000,
          context_percent: 41.6,
          reasoning_content: "PRIVATE-CONTENT",
        },
      },
    });

    render(<ActivityPanel />);

    expect(screen.getByRole("progressbar", { name: "Uso de contexto" })).toHaveAttribute("aria-valuenow", "42");
    expect(screen.getByText("12,800")).toBeVisible();
    expect(screen.getByText("53,248")).toBeVisible();
    expect(screen.getByText("128,000")).toBeVisible();
    expect(screen.getByText("tokens acumulados")).toBeVisible();
    expect(screen.queryByText("PRIVATE-CONTENT")).not.toBeInTheDocument();
    expect(screen.queryByText(/demo/i)).not.toBeInTheDocument();
  });

  it("states honestly when Hermes has not emitted usage for the session", () => {
    render(<ActivityPanel />);

    expect(screen.queryByRole("progressbar", { name: "Uso de contexto" })).not.toBeInTheDocument();
    expect(screen.getByText("Hermes no anunció telemetría de uso para esta sesión.")).toBeVisible();
  });

  it("does not show another profile's session when no session is selected", () => {
    useAppStore.setState({ selectedSessionId: "" });

    render(<ActivityPanel />);

    expect(screen.getByText("sin sesión")).toBeVisible();
    expect(screen.queryByText("stored-a")).not.toBeInTheDocument();
  });
});
