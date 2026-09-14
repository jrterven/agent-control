import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConnectorPairing, ConnectorView } from "@hermes-control/shared-types";
import { api, ApiError } from "../lib/api";
import { googleLoginUrl, isCloudProfileOffline, useCloudConfigurationStore } from "../lib/cloud";
import { ConnectorPairingForm, ConnectorsScreen } from "../screens/CloudScreens";
import { GatewaysScreen, LoginScreen } from "../screens/Screens";
import { useAppStore } from "../store/appStore";
import { gateways, profiles } from "../data";
import { createChatForCurrentContext, submitPrompt } from "../hooks";
import i18n from "../i18n";

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children, to, ...props }: { children: ReactNode; to: string }) => <a href={to} {...props}>{children}</a>,
  useNavigate: () => vi.fn(),
}));

const pairing: ConnectorPairing = { code: "ABCD-EFGH", name: "My Mac", profiles: ["default", "research"], expiresAt: new Date(Date.now() + 600_000).toISOString() };
const connector: ConnectorView = { id: "computer-a", name: "My Mac", status: "offline", profiles: ["default"], version: "0.1.0", lastSeenAt: null };
const bootstrap = { gateways: [], profiles: [], workspaces: [], sessions: [], automations: [] };

beforeEach(async () => {
  await i18n.changeLanguage("en");
  window.history.replaceState({}, "", "/connect");
  useCloudConfigurationStore.setState({ methods: { mode: "cloud", googleEnabled: true }, loading: false, error: false });
  useAppStore.setState({ authState: "authenticated", csrfToken: "csrf-test", gateways: [], profiles: [], sessions: [], messages: [], demoMode: false, streamingBySession: {}, selectedSessionId: "", selectedProfileId: "" });
  vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap);
});

afterEach(async () => {
  vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals();
  useCloudConfigurationStore.setState({ methods: undefined, loading: false, error: false });
  window.history.replaceState({}, "", "/");
  await i18n.changeLanguage("es");
});

describe("hosted sign-in", () => {
  it("shows Google only and preserves the pairing destination", () => {
    window.history.replaceState({}, "", "/login?returnTo=%2Fconnect%3Fcode%3DABCD-EFGH&error=invite_required");
    render(<LoginScreen />);
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Continue with Google" })).toHaveAttribute("href", "/api/v1/auth/google/start?returnTo=%2Fconnect%3Fcode%3DABCD-EFGH");
    expect(screen.getByRole("alert")).toHaveTextContent("account that received your invitation");
    expect(screen.getByText(/credentials stay on your computer/)).toBeInTheDocument();
  });

  it("does not turn a return URL into an external redirect", () => {
    expect(googleLoginUrl("?returnTo=https://attacker.test/")).toBe("/api/v1/auth/google/start?returnTo=%2Fchats");
    expect(googleLoginUrl("?returnTo=//attacker.test/")).toBe("/api/v1/auth/google/start?returnTo=%2Fchats");
    expect(googleLoginUrl("?returnTo=%2Fconnect%3Fcode%3DA%26returnTo%3Dhttps%3A%2F%2Fattacker.test")).toBe("/api/v1/auth/google/start?returnTo=%2Fconnect%3Fcode%3DA");
  });

  it("fails closed when the public authentication configuration cannot load", async () => {
    useCloudConfigurationStore.setState({ methods: undefined });
    vi.spyOn(api, "authMethods").mockRejectedValue(new Error("network unavailable"));
    render(<LoginScreen />);
    expect(await screen.findByRole("alert")).toHaveTextContent("temporarily unavailable");
    expect(screen.queryByLabelText("Password")).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Continue with Google" })).not.toBeInTheDocument();
  });

  it("keeps password sign-in for private installations", () => {
    useCloudConfigurationStore.setState({ methods: { mode: "private", googleEnabled: false } });
    render(<LoginScreen />);
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Continue with Google" })).not.toBeInTheDocument();
  });
});

