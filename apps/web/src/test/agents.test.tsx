import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { AgentsScreen } from "../screens/Screens";
import { useAppStore } from "../store/appStore";
import type { BootstrapData, Gateway, Profile } from "../types";

const navigate = vi.hoisted(() => vi.fn());

vi.mock("@tanstack/react-router", async (importOriginal) => ({
  ...await importOriginal<typeof import("@tanstack/react-router")>(),
  useNavigate: () => navigate,
}));

const gateway: Gateway = {
  id: "gateway-a",
  name: "Gateway A",
  location: "Private tunnel",
  status: "connected",
  version: "0.20.5",
  sha: "abc1234",
  capabilities: {
    realtime: true,
    sessions: true,
    prompts: true,
    interrupt: true,
    cron: true,
    profiles: true,
    config: true,
    memory: true,
  },
};

const sourceProfile: Profile = {
  id: "profile-default",
  gatewayId: gateway.id,
  technicalName: "default",
  displayName: "Newton",
  model: "gpt-test",
  status: "ready",
  mutable: true,
  capabilities: {
    ...gateway.capabilities,
    config: false,
    profileCreate: true,
  },
};

const createdProfile: Profile = {
  ...sourceProfile,
  id: "profile-researcher",
  technicalName: "researcher-ai",
  displayName: "Researcher",
};

function bootstrap(profiles: Profile[]): BootstrapData {
  return { gateways: [gateway], profiles, workspaces: [], sessions: [], automations: [] };
}

describe("new agent flow", () => {
  beforeEach(() => {
    navigate.mockReset();
    useAppStore.setState({
      authState: "authenticated",
      demoMode: false,
      csrfToken: "csrf-memory-only",
      bootstrapLoaded: true,
      selectedGatewayId: gateway.id,
      selectedProfileId: sourceProfile.id,
      selectedWorkspaceId: "",
      selectedSessionId: "",
      gateways: [gateway],
      profiles: [sourceProfile],
      workspaces: [],
      sessions: [],
      automations: [],
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("creates a fresh profile, rehydrates it, selects it, and opens chats", async () => {
    const create = vi.spyOn(api, "createProfile").mockResolvedValue(createdProfile);
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap([sourceProfile, createdProfile]));
    const user = userEvent.setup();

    render(<AgentsScreen />);

    const bottomCallout = screen.getByText("Crea otro agente").closest(".create-agent-callout");
    expect(bottomCallout).not.toBeNull();
    await user.click(within(bottomCallout as HTMLElement).getByRole("button", { name: "Nuevo agente" }));

    const dialog = screen.getByRole("dialog", { name: "Crear un agente" });
    await user.type(within(dialog).getByLabelText("Nombre técnico"), "researcher-ai");
    await user.type(within(dialog).getByLabelText("Nombre visible"), "Researcher");
    await user.type(within(dialog).getByLabelText("Descripción del agente"), "Investiga fuentes técnicas y prepara reportes verificables.");
    await user.click(within(dialog).getByRole("button", { name: "Crear y usar" }));

    await waitFor(() => expect(create).toHaveBeenCalledWith({
      gatewayId: gateway.id,
      technicalName: "researcher-ai",
      displayName: "Researcher",
      description: "Investiga fuentes técnicas y prepara reportes verificables.",
    }, "csrf-memory-only"));
    await waitFor(() => expect(useAppStore.getState().selectedProfileId).toBe(createdProfile.id));
    expect(useAppStore.getState().profiles).toContainEqual(createdProfile);
    expect(navigate).toHaveBeenCalledWith({ to: "/chats" });
    expect(screen.queryByRole("dialog", { name: "Crear un agente" })).not.toBeInTheDocument();
  });

  it("validates identifiers and keeps creation hidden behind the verified capability", async () => {
    useAppStore.setState({ profiles: [{ ...sourceProfile, capabilities: { ...sourceProfile.capabilities!, profileCreate: false } }] });
    const { rerender } = render(<AgentsScreen />);
    expect(screen.getByRole("button", { name: "Nuevo agente" })).toBeDisabled();

    useAppStore.setState({ profiles: [sourceProfile] });
    rerender(<AgentsScreen />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Nuevo agente" }));
    await user.type(screen.getByLabelText("Nombre técnico"), "Nombre Inválido");
    await user.type(screen.getByLabelText("Nombre visible"), "A");
    await user.type(screen.getByLabelText("Descripción del agente"), "corta");
    await user.click(screen.getByRole("button", { name: "Crear y usar" }));

    expect(await screen.findByText(/minúsculas, números o guiones/)).toBeInTheDocument();
    expect(screen.getByText(/nombre visible de entre 2 y 80/)).toBeInTheDocument();
    expect(screen.getByText(/entre 10 y 4000/)).toBeInTheDocument();
  });
});
