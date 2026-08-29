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
      workspaces: [],
      sessions: [session],
      automations: [],
      selectedGatewayId: "gateway-a",
      selectedProfileId: profile.id,
      selectedWorkspaceId: "",
      selectedSessionId: session.id,
      bootstrapLoaded: true,
    });
  });

  afterEach(() => vi.restoreAllMocks());

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

  it("requires the exact persistent id before permanent deletion", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "deleteSessionFromHermes").mockResolvedValue(undefined);
    render(<LeftSidebar />);

    await user.click(screen.getByRole("button", { name: "Opciones de “Conversación”" }));
    await user.click(screen.getByRole("menuitem", { name: "Eliminar…" }));
    const dialog = screen.getByRole("dialog", { name: "Eliminar “Conversación”" });
    const input = within(dialog).getByRole("textbox", { name: /ID persistente/ });
    const submit = within(dialog).getByRole("button", { name: "Eliminar de Hermes" });
    expect(submit).toBeDisabled();

    await user.type(input, "stored-exact-42");
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
    expect(screen.getByRole("menuitem", { name: "Cambiar nombre" })).toHaveFocus();
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());
  });
});
