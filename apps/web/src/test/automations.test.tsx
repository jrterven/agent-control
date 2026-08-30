import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { AutomationsScreen } from "../screens/Screens";
import { useAppStore } from "../store/appStore";
import type { Automation, AutomationRun, Profile, Workspace } from "../types";

const profile: Profile = {
  id: "profile-newton",
  gatewayId: "gateway-a",
  technicalName: "default",
  displayName: "Newton",
  model: "mock",
  status: "ready",
  mutable: true,
  capabilities: {
    realtime: true,
    sessions: true,
    prompts: true,
    interrupt: true,
    cron: true,
    cronCreate: true,
    cronUpdate: true,
    cronDelete: true,
    cronTrigger: true,
    profiles: true,
    config: false,
    memory: false,
  },
};

const workspace: Workspace = {
  id: "workspace-research",
  name: "Investigación",
  description: "",
  sessionCount: 0,
  updatedAt: "2030-01-01T00:00:00Z",
};

describe("automation editor contract", () => {
  beforeEach(() => {
    useAppStore.setState({
      profiles: [profile],
      workspaces: [workspace],
      selectedWorkspaceId: workspace.id,
      automations: [],
      csrfToken: "csrf-memory-only",
      demoMode: false,
      authState: "authenticated",
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("creates a paused five-field cron on a backend-authorized profile", async () => {
    const created: Automation = {
      id: "automation-1",
      gatewayId: "gateway-a",
      workspaceId: workspace.id,
      profileName: "default",
      name: "Resumen semanal",
      schedule: "30 8 * * FRI",
      timezone: "Hermes local",
      profileId: "",
      prompt: "Prepara el resumen semanal",
      enabled: false,
      nextRun: "2030-01-04T14:30:00Z",
      nextRuns: ["2030-01-04T14:30:00Z"],
      lastStatus: "idle",
    };
    const create = vi.spyOn(api, "createAutomation").mockResolvedValue(created);
    const user = userEvent.setup();
    render(<AutomationsScreen />);

    await user.click(screen.getByRole("button", { name: "Nueva automatización" }));
    expect(screen.getByRole("combobox", { name: /Espacio de trabajo/ })).toHaveValue(workspace.id);
    await user.type(screen.getByLabelText("Nombre"), "Resumen semanal");
    await user.type(screen.getByLabelText("Prompt"), "Prepara el resumen semanal");
    await user.click(screen.getByRole("button", { name: "Crear pausada" }));

    await waitFor(() => expect(create).toHaveBeenCalledWith({
      gatewayId: "gateway-a",
      profileName: "default",
      workspaceId: workspace.id,
      name: "Resumen semanal",
      schedule: "30 8 * * FRI",
      timezone: "Hermes local",
      prompt: "Prepara el resumen semanal",
      enabled: false,
    }, "csrf-memory-only"));
    expect(await screen.findByText("Resumen semanal")).toBeInTheDocument();
    expect(screen.getByText("Investigación")).toBeInTheDocument();
  });

  it("moves an existing automation to another workspace", async () => {
    const operations: Workspace = { ...workspace, id: "workspace-operations", name: "Operaciones" };
    const existing: Automation = {
      id: "automation-existing",
      gatewayId: "gateway-a",
      workspaceId: workspace.id,
      profileName: "default",
      name: "Radar diario",
      schedule: "0 9 * * *",
      timezone: "Hermes local",
      profileId: profile.id,
      prompt: "Prepara el radar diario",
      enabled: true,
      nextRun: "",
      nextRuns: [],
      lastStatus: "idle",
    };
    useAppStore.setState({ workspaces: [workspace, operations], automations: [existing] });
    const update = vi.spyOn(api, "updateAutomation").mockResolvedValue({ ...existing, workspaceId: operations.id });
    vi.spyOn(api, "automationRuns").mockResolvedValue([]);
    const user = userEvent.setup();
    render(<AutomationsScreen />);

    await user.click(screen.getByRole("button", { name: "Editar" }));
    await user.selectOptions(screen.getByRole("combobox", { name: /Espacio de trabajo/ }), operations.id);
    await user.click(screen.getByRole("button", { name: "Guardar cambios" }));

    await waitFor(() => expect(update).toHaveBeenCalledWith("automation-existing", {
      workspaceId: operations.id,
    }, "csrf-memory-only"));
    expect(await screen.findByText("Operaciones")).toBeInTheDocument();
  });

  it("filters automations by the persisted read state of their latest result", async () => {
    const automation = (id: string, name: string): Automation => ({
      id,
      gatewayId: "gateway-a",
      profileName: "default",
      name,
      schedule: "0 9 * * *",
      timezone: "America/Mexico_City",
      profileId: profile.id,
      prompt: "Prepare a report",
      enabled: true,
      nextRun: "2030-01-04T15:00:00Z",
      nextRuns: ["2030-01-04T15:00:00Z"],
      lastStatus: "success",
    });
    const run = (automationId: string, readAt?: string): AutomationRun => ({
      id: `run-${automationId}`,
      automationId,
      hermesRunId: `hermes-${automationId}`,
      sessionLinkId: `session-${automationId}`,
      status: "completed",
      readAt,
      createdAt: "2030-01-03T15:00:00Z",
      updatedAt: "2030-01-03T15:00:00Z",
    });
    useAppStore.setState({
      automations: [
        automation("unread", "Informe pendiente"),
        automation("read", "Informe leído"),
        automation("empty", "Sin resultados"),
      ],
    });
    vi.spyOn(api, "automationRuns").mockImplementation(async (automationId) => {
      if (automationId === "unread") return [run(automationId)];
      if (automationId === "read") return [run(automationId, "2030-01-03T16:00:00Z")];
      return [];
    });
    const user = userEvent.setup();

    const { container } = render(<AutomationsScreen />);

    expect(await screen.findByRole("img", { name: "Resultado no leído" })).toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: /No leídas/ }));
    expect(container.querySelectorAll(".automation-row")).toHaveLength(1);
    expect(container.querySelector(".automation-row")).toHaveTextContent("Informe pendiente");

    await user.click(screen.getByRole("tab", { name: /Leídas/ }));
    expect(container.querySelectorAll(".automation-row")).toHaveLength(1);
    expect(container.querySelector(".automation-row")).toHaveTextContent("Informe leído");
  });
});
