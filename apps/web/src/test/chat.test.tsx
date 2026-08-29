import axe from "axe-core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../i18n";
import { ChatView } from "../components/ChatView";
import { automations, gateways, initialMessages, profiles, sessions, workspaces } from "../data";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";

describe("mobile-first chat", () => {
  beforeEach(() => {
    useAppStore.setState({
      authState: "authenticated",
      csrfToken: "csrf-memory-only",
      demoMode: true,
      selectedProfileId: "profile-newton",
      selectedSessionId: "session-papers",
      selectedGatewayId: "gateway-home",
      selectedWorkspaceId: "workspace-papers",
      gateways,
      profiles,
      sessions,
      workspaces,
      automations,
      messages: initialMessages,
      streamingBySession: {},
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("renders the approved conversation hierarchy and expandable tools", async () => {
    const user = userEvent.setup();
    const { container } = render(<ChatView />);
    expect(screen.getByRole("heading", { name: "Memoria de agentes · agosto" })).toBeInTheDocument();
    expect(screen.getByText("Comparativa de memoria de agentes — Agosto 2026")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Herramientas · 2/i }));
    expect(screen.getByText("Búsqueda académica")).toBeVisible();
    expect((await axe.run(container)).violations).toHaveLength(0);
  });

  it("streams a demo response and offers an explicit stop action", async () => {
    const user = userEvent.setup();
    render(<ChatView />);
    const composer = screen.getByRole("textbox", { name: "Mensaje a Newton…" });
    await user.type(composer, "Resume los riesgos{Enter}");
    expect(await screen.findByRole("button", { name: /Detener/i })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Detener/i }));
    await waitFor(() => expect(useAppStore.getState().streamingBySession["session-papers"]).toBeUndefined());
  });

  it("does not borrow another session when the selected profile has no chat in the workspace", () => {
    useAppStore.setState({
      demoMode: false,
      selectedProfileId: "profile-newton",
      selectedSessionId: "",
    });

    render(<ChatView />);

    expect(screen.getByRole("heading", { name: "Newton está en modo solo lectura" })).toBeVisible();
    expect(screen.getByText("Para escribir, selecciona el entorno de pruebas.")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Memoria de agentes · agosto" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("offers a primary new-chat action in the empty conversation and uses the active workspace", async () => {
    const user = userEvent.setup();
    const writableProfile = {
      ...profiles[0],
      mutable: true,
      capabilities: { ...gateways[0].capabilities, sessions: true },
    };
    const createdSession = {
      ...sessions[0],
      id: "session-created-from-empty-state",
      profileId: writableProfile.id,
      workspaceId: "workspace-papers",
      title: "Nuevo chat",
    };
    useAppStore.setState({
      demoMode: false,
      selectedProfileId: writableProfile.id,
      selectedSessionId: "",
      selectedWorkspaceId: "workspace-papers",
      profiles: [writableProfile],
      sessions: [],
      messages: [],
    });
    const createSession = vi.spyOn(api, "createSession").mockResolvedValue(createdSession);

    render(<ChatView />);
    const action = screen.getByRole("button", { name: "Nuevo chat" });
    expect(action).toBeVisible();
    await user.click(action);

    await waitFor(() => expect(createSession).toHaveBeenCalledWith(
      writableProfile.id,
      "workspace-papers",
      "csrf-memory-only",
    ));
    expect(useAppStore.getState().selectedSessionId).toBe(createdSession.id);
  });
});
