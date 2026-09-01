import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tanstack/react-router", () => ({
  useRouterState: ({ select }: { select: (state: { location: { pathname: string } }) => unknown }) => select({ location: { pathname: "/chats" } }),
  Link: ({ to, children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }) => <a href={to} {...props}>{children}</a>,
}));

import { LeftSidebar } from "../components/LeftSidebar";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";
import type { Profile, SessionSummary } from "../types";

const capabilityFlags = { realtime: true, sessions: true, prompts: true, interrupt: true, cron: true, profiles: true, config: true, memory: true };

const profile: Profile = {
  id: "profile-jarvis",
  gatewayId: "gateway-a",
  technicalName: "jarvis",
  displayName: "Jarvis",
  model: "model",
  status: "ready",
  mutable: true,
  capabilities: capabilityFlags,
  capabilitySet: { protocol: "dashboard-rest", methods: ["session.delete"], features: [], version: "0.20.6" },
};

const session: SessionSummary = {
  id: "session-a",
  gatewayId: "gateway-a",
  profileName: "jarvis",
  profileId: profile.id,
  storedSessionId: "stored-exact-42",
  title: "Conversación",
  preview: "Vista previa",
  updatedAt: "ahora",
};

const workspace = {
  id: "workspace-papers",
  name: "Papers",
  description: "Investigación",
  sessionCount: 0,
  updatedAt: "ahora",
};

