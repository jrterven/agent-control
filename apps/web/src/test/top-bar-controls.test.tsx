import axe from "axe-core";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { AnchorHTMLAttributes, ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tanstack/react-router", () => ({
  useRouterState: ({ select }: { select: (state: { location: { pathname: string } }) => unknown }) => select({ location: { pathname: "/chats" } }),
  useNavigate: () => vi.fn(),
  Link: ({ to, children, ...props }: AnchorHTMLAttributes<HTMLAnchorElement> & { to: string; children?: ReactNode }) => <a href={to} {...props}>{children}</a>,
}));

import { AppShell } from "../components/AppShell";
import { TopBar } from "../components/TopBar";
import { resetPwaUpdateStateForTests, usePwaUpdateStore } from "../lib/pwaUpdate";
import { useAppStore } from "../store/appStore";

function matchMediaFor(width: "desktop" | "tablet") {
  return (query: string): MediaQueryList => ({
    matches: query.includes("prefers-color-scheme: dark")
      || (width === "desktop" ? query.includes("min-width: 1200px") : query.includes("max-width: 1199px")),
    media: query,
    onchange: null,
    addListener: vi.fn(),
    removeListener: vi.fn(),
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
    dispatchEvent: vi.fn(),
  });
}

const workspaces = [
  { id: "workspace-agent-control", name: "Agent Control", description: "Producto y despliegues", sessionCount: 1, updatedAt: "2026-08-31T10:00:00Z" },
  { id: "workspace-personal", name: "Personal", description: "Pendientes privados", sessionCount: 1, updatedAt: "2026-08-30T10:00:00Z" },
];

const capabilities = {
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
  config: true,
  memory: true,
};

const sessions = [
  { id: "session-unassigned", storedSessionId: "stored-unassigned", profileId: "profile-jarvis", title: "Sin carpeta", preview: "", updatedAt: "2026-08-31T10:00:00Z" },
  { id: "session-agent-control", storedSessionId: "stored-agent-control", profileId: "profile-jarvis", workspaceId: "workspace-agent-control", title: "Agent Control", preview: "", updatedAt: "2026-08-31T09:00:00Z" },
  { id: "session-personal", storedSessionId: "stored-personal", profileId: "profile-jarvis", workspaceId: "workspace-personal", title: "Personal", preview: "", updatedAt: "2026-08-30T09:00:00Z" },
];

