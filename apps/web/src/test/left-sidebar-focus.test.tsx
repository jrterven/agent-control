import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@tanstack/react-router", () => ({
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
});
