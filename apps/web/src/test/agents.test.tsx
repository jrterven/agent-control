import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { AgentsScreen } from "../screens/Screens";
import { useAppStore } from "../store/appStore";
import type { BootstrapData, Gateway, Profile, SessionSummary } from "../types";

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

const setupSession: SessionSummary = {
  id: "session-researcher-setup",
  gatewayId: gateway.id,
  profileName: createdProfile.technicalName,
  profileId: createdProfile.id,
  storedSessionId: "stored-researcher-setup",
  runtimeSessionId: "runtime-researcher-setup",
  title: "New conversation",
  preview: "",
  updatedAt: "2026-08-29T12:00:00Z",
};

function bootstrap(profiles: Profile[], sessions: SessionSummary[] = []): BootstrapData {
  return { gateways: [gateway], profiles, workspaces: [], sessions, automations: [] };
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
      messages: [],
      pendingOperations: {},
      streamingBySession: {},
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("creates a clean profile and lets Hermes analyze a long brief in a setup chat", async () => {
    const create = vi.spyOn(api, "createProfile").mockResolvedValue(createdProfile);
    const createSession = vi.spyOn(api, "createSession").mockResolvedValue(setupSession);
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap([sourceProfile, createdProfile], [setupSession]));
    const submit = vi.spyOn(api, "submitPrompt").mockResolvedValue({ operationId: "setup-operation", status: "completed" });
    const user = userEvent.setup();
    const longBrief = `Investiga fuentes técnicas y prepara reportes verificables. ${"Contexto detallado. ".repeat(300)}`.trim();

    render(<AgentsScreen />);

    const bottomCallout = screen.getByText("Crea otro agente").closest(".create-agent-callout");
    expect(bottomCallout).not.toBeNull();
    await user.click(within(bottomCallout as HTMLElement).getByRole("button", { name: "Nuevo agente" }));

    const dialog = screen.getByRole("dialog", { name: "Crear un agente" });
    await user.type(within(dialog).getByLabelText("Nombre técnico"), "Researcher_AI");
    expect(within(dialog).getByLabelText("Nombre técnico")).toHaveValue("researcher-ai");
    await user.type(within(dialog).getByLabelText("Nombre visible"), "Researcher");
    fireEvent.change(within(dialog).getByLabelText("Descripción del agente"), { target: { value: longBrief } });
    await user.click(within(dialog).getByRole("button", { name: "Crear y configurar" }));

    await waitFor(() => expect(create).toHaveBeenCalledWith({
      gatewayId: gateway.id,
      technicalName: "researcher-ai",
      displayName: "Researcher",
      description: longBrief,
    }, "csrf-memory-only"));
    expect(createSession).toHaveBeenCalledWith(createdProfile.id, undefined, "csrf-memory-only");
    await waitFor(() => expect(useAppStore.getState().selectedProfileId).toBe(createdProfile.id));
    expect(useAppStore.getState().selectedSessionId).toBe(setupSession.id);
    expect(useAppStore.getState().profiles).toContainEqual(createdProfile);
    await waitFor(() => expect(submit).toHaveBeenCalled());
    const submittedPrompt = submit.mock.calls[0]?.[1] ?? "";
    expect(submittedPrompt).toContain(longBrief);
    expect(submittedPrompt).toContain("no lo copies literalmente a SOUL.md");
    expect(submittedPrompt).not.toBe(longBrief);
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
    await user.click(screen.getByRole("button", { name: "Crear y configurar" }));

    expect(await screen.findByText(/minúsculas, números o guiones/)).toBeInTheDocument();
    expect(screen.getByText(/nombre visible de entre 2 y 80/)).toBeInTheDocument();
    expect(screen.getByText(/al menos 10/)).toBeInTheDocument();
  });

  it("retries setup after a transient session failure without creating a duplicate profile", async () => {
    const turingProfile = { ...createdProfile, technicalName: "turing", displayName: "Turing" };
    const turingSession = { ...setupSession, profileName: "turing" };
    const create = vi.spyOn(api, "createProfile").mockResolvedValue(turingProfile);
    const createSession = vi.spyOn(api, "createSession")
      .mockRejectedValueOnce(new Error("Temporary setup connection failure"))
      .mockResolvedValueOnce(turingSession);
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap([sourceProfile, turingProfile], [turingSession]));
    const submit = vi.spyOn(api, "submitPrompt").mockResolvedValue({ operationId: "setup-retry", status: "completed" });
    const user = userEvent.setup();

    render(<AgentsScreen />);
    await user.click(screen.getByRole("button", { name: "Nuevo agente" }));
    await user.type(screen.getByLabelText("Nombre técnico"), "Turing");
    await user.type(screen.getByLabelText("Nombre visible"), "Turing");
    await user.type(screen.getByLabelText("Descripción del agente"), "Diseña software y explica claramente sus decisiones.");
    await user.click(screen.getByRole("button", { name: "Crear y configurar" }));

    expect(await screen.findByText("Temporary setup connection failure")).toBeInTheDocument();
    expect(screen.getByLabelText("Nombre técnico")).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Reintentar configuración" }));

    await waitFor(() => expect(submit).toHaveBeenCalled());
    expect(create).toHaveBeenCalledTimes(1);
    expect(createSession).toHaveBeenCalledTimes(2);
    expect(create).toHaveBeenCalledWith(expect.objectContaining({ technicalName: "turing" }), "csrf-memory-only");
  });
});