describe("sidebar session menu", () => {
  beforeEach(() => {
    useAppStore.getState().resetPrivateState();
    useAppStore.setState({
      authState: "authenticated",
      csrfToken: "csrf-memory-only",
      demoMode: false,
      leftDrawerOpen: true,
      gateways: [{ id: "gateway-a", name: "Hermes", location: "Privado", status: "connected", version: "0.20.6", sha: "test-sha", capabilities: capabilityFlags }],
      profiles: [profile],
      workspaces: [workspace],
      sessions: [session],
      automations: [],
      selectedGatewayId: "gateway-a",
      selectedProfileId: profile.id,
      selectedWorkspaceId: "",
      selectedSessionId: session.id,
      timeZone: "America/Mexico_City",
      bootstrapLoaded: true,
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("shows Hermes UTC history timestamps in the selected zone with minute precision", () => {
    useAppStore.setState({ sessions: [{ ...session, updatedAt: "2026-09-01T01:14:23.872776" }] });
    render(<LeftSidebar />);

    expect(screen.getByText("Lun 31/08/2026, 19:14")).toBeInTheDocument();
    expect(screen.queryByText(/2026-09-01T01:14:23/)).not.toBeInTheDocument();
  });

  it("pins and unpins a chat in a separate list without duplicating it", async () => {
    const user = userEvent.setup();
    const setPinned = vi.spyOn(api, "setSessionPinned")
      .mockResolvedValueOnce({ ...session, pinnedAt: "2026-08-31T16:00:00Z", updatedAt: "después" })
      .mockResolvedValueOnce({ ...session, pinnedAt: undefined, updatedAt: "después" });
    render(<LeftSidebar />);

    expect(screen.queryByText("Fijados")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Opciones de “Conversación”" }));
    await user.click(screen.getByRole("menuitem", { name: "Fijar chat" }));

    await waitFor(() => expect(setPinned).toHaveBeenNthCalledWith(1, "session-a", true, "csrf-memory-only"));
    expect(useAppStore.getState().sessions[0].pinnedAt).toBe("2026-08-31T16:00:00Z");
    const pinnedHeading = screen.getByText("Fijados");
    const workspacesHeading = screen.getByText("Espacios de trabajo");
    const conversationsHeading = screen.getByText("Conversaciones");
    expect(workspacesHeading.compareDocumentPosition(pinnedHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(pinnedHeading.compareDocumentPosition(conversationsHeading) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(screen.getAllByText("Conversación")).toHaveLength(1);
    expect(screen.getByText("Jarvis · Sin espacio de trabajo")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("se fijó en la lista");

    await user.click(screen.getByRole("button", { name: "Opciones de “Conversación”" }));
    await user.click(screen.getByRole("menuitem", { name: "Desfijar chat" }));

    await waitFor(() => expect(setPinned).toHaveBeenNthCalledWith(2, "session-a", false, "csrf-memory-only"));
    expect(useAppStore.getState().sessions[0].pinnedAt).toBeUndefined();
    expect(screen.queryByText("Fijados")).not.toBeInTheDocument();
    expect(screen.getAllByText("Conversación")).toHaveLength(1);
    expect(screen.getByRole("status")).toHaveTextContent("se quitó de Fijados");
  });

  it("collapses workspaces without hiding the pinned list", async () => {
    const user = userEvent.setup();
    useAppStore.setState({ sessions: [{ ...session, pinnedAt: "2026-08-31T16:00:00Z" }] });
    render(<LeftSidebar />);

    const collapse = screen.getByRole("button", { name: "Colapsar espacios de trabajo" });
    expect(collapse).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Papers/ })).toBeInTheDocument();
    expect(screen.getByText("Fijados")).toBeInTheDocument();

    await user.click(collapse);

    const expand = screen.getByRole("button", { name: "Expandir espacios de trabajo" });
    expect(expand).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /Papers/ })).not.toBeInTheDocument();
    expect(screen.getByText("Fijados")).toBeInTheDocument();

    await user.click(expand);
    expect(screen.getByRole("button", { name: /Papers/ })).toBeInTheDocument();
  });

  it("scrolls workspaces, pinned chats, and conversations as one sidebar region", () => {
    useAppStore.setState({ sessions: [{ ...session, pinnedAt: "2026-08-31T16:00:00Z" }] });
    const { container } = render(<LeftSidebar />);

    const scrollRegion = container.querySelector<HTMLElement>(".sidebar-scroll");
    const footer = container.querySelector<HTMLElement>(".sidebar-footer");
    const profileStrip = container.querySelector<HTMLElement>(".profile-strip");
    expect(scrollRegion).not.toBeNull();
    expect(within(scrollRegion as HTMLElement).getByText("Espacios de trabajo")).toBeInTheDocument();
    expect(within(scrollRegion as HTMLElement).getByText("Fijados")).toBeInTheDocument();
    expect(within(scrollRegion as HTMLElement).getByText("Conversaciones")).toBeInTheDocument();
    expect(scrollRegion).not.toContainElement(footer);
    expect(scrollRegion).not.toContainElement(profileStrip);
  });

  it("shows pinned chats from another agent and workspace and restores their context", async () => {
    const user = userEvent.setup();
    const otherProfile: Profile = {
      ...profile,
      id: "profile-newton",
      technicalName: "newton",
      displayName: "Newton",
    };
    const operations = {
      ...workspace,
      id: "workspace-operations",
      name: "Operaciones",
    };
    const pinnedElsewhere: SessionSummary = {
      ...session,
      id: "session-pinned-elsewhere",
      profileId: otherProfile.id,
      profileName: "newton",
      workspaceId: operations.id,
      title: "Plan importante",
      pinnedAt: "2026-08-31T17:00:00Z",
    };
    useAppStore.setState({
      profiles: [profile, otherProfile],
      workspaces: [workspace, operations],
      sessions: [session, pinnedElsewhere],
    });
    render(<LeftSidebar />);

    expect(screen.getByText("Newton · Operaciones")).toBeInTheDocument();
    const pinnedList = screen.getByRole("list", { name: "Fijados" });
    await user.click(within(pinnedList).getByRole("button", { name: /^Plan importante/ }));

    expect(useAppStore.getState().selectedSessionId).toBe(pinnedElsewhere.id);
    expect(useAppStore.getState().selectedProfileId).toBe(otherProfile.id);
    expect(useAppStore.getState().selectedWorkspaceId).toBe(operations.id);
  });

  it("renames the Control label without selecting another chat", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "renameSession").mockResolvedValue({ ...session, title: "Proyecto Turing", updatedAt: "después" });
    render(<LeftSidebar />);

    await user.click(screen.getByRole("button", { name: "Opciones de “Conversación”" }));
    const menu = screen.getByRole("menu", { name: "Opciones de “Conversación”" });
    await user.click(within(menu).getByRole("menuitem", { name: "Cambiar nombre" }));
    const dialog = screen.getByRole("dialog", { name: "Cambiar nombre del chat" });
    const input = within(dialog).getByRole("textbox", { name: "Nombre del chat" });
    await user.clear(input);
    await user.type(input, "Proyecto Turing");
    await user.click(within(dialog).getByRole("button", { name: "Cambiar nombre" }));

    await waitFor(() => expect(api.renameSession).toHaveBeenCalledWith("session-a", "Proyecto Turing", "csrf-memory-only"));
    expect(useAppStore.getState().sessions[0].title).toBe("Proyecto Turing");
    expect(useAppStore.getState().selectedSessionId).toBe("session-a");
    expect(screen.getByRole("status")).toHaveTextContent("ahora se llama “Proyecto Turing”");
  });

  it("moves a chat to a workspace and keeps the active chat selected", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "moveSession").mockResolvedValue({ ...session, workspaceId: workspace.id, updatedAt: "después" });
    render(<LeftSidebar />);

    await user.click(screen.getByRole("button", { name: "Opciones de “Conversación”" }));
    await user.click(screen.getByRole("menuitem", { name: "Mover a espacio de trabajo…" }));
    const dialog = screen.getByRole("dialog", { name: "Mover chat" });
    await user.selectOptions(within(dialog).getByRole("combobox", { name: "Espacio de trabajo" }), workspace.id);
    await user.click(within(dialog).getByRole("button", { name: "Mover" }));

    await waitFor(() => expect(api.moveSession).toHaveBeenCalledWith("session-a", workspace.id, "csrf-memory-only"));
    expect(useAppStore.getState().sessions[0].workspaceId).toBe(workspace.id);
    expect(useAppStore.getState().selectedWorkspaceId).toBe(workspace.id);
    expect(useAppStore.getState().selectedSessionId).toBe("session-a");
    expect(useAppStore.getState().workspaces[0].sessionCount).toBe(1);
    expect(screen.getByRole("status")).toHaveTextContent("se movió a Papers");
  });

  it("requires a clear irreversible warning before permanent deletion", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "deleteSessionFromHermes").mockResolvedValue(undefined);
    render(<LeftSidebar />);

    await user.click(screen.getByRole("button", { name: "Opciones de “Conversación”" }));
    await user.click(screen.getByRole("menuitem", { name: "Eliminar…" }));
    const dialog = screen.getByRole("dialog", { name: "Eliminar “Conversación”" });
    const submit = within(dialog).getByRole("button", { name: "Eliminar de Hermes" });
    expect(within(dialog).queryByRole("textbox")).not.toBeInTheDocument();
    expect(dialog).toHaveTextContent("Esta acción no se puede deshacer");
    expect(submit).toBeEnabled();
    await user.click(submit);

    await waitFor(() => expect(api.deleteSessionFromHermes).toHaveBeenCalledWith("session-a", "stored-exact-42", "csrf-memory-only"));
    expect(useAppStore.getState().sessions).toEqual([]);
    expect(useAppStore.getState().selectedSessionId).toBe("");
  });

  it("closes the menu with Escape and restores focus to the three-dot button", async () => {
    const user = userEvent.setup();
    render(<LeftSidebar />);
    const trigger = screen.getByRole("button", { name: "Opciones de “Conversación”" });

    await user.click(trigger);
    expect(screen.getByRole("menuitem", { name: "Fijar chat" })).toHaveFocus();
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});