describe("desktop top bar controls", () => {
  beforeEach(() => {
    vi.mocked(window.matchMedia).mockImplementation(matchMediaFor("desktop"));
    resetPwaUpdateStateForTests();
    useAppStore.getState().resetPrivateState();
    useAppStore.setState({
      authState: "authenticated",
      bootstrapLoaded: true,
      connection: "connected",
      desktopContextOpen: true,
      activityOpen: false,
      selectedGatewayId: "gateway-1",
      selectedProfileId: "profile-jarvis",
      selectedWorkspaceId: "",
      selectedSessionId: "session-unassigned",
      gateways: [{ id: "gateway-1", name: "Hermes remoto", location: "Tailscale", status: "connected", version: "test", sha: "test", capabilities }],
      profiles: [{ id: "profile-jarvis", gatewayId: "gateway-1", technicalName: "jarvis", displayName: "Jarvis", model: "gpt-test", status: "ready", mutable: true }],
      workspaces,
      sessions,
      automations: [],
      messages: [],
    });
  });

  it("lists workspaces and moves the active context between them", async () => {
    const user = userEvent.setup();
    render(<TopBar />);
    const trigger = screen.getByRole("button", { name: "Elegir espacio de trabajo: Sin espacio de trabajo" });

    await user.click(trigger);
    const menu = screen.getByRole("menu", { name: "Espacios de trabajo" });
    expect(trigger).toHaveAttribute("aria-expanded", "true");
    expect(within(menu).getAllByRole("menuitemradio")).toHaveLength(3);
    expect(within(menu).getByRole("menuitemradio", { name: /Sin espacio de trabajo/ })).toHaveAttribute("aria-checked", "true");
    expect((await axe.run(menu)).violations).toEqual([]);

    await user.click(within(menu).getByRole("menuitemradio", { name: /Agent Control/ }));
    expect(useAppStore.getState().selectedWorkspaceId).toBe("workspace-agent-control");
    expect(useAppStore.getState().selectedSessionId).toBe("session-agent-control");
    expect(trigger).toHaveTextContent("Agent Control");
    expect(trigger).toHaveAccessibleName("Elegir espacio de trabajo: Agent Control");
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    await user.click(trigger);
    await user.click(screen.getByRole("menuitemradio", { name: /Sin espacio de trabajo/ }));
    expect(useAppStore.getState().selectedWorkspaceId).toBe("");
    expect(useAppStore.getState().selectedSessionId).toBe("session-unassigned");
  });

  it("closes the workspace menu with Escape or an outside click", async () => {
    const user = userEvent.setup();
    render(<><TopBar /><button type="button">Fuera</button></>);
    const trigger = screen.getByRole("button", { name: "Elegir espacio de trabajo: Sin espacio de trabajo" });

    trigger.focus();
    await user.keyboard("{ArrowUp}");
    const keyboardMenu = screen.getByRole("menu", { name: "Espacios de trabajo" });
    await waitFor(() => expect(within(keyboardMenu).getAllByRole("menuitemradio").at(-1)).toHaveFocus());
    expect(within(keyboardMenu).getAllByRole("menuitemradio").every((item) => item.tabIndex === -1)).toBe(true);
    await user.tab();
    expect(screen.queryByRole("menu", { name: "Espacios de trabajo" })).not.toBeInTheDocument();

    await user.click(trigger);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu", { name: "Espacios de trabajo" })).not.toBeInTheDocument();
    await waitFor(() => expect(trigger).toHaveFocus());

    await user.click(trigger);
    fireEvent.pointerDown(screen.getByRole("button", { name: "Fuera" }));
    expect(screen.queryByRole("menu", { name: "Espacios de trabajo" })).not.toBeInTheDocument();
  });

  it("collapses and restores the docked context panel", async () => {
    const user = userEvent.setup();
    const { container } = render(<AppShell><div>Contenido del chat</div></AppShell>);
    const shell = container.querySelector(".app-shell");
    const panel = container.querySelector("#activity-panel");

    expect(shell).not.toHaveClass("is-context-collapsed");
    expect(panel).not.toHaveAttribute("inert");
    await user.click(screen.getByRole("button", { name: "Ocultar contexto" }));
    expect(useAppStore.getState().desktopContextOpen).toBe(false);
    expect(useAppStore.getState().activityOpen).toBe(false);
    expect(shell).toHaveClass("is-context-collapsed");
    expect(panel).toHaveAttribute("aria-hidden", "true");
    expect(panel).toHaveAttribute("inert");

    await user.click(screen.getByRole("button", { name: "Mostrar contexto" }));
    expect(useAppStore.getState().desktopContextOpen).toBe(true);
    expect(shell).not.toHaveClass("is-context-collapsed");
    expect(panel).not.toHaveAttribute("aria-hidden");
  });

  it("keeps the tablet overlay state separate from the docked desktop preference", async () => {
    vi.mocked(window.matchMedia).mockImplementation(matchMediaFor("tablet"));
    const user = userEvent.setup();
    render(<TopBar />);
    await user.click(screen.getByRole("button", { name: "Mostrar contexto" }));
    expect(useAppStore.getState().activityOpen).toBe(true);
    expect(useAppStore.getState().desktopContextOpen).toBe(true);
  });

  it("announces an available update without hiding the context action", () => {
    usePwaUpdateStore.setState({ status: "available" });
    render(<TopBar />);
    expect(screen.getByRole("button", { name: "Ocultar contexto · Hay una actualización de Agent Control" })).toBeInTheDocument();
  });
});
