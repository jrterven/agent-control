import axe from "axe-core";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ActivityPanel } from "../components/ActivityPanel";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";
import type { BootstrapData, Profile, SessionSummary } from "../types";

const capabilityFlags = {
  realtime: true,
  sessions: true,
  prompts: true,
  interrupt: true,
  cron: false,
  profiles: true,
  config: false,
  memory: false,
};

function stateForSession(mutable: boolean, methods: string[]) {
  const profile: Profile = {
    id: "profile-control-dev",
    gatewayId: "gateway-a",
    technicalName: "control-dev",
    displayName: "Control Dev",
    model: "hermes-test",
    status: "ready",
    mutable,
    capabilities: capabilityFlags,
    capabilitySet: {
      protocol: "dashboard-rest",
      version: "0.20.6",
      sourceSha: "9978706e",
      methods,
      features: ["sessions"],
    },
  };
  const session: SessionSummary = {
    id: "session-a",
    gatewayId: "gateway-a",
    profileName: "control-dev",
    profileId: profile.id,
    workspaceId: "workspace-a",
    storedSessionId: "stored-exact-42",
    runtimeSessionId: "runtime-a",
    title: "Sesión mutable",
    preview: "",
    updatedAt: "ahora",
  };
  const emptyBootstrap: BootstrapData = {
    gateways: [{
      id: "gateway-a", name: "Hermes", location: "Túnel", status: "connected",
      version: "0.20.6", sha: "9978706e", capabilities: capabilityFlags,
    }],
    profiles: [profile],
    workspaces: [{ id: "workspace-a", name: "Trabajo", description: "", sessionCount: 0, updatedAt: "ahora" }],
    sessions: [],
    automations: [],
  };
  return { profile, session, emptyBootstrap };
}

describe("session actions", () => {
  beforeEach(() => {
    useAppStore.getState().resetPrivateState();
  });

  afterEach(() => vi.restoreAllMocks());

  function prepare(mutable: boolean, methods: string[]) {
    const fixture = stateForSession(mutable, methods);
    useAppStore.setState({
      authState: "authenticated",
      csrfToken: "csrf-memory-only",
      demoMode: false,
      activityOpen: true,
      selectedGatewayId: "gateway-a",
      selectedProfileId: fixture.profile.id,
      selectedWorkspaceId: "workspace-a",
      selectedSessionId: fixture.session.id,
      gateways: fixture.emptyBootstrap.gateways,
      profiles: [fixture.profile],
      workspaces: [{ ...fixture.emptyBootstrap.workspaces[0], sessionCount: 1 }],
      sessions: [fixture.session],
      messages: [{ id: "message-a", sessionId: fixture.session.id, role: "assistant", content: "Privado", createdAt: "ahora" }],
      streamingBySession: { [fixture.session.id]: "message-a" },
      pendingOperations: { "operation-a": "message-a" },
      bootstrapLoaded: true,
    });
    vi.spyOn(api, "bootstrap").mockResolvedValue(fixture.emptyBootstrap);
    return fixture;
  }

  it("always offers local archiving but hides Hermes deletion without both exact capability and mutability", () => {
    prepare(false, ["session.delete"]);
    const { rerender } = render(<ActivityPanel />);

    expect(screen.getByRole("button", { name: "Archivar" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Eliminar…" })).not.toBeInTheDocument();

    prepare(true, ["session.history"]);
    rerender(<ActivityPanel />);
    expect(screen.getByRole("button", { name: "Archivar" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Eliminar…" })).not.toBeInTheDocument();
  });

  it("archives only in Control and refreshes the in-memory/bootstrap session list", async () => {
    const fixture = prepare(false, []);
    const archive = vi.spyOn(api, "archiveSession").mockResolvedValue({ ...fixture.session, archived: true });
    const user = userEvent.setup();
    render(<ActivityPanel />);

    await user.click(screen.getByRole("button", { name: "Archivar" }));

    await waitFor(() => expect(archive).toHaveBeenCalledWith("session-a", "csrf-memory-only"));
    expect(api.bootstrap).toHaveBeenCalledOnce();
    expect(useAppStore.getState().sessions).toEqual([]);
    expect(useAppStore.getState().selectedSessionId).toBe("");
    expect(useAppStore.getState().messages).toEqual([]);
    expect(screen.getByRole("status")).toHaveTextContent("se archivó solo en Agent Control");
  });

  it("requires the exact stored id before deleting from Hermes", async () => {
    const fixture = prepare(true, ["session.delete"]);
    const remove = vi.spyOn(api, "deleteSessionFromHermes").mockResolvedValue(undefined);
    const user = userEvent.setup();
    const { container } = render(<ActivityPanel />);

    await user.click(screen.getByRole("button", { name: "Eliminar…" }));
    const dialog = screen.getByRole("dialog", { name: "Eliminar “Sesión mutable”" });
    const confirmation = within(dialog).getByRole("textbox", { name: /ID persistente/ });
    const submit = within(dialog).getByRole("button", { name: "Eliminar de Hermes" });
    expect(submit).toBeDisabled();
    expect((await axe.run(container)).violations).toHaveLength(0);

    await user.type(confirmation, "stored-incorrecto");
    expect(confirmation).toHaveAttribute("aria-invalid", "true");
    expect(submit).toBeDisabled();
    await user.clear(confirmation);
    await user.type(confirmation, fixture.session.storedSessionId);
    expect(submit).toBeEnabled();
    await user.click(submit);

    await waitFor(() => expect(remove).toHaveBeenCalledWith("session-a", "stored-exact-42", "csrf-memory-only"));
    expect(api.bootstrap).toHaveBeenCalledOnce();
    expect(screen.queryByRole("dialog", { name: "Eliminar “Sesión mutable”" })).not.toBeInTheDocument();
    expect(useAppStore.getState().sessions).toEqual([]);
    expect(screen.getByRole("status")).toHaveTextContent("se eliminó de Hermes y de Agent Control");
  });
});
