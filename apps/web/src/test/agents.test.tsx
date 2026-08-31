import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { AgentsScreen } from "../screens/Screens";
import { useAppStore } from "../store/appStore";
import type { BootstrapData, Gateway, Profile, SessionSummary } from "../types";

const navigate = vi.hoisted(() => vi.fn());
const prepareAvatar = vi.hoisted(() => vi.fn());

vi.mock("@tanstack/react-router", async (importOriginal) => ({
  ...await importOriginal<typeof import("@tanstack/react-router")>(),
  useNavigate: () => navigate,
}));

vi.mock("../lib/profileAvatar", () => ({ prepareProfileAvatar: prepareAvatar }));

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

const secondaryGateway: Gateway = {
  ...gateway,
  id: "gateway-b",
  name: "Gateway B",
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

const secondarySourceProfile: Profile = {
  ...sourceProfile,
  id: "profile-primary-b",
  gatewayId: secondaryGateway.id,
  technicalName: "primary",
  displayName: "Turing",
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

const cleanSession: SessionSummary = {
  ...setupSession,
  id: "session-researcher-clean",
  storedSessionId: "stored-researcher-clean",
  runtimeSessionId: "runtime-researcher-clean",
};

function bootstrap(
  profiles: Profile[],
  sessions: SessionSummary[] = [],
  gateways: Gateway[] = [gateway],
): BootstrapData {
  return { gateways, profiles, workspaces: [], sessions, automations: [] };
}

describe("new agent flow", () => {
  beforeEach(() => {
    navigate.mockReset();
    prepareAvatar.mockReset().mockResolvedValue(new Blob([new Uint8Array([0xff, 0xd8, 0xff])], { type: "image/jpeg" }));
    Object.defineProperty(URL, "createObjectURL", { configurable: true, value: vi.fn(() => "blob:avatar-preview") });
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
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

  afterEach(() => {
    vi.restoreAllMocks();
    Reflect.deleteProperty(URL, "createObjectURL");
    Reflect.deleteProperty(URL, "revokeObjectURL");
  });

  it("shows setup progress, archives the internal session, and opens a clean chat with the new agent active", async () => {
    const create = vi.spyOn(api, "createProfile").mockResolvedValue(createdProfile);
    const createSession = vi.spyOn(api, "createSession")
      .mockResolvedValueOnce(setupSession)
      .mockResolvedValueOnce(cleanSession);
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap([sourceProfile, createdProfile]));
    const archive = vi.spyOn(api, "archiveSession").mockResolvedValue(setupSession);
    let finishSetup!: (value: { operationId: string; status: string }) => void;
    const submit = vi.spyOn(api, "submitPrompt").mockImplementation(() => new Promise((resolve) => {
      finishSetup = resolve;
    }));
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
    await waitFor(() => expect(submit).toHaveBeenCalled());
    expect(screen.getByRole("dialog", { name: "Creando a Researcher" })).toBeInTheDocument();
    expect(screen.getByText("Hermes sigue trabajando. Esto puede tardar unos minutos.")).toBeInTheDocument();
    expect(createSession).toHaveBeenNthCalledWith(1, createdProfile.id, undefined, "csrf-memory-only");
    const submittedPrompt = submit.mock.calls[0]?.[1] ?? "";
    expect(submittedPrompt).toContain(longBrief);
    expect(submittedPrompt).toContain("no lo copies literalmente a SOUL.md");
    expect(submittedPrompt).not.toBe(longBrief);

    finishSetup({ operationId: "setup-operation", status: "completed" });

    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/chats" }));
    expect(archive).toHaveBeenCalledWith(setupSession.id, "csrf-memory-only");
    expect(createSession).toHaveBeenNthCalledWith(2, createdProfile.id, undefined, "csrf-memory-only");
    expect(useAppStore.getState().selectedProfileId).toBe(createdProfile.id);
    expect(useAppStore.getState().selectedSessionId).toBe(cleanSession.id);
    expect(useAppStore.getState().profiles).toContainEqual(createdProfile);
    expect(useAppStore.getState().sessions).toContainEqual(cleanSession);
    expect(useAppStore.getState().sessions).not.toContainEqual(setupSession);
    expect(useAppStore.getState().messages.filter((message) => message.sessionId === cleanSession.id)).toHaveLength(0);
    expect(navigate).toHaveBeenCalledWith({ to: "/chats" });
    expect(screen.queryByRole("dialog", { name: "Crear un agente" })).not.toBeInTheDocument();
  });

  it("creates and configures an agent on the explicitly selected gateway", async () => {
    const gatewayAExistingProfile: Profile = {
      ...sourceProfile,
      id: "profile-ada-a",
      technicalName: "ada",
      displayName: "Ada on Gateway A",
    };
    const gatewayBCreatedProfile: Profile = {
      ...secondarySourceProfile,
      id: "profile-ada-b",
      technicalName: "ada",
      displayName: "Ada",
    };
    const gatewayBSetupSession: SessionSummary = {
      ...setupSession,
      id: "session-ada-setup",
      gatewayId: secondaryGateway.id,
      profileId: gatewayBCreatedProfile.id,
      profileName: gatewayBCreatedProfile.technicalName,
      storedSessionId: "stored-ada-setup",
      runtimeSessionId: "runtime-ada-setup",
    };
    const gatewayBCleanSession: SessionSummary = {
      ...gatewayBSetupSession,
      id: "session-ada-clean",
      storedSessionId: "stored-ada-clean",
      runtimeSessionId: "runtime-ada-clean",
    };
    useAppStore.setState({
      gateways: [gateway, secondaryGateway],
      profiles: [sourceProfile, gatewayAExistingProfile, secondarySourceProfile],
    });
    const create = vi.spyOn(api, "createProfile").mockResolvedValue(gatewayBCreatedProfile);
    const createSession = vi.spyOn(api, "createSession")
      .mockResolvedValueOnce(gatewayBSetupSession)
      .mockResolvedValueOnce(gatewayBCleanSession);
    vi.spyOn(api, "submitPrompt").mockResolvedValue({ operationId: "setup-ada", status: "completed" });
    vi.spyOn(api, "archiveSession").mockResolvedValue(gatewayBSetupSession);
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap(
      [sourceProfile, gatewayAExistingProfile, secondarySourceProfile, gatewayBCreatedProfile],
      [],
      [gateway, secondaryGateway],
    ));
    const user = userEvent.setup();

    render(<AgentsScreen />);
    await user.click(screen.getByRole("button", { name: "Nuevo agente" }));
    const dialog = screen.getByRole("dialog", { name: "Crear un agente" });
    const gatewaySelect = within(dialog).getByLabelText("Gateway");
    expect(gatewaySelect).toHaveValue(gateway.id);
    expect(within(gatewaySelect).getAllByRole("option")).toHaveLength(2);
    await user.selectOptions(gatewaySelect, secondaryGateway.id);
    await user.type(within(dialog).getByLabelText("Nombre técnico"), "ada");
    await user.type(within(dialog).getByLabelText("Nombre visible"), "Ada");
    await user.type(within(dialog).getByLabelText("Descripción del agente"), "Diseña sistemas confiables y documenta sus decisiones.");
    await user.click(within(dialog).getByRole("button", { name: "Crear y configurar" }));

    await waitFor(() => expect(create).toHaveBeenCalledWith({
      gatewayId: secondaryGateway.id,
      technicalName: "ada",
      displayName: "Ada",
      description: "Diseña sistemas confiables y documenta sus decisiones.",
    }, "csrf-memory-only"));
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/chats" }));
    expect(createSession).toHaveBeenNthCalledWith(1, gatewayBCreatedProfile.id, undefined, "csrf-memory-only");
    expect(createSession).toHaveBeenNthCalledWith(2, gatewayBCreatedProfile.id, undefined, "csrf-memory-only");
    expect(useAppStore.getState().selectedGatewayId).toBe(secondaryGateway.id);
    expect(useAppStore.getState().selectedProfileId).toBe(gatewayBCreatedProfile.id);
    expect(useAppStore.getState().selectedSessionId).toBe(gatewayBCleanSession.id);
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

  it("assigns an image to an existing agent and refreshes every avatar surface", async () => {
    const avatarUrl = "/api/v1/profiles/profile-default/avatar?v=updated";
    const upload = vi.spyOn(api, "setProfileAvatar").mockResolvedValue({ avatarUrl });
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap([{ ...sourceProfile, avatarUrl }]));
    const user = userEvent.setup();

    render(<AgentsScreen />);
    await user.click(screen.getByRole("button", { name: "Imagen" }));
    const dialog = screen.getByRole("dialog", { name: "Imagen de Newton" });
    const file = new File(["photo"], "newton.png", { type: "image/png" });
    await user.upload(within(dialog).getByLabelText("Elegir imagen"), file);
    await waitFor(() => expect(prepareAvatar).toHaveBeenCalledWith(file));
    await user.click(within(dialog).getByRole("button", { name: "Guardar imagen" }));

    await waitFor(() => expect(upload).toHaveBeenCalledWith(sourceProfile.id, expect.any(Blob), "csrf-memory-only"));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Imagen de Newton" })).not.toBeInTheDocument());
    expect(useAppStore.getState().profiles[0].avatarUrl).toBe(avatarUrl);
  });

  it("retries setup after a transient session failure without creating a duplicate profile", async () => {
    const turingProfile = { ...createdProfile, technicalName: "turing", displayName: "Turing" };
    const turingSession = { ...setupSession, profileName: "turing" };
    const turingCleanSession = { ...cleanSession, profileName: "turing" };
    const create = vi.spyOn(api, "createProfile").mockResolvedValue(turingProfile);
    const createSession = vi.spyOn(api, "createSession")
      .mockRejectedValueOnce(new Error("Temporary setup connection failure"))
      .mockResolvedValueOnce(turingSession)
      .mockResolvedValueOnce(turingCleanSession);
    vi.spyOn(api, "archiveSession").mockResolvedValue(turingSession);
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap([sourceProfile, turingProfile]));
    const submit = vi.spyOn(api, "submitPrompt").mockResolvedValue({ operationId: "setup-retry", status: "completed" });
    const user = userEvent.setup();

    render(<AgentsScreen />);
    await user.click(screen.getByRole("button", { name: "Nuevo agente" }));
    await user.type(screen.getByLabelText("Nombre técnico"), "Turing");
    await user.type(screen.getByLabelText("Nombre visible"), "Turing");
    await user.type(screen.getByLabelText("Descripción del agente"), "Diseña software y explica claramente sus decisiones.");
    await user.click(screen.getByRole("button", { name: "Crear y configurar" }));

    expect(await screen.findByText("Temporary setup connection failure")).toBeInTheDocument();
    expect(screen.queryByLabelText("Nombre técnico")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Reintentar desde aquí" }));

    await waitFor(() => expect(submit).toHaveBeenCalled());
    expect(create).toHaveBeenCalledTimes(1);
    expect(createSession).toHaveBeenCalledTimes(3);
    expect(create).toHaveBeenCalledWith(expect.objectContaining({ technicalName: "turing" }), "csrf-memory-only");
  });

  it("checks the same ambiguous setup operation instead of resending the brief", async () => {
    vi.spyOn(api, "createProfile").mockResolvedValue(createdProfile);
    vi.spyOn(api, "createSession")
      .mockResolvedValueOnce(setupSession)
      .mockResolvedValueOnce(cleanSession);
    const submit = vi.spyOn(api, "submitPrompt").mockRejectedValueOnce(new Error("Temporary network loss"));
    const operation = vi.spyOn(api, "promptOperation").mockResolvedValue({ operationId: "preserved-operation", status: "completed" });
    vi.spyOn(api, "archiveSession").mockResolvedValue(setupSession);
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap([sourceProfile, createdProfile]));
    const user = userEvent.setup();

    render(<AgentsScreen />);
    await user.click(screen.getByRole("button", { name: "Nuevo agente" }));
    await user.type(screen.getByLabelText("Nombre técnico"), "researcher-ai");
    await user.type(screen.getByLabelText("Nombre visible"), "Researcher");
    await user.type(screen.getByLabelText("Descripción del agente"), "Investiga y entrega resultados con fuentes verificables.");
    await user.click(screen.getByRole("button", { name: "Crear y configurar" }));

    expect(await screen.findByText("Temporary network loss")).toBeInTheDocument();
    const preservedIdempotencyKey = submit.mock.calls[0]?.[2];
    expect(preservedIdempotencyKey).toEqual(expect.any(String));
    await user.click(screen.getByRole("button", { name: "Reintentar desde aquí" }));

    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/chats" }));
    expect(submit).toHaveBeenCalledTimes(1);
    expect(operation).toHaveBeenCalledWith(setupSession.id, preservedIdempotencyKey);
    expect(useAppStore.getState().selectedSessionId).toBe(cleanSession.id);
  });
});
