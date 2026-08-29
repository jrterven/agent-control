import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ActivityPanel } from "../components/ActivityPanel";
import { useAppStore } from "../store/appStore";

function mobileMatchMedia(query: string): MediaQueryList {
  return {
    matches: query.includes("max-width") || query.includes("prefers-color-scheme: dark"),
    media: query,
    onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  };
}

function ActivityHarness() {
  const setOpen = useAppStore((state) => state.setActivityOpen);
  return <><button type="button" onClick={() => setOpen(true)}>Abrir panel de prueba</button><ActivityPanel /></>;
}

describe("mobile overlay focus management", () => {
  beforeEach(() => {
    vi.mocked(window.matchMedia).mockImplementation(mobileMatchMedia);
    useAppStore.setState({ activityOpen: false, demoMode: true, connection: "connected" });
  });

  it("hides a closed drawer from assistive technology and traps/restores focus while open", async () => {
    const user = userEvent.setup();
    const { container } = render(<ActivityHarness />);
    const trigger = screen.getByRole("button", { name: "Abrir panel de prueba" });
    const panel = container.querySelector<HTMLElement>("#activity-panel");
    expect(panel).toHaveAttribute("aria-hidden", "true");
    expect(panel).toHaveAttribute("inert");

    await user.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "Actividad y contexto" });
    const closeButton = within(dialog).getByRole("button", { name: "Cerrar actividad" });
    expect(closeButton).toHaveFocus();

    await user.tab({ shift: true });
    expect(within(dialog).getByRole("switch", { name: "Modo avanzado" })).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(useAppStore.getState().activityOpen).toBe(false);
    expect(trigger).toHaveFocus();
    expect(panel).toHaveAttribute("aria-hidden", "true");
  });
});
