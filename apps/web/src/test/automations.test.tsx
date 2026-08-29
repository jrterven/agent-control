import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { AutomationsScreen } from "../screens/Screens";
import { useAppStore } from "../store/appStore";
import type { Automation, Profile } from "../types";

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

describe("automation editor contract", () => {
  beforeEach(() => {
    useAppStore.setState({ profiles: [profile], automations: [], csrfToken: "csrf-memory-only", demoMode: false });
  });
  afterEach(() => vi.restoreAllMocks());

  it("creates a paused five-field cron on a backend-authorized profile", async () => {
    const created: Automation = {
      id: "automation-1",
      gatewayId: "gateway-a",
      profileName: "default",
      name: "Resumen semanal",
      schedule: "30 8 * * FRI",
      timezone: "America/Mexico_City",
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
    await user.type(screen.getByLabelText("Nombre"), "Resumen semanal");
    await user.type(screen.getByLabelText("Prompt"), "Prepara el resumen semanal");
    await user.click(screen.getByRole("button", { name: "Crear pausada" }));

    await waitFor(() => expect(create).toHaveBeenCalledWith({
      gatewayId: "gateway-a",
      profileName: "default",
      name: "Resumen semanal",
      schedule: "30 8 * * FRI",
      timezone: "America/Mexico_City",
      prompt: "Prepara el resumen semanal",
      enabled: false,
    }, "csrf-memory-only"));
    expect(await screen.findByText("Resumen semanal")).toBeInTheDocument();
  });
});
