import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import "../i18n";

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => vi.fn(),
  useRouterState: ({ select }: { select: (state: { location: { pathname: string } }) => unknown }) => select({ location: { pathname: "/chats" } }),
  Link: ({ to, children, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }) => <a href={to} {...props}>{children}</a>,
}));

import { LeftSidebar } from "../components/LeftSidebar";
import { automations, gateways, profiles, sessions, workspaces } from "../data";
import { useAppStore } from "../store/appStore";

function mobileMatchMedia(query: string): MediaQueryList {
  return {
    matches: query.includes("max-width") || query.includes("prefers-color-scheme: dark"),
    media: query,
    onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  };
}

function SidebarHarness() {
  const setOpen = useAppStore((state) => state.setLeftDrawerOpen);
  return <><button type="button" onClick={() => setOpen(true)}>Abrir navegación de prueba</button><LeftSidebar /></>;
}

describe("mobile navigation focus management", () => {
  beforeEach(() => {
    vi.mocked(window.matchMedia).mockImplementation(mobileMatchMedia);
    useAppStore.setState({
      leftDrawerOpen: false, gatewayMenuOpen: false, demoMode: true,
      gateways, profiles, sessions, workspaces, automations,
      selectedGatewayId: "gateway-home", selectedProfileId: "profile-newton",
      selectedWorkspaceId: "workspace-papers", selectedSessionId: "session-papers",
    });
  });

  it("keeps a closed drawer hidden and restores its trigger after Escape", async () => {
    const user = userEvent.setup();
    const { container } = render(<SidebarHarness />);
    const trigger = screen.getByRole("button", { name: "Abrir navegación de prueba" });
    const sidebar = container.querySelector<HTMLElement>("#left-sidebar");
    expect(sidebar).toHaveAttribute("aria-hidden", "true");
    expect(sidebar).toHaveAttribute("inert");

    await user.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "Navegación de Agent Control" });
    expect(within(dialog).getByRole("button", { name: "Cerrar navegación" })).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(useAppStore.getState().leftDrawerOpen).toBe(false);
    expect(trigger).toHaveFocus();
    expect(sidebar).toHaveAttribute("aria-hidden", "true");
  });

  it("dismisses the equipment menu before closing the mobile drawer", async () => {
    const user = userEvent.setup();
    render(<SidebarHarness />);
    await user.click(screen.getByRole("button", { name: "Abrir navegación de prueba" }));
    const trigger = screen.getByRole("button", { name: /gx10-58f9 Tailscale/ });
    await user.click(trigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    expect(useAppStore.getState().leftDrawerOpen).toBe(true);
    await user.keyboard("{Escape}");
    expect(useAppStore.getState().leftDrawerOpen).toBe(false);
  });

  it("prevents WebKit from moving focus to the drawer before the equipment click", async () => {
    const user = userEvent.setup();
    useAppStore.setState({ profiles: [...profiles, { ...profiles[1], id: "profile-mock", gatewayId: "gateway-mock" }] });
    render(<SidebarHarness />);
    await user.click(screen.getByRole("button", { name: "Abrir navegación de prueba" }));
    const trigger = screen.getByRole("button", { name: /gx10-58f9 Tailscale/ });
    await user.click(trigger);
    const current = screen.getByRole("menuitemradio", { name: /gx10-58f9/ });
    const other = screen.getByRole("menuitemradio", { name: /Mock local/ });
    expect(current).toHaveFocus();

    // Safari's default mousedown moves focus to the tabindex=-1 drawer. The
    // resulting blur would remove the menu before the second button's click.
    await user.pointer({ target: other, keys: "[MouseLeft>]" });
    expect(current).toHaveFocus();
    expect(other).toBeInTheDocument();
    await user.pointer({ keys: "[/MouseLeft]" });

    expect(useAppStore.getState()).toMatchObject({ selectedGatewayId: "gateway-mock", selectedProfileId: "profile-mock" });
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("still dismisses the equipment menu when focus or a pointer moves outside its selector", async () => {
    const user = userEvent.setup();
    render(<SidebarHarness />);
    await user.click(screen.getByRole("button", { name: "Abrir navegación de prueba" }));
    const trigger = screen.getByRole("button", { name: /gx10-58f9 Tailscale/ });
    await user.click(trigger);
    await user.keyboard("{End}{Tab}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();

    await user.click(trigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();
    fireEvent.pointerDown(screen.getByText("Agent", { exact: true }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(useAppStore.getState().leftDrawerOpen).toBe(true);
  });
});