describe("computer pairing", () => {
  it("reviews the computer and submits only the explicitly selected profiles", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "inspectConnectorPairing").mockResolvedValue(pairing);
    vi.spyOn(api, "approveConnectorPairing").mockResolvedValue(connector);
    render(<ConnectorPairingForm initialCode="abcd-efgh" />);
    expect(api.inspectConnectorPairing).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Review computer" }));
    expect(api.inspectConnectorPairing).toHaveBeenCalledWith("ABCD-EFGH", "csrf-test");
    expect(await screen.findByRole("heading", { name: "My Mac" })).toBeInTheDocument();
    await user.click(screen.getByRole("checkbox", { name: "research" }));
    await user.click(screen.getByRole("button", { name: "Connect selected profiles" }));
    expect(api.approveConnectorPairing).toHaveBeenCalledWith("ABCD-EFGH", ["default"], "csrf-test");
    expect(await screen.findByRole("status")).toHaveTextContent("Computer linked");
  });

  it("blocks expired codes and requires at least one selected profile", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "inspectConnectorPairing").mockResolvedValue({ ...pairing, expiresAt: new Date(Date.now() - 1000).toISOString() });
    vi.spyOn(api, "approveConnectorPairing");
    render(<ConnectorPairingForm initialCode={pairing.code} />);
    await user.click(screen.getByRole("button", { name: "Review computer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("invalid or has expired");
    expect(screen.getByRole("button", { name: "Connect selected profiles" })).toBeDisabled();
    expect(api.approveConnectorPairing).not.toHaveBeenCalled();
  });

  it("disables approval after the inspected code expires on screen", async () => {
    const currentTime = Date.now();
    const clock = vi.spyOn(Date, "now").mockReturnValue(currentTime);
    vi.spyOn(api, "inspectConnectorPairing").mockResolvedValue({ ...pairing, expiresAt: new Date(currentTime + 2000).toISOString() });
    const user = userEvent.setup();
    render(<ConnectorPairingForm initialCode={pairing.code} />);
    await user.click(screen.getByRole("button", { name: "Review computer" }));
    expect(screen.getByRole("button", { name: "Connect selected profiles" })).toBeEnabled();
    clock.mockReturnValue(currentTime + 3000);
    await waitFor(() => expect(screen.getByRole("button", { name: "Connect selected profiles" })).toBeDisabled(), { timeout: 2000 });
  });

  it("clears an inspected pairing when the code changes", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "inspectConnectorPairing").mockResolvedValue(pairing);
    render(<ConnectorPairingForm initialCode={pairing.code} />);
    await user.click(screen.getByRole("button", { name: "Review computer" }));
    await user.click(screen.getByRole("checkbox", { name: "default" }));
    await user.click(screen.getByRole("checkbox", { name: "research" }));
    expect(screen.getByRole("button", { name: "Connect selected profiles" })).toBeDisabled();
    await user.type(screen.getByLabelText("Pairing code"), "X");
    expect(screen.queryByRole("button", { name: "Connect selected profiles" })).not.toBeInTheDocument();
  });

  it.each([[404, "invalid or has expired"], [409, "already been used"]])("explains pairing HTTP %s without displaying raw server details", async (status, message) => {
    const user = userEvent.setup();
    vi.spyOn(api, "inspectConnectorPairing").mockRejectedValue(new ApiError(status as number, "sensitive internal detail"));
    render(<ConnectorPairingForm initialCode={pairing.code} />);
    await user.click(screen.getByRole("button", { name: "Review computer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(message as string);
    expect(screen.queryByText("sensitive internal detail")).not.toBeInTheDocument();
  });
});

describe("my computers", () => {
  it("replaces direct gateway configuration and confirms a revocation", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "connectors").mockResolvedValue({ items: [connector], installCommand: "install connector" });
    vi.spyOn(api, "revokeConnector").mockResolvedValue();
    render(<GatewaysScreen />);
    expect(await screen.findByText("My Mac")).toBeInTheDocument();
    expect(screen.queryByText("REST dashboard")).not.toBeInTheDocument();
    expect(screen.getByText("Offline")).toBeInTheDocument();
    expect(screen.getByText("Not connected yet")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Revoke access" }));
    expect(api.revokeConnector).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Yes, revoke access" }));
    await waitFor(() => expect(api.revokeConnector).toHaveBeenCalledWith("computer-a", "csrf-test"));
  });

  it("shows the installation command and phone instructions", async () => {
    vi.spyOn(api, "connectors").mockResolvedValue({ items: [], installCommand: "curl https://control.example/connector/install.sh | sh" });
    render(<ConnectorsScreen pairing />);
    expect(await screen.findByText("curl https://control.example/connector/install.sh | sh")).toBeInTheDocument();
    expect(screen.getByText(/On iPhone, open this site in Safari/)).toBeInTheDocument();
  });

  it("prevents cloud prompts and session creation while the connector is offline", async () => {
    vi.spyOn(api, "submitPrompt"); vi.spyOn(api, "createSession");
    const profile = { ...profiles[0], status: "ready" as const, mutable: true };
    const disconnected = { ...gateways[0], id: profile.gatewayId, status: "offline" as const };
    useAppStore.setState({ profiles: [profile], gateways: [disconnected], selectedProfileId: profile.id, selectedSessionId: "existing-session" });
    expect(isCloudProfileOffline(profile, [disconnected])).toBe(true);
    await submitPrompt("Do not enqueue me");
    await createChatForCurrentContext();
    expect(api.submitPrompt).not.toHaveBeenCalled();
    expect(api.createSession).not.toHaveBeenCalled();
    expect(useAppStore.getState().messages).toHaveLength(0);
  });
});
