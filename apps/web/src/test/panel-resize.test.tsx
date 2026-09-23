import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PanelResizeHandle, useDesktopPanelResize } from "../components/DesktopPanelResize";
import { fitPanelWidths, PANEL_WIDTHS_KEY, readPanelWidths } from "../lib/panelWidths";

function Panels({ left = true, right = true }: { left?: boolean; right?: boolean }) {
  const panels = useDesktopPanelResize(left, right);
  return <div ref={panels.shellRef} style={panels.style} data-testid="shell" data-resizing={panels.resizing}>
    <aside id="left-sidebar" /><main /><aside id="activity-panel" />
    <PanelResizeHandle {...panels.handle("left")} /><PanelResizeHandle {...panels.handle("right")} />
  </div>;
}
const leftHandle = () => screen.getByRole("separator", { name: "Ajustar ancho de la barra lateral" });
const rightHandle = () => screen.getByRole("separator", { name: "Ajustar ancho del panel de contexto" });

describe("desktop panel resizing", () => {
  let measure: () => void;
  beforeEach(() => {
    localStorage.removeItem(PANEL_WIDTHS_KEY);
    vi.spyOn(window, "innerWidth", "get").mockReturnValue(1440);
    vi.mocked(window.matchMedia).mockImplementation((query) => ({ matches: true, media: query, addEventListener: vi.fn(), removeEventListener: vi.fn() }) as unknown as MediaQueryList);
    vi.stubGlobal("ResizeObserver", class { constructor(callback: () => void) { measure = callback; } observe() {} disconnect() {} });
    vi.stubGlobal("PointerEvent", class extends MouseEvent {
      pointerId: number; isPrimary: boolean;
      constructor(type: string, init: PointerEventInit = {}) { super(type, init); this.pointerId = init.pointerId ?? 1; this.isPrimary = init.isPrimary ?? true; }
    });
    Object.defineProperty(HTMLElement.prototype, "setPointerCapture", { configurable: true, value: vi.fn() });
  });
  afterEach(() => { Reflect.deleteProperty(HTMLElement.prototype, "setPointerCapture"); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

  it("limits both panels and reserves space for the chat as a window narrows", () => {
    for (let viewport = 1200; viewport <= 2000; viewport += 10) {
      for (const open of [{ left: true, right: true }, { left: true, right: false }, { left: false, right: true }]) {
        const widths = fitPanelWidths({ left: 440, right: 480 }, viewport, open);
        expect(viewport - widths.left - widths.right).toBeGreaterThanOrEqual(480);
        expect(widths.left).toBeGreaterThanOrEqual(open.left ? 240 : 0);
        expect(widths.right).toBeGreaterThanOrEqual(open.right ? 280 : 0);
        expect(widths.left).toBeLessThanOrEqual(open.left ? 440 : 0);
        expect(widths.right).toBeLessThanOrEqual(open.right ? 480 : 0);
      }
    }
  });

  it("uses safe defaults for invalid storage and works when persistence is unavailable", () => {
    localStorage.setItem(PANEL_WIDTHS_KEY, "not json");
    expect(readPanelWidths()).toEqual({ left: 282, right: 320 });
    localStorage.setItem(PANEL_WIDTHS_KEY, JSON.stringify({ left: "500", right: 5000 }));
    expect(readPanelWidths()).toEqual({ left: 282, right: 480 });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new Error("unavailable"); });
    render(<Panels />);
    fireEvent.keyDown(leftHandle(), { key: "ArrowRight" });
    expect(leftHandle()).toHaveAttribute("aria-valuenow", "292");
  });

  it("supports directional keyboard resizing, limits, reset and reopening", () => {
    const { unmount } = render(<Panels />);
    fireEvent.keyDown(leftHandle(), { key: "ArrowRight", shiftKey: true });
    fireEvent.keyDown(rightHandle(), { key: "ArrowLeft" });
    expect(leftHandle()).toHaveAttribute("aria-valuenow", "322");
    expect(rightHandle()).toHaveAttribute("aria-valuenow", "330");
    fireEvent.keyDown(leftHandle(), { key: "End" });
    fireEvent.keyDown(rightHandle(), { key: "Home" });
    expect(readPanelWidths()).toEqual({ left: 440, right: 280 });
    fireEvent.doubleClick(leftHandle());
    fireEvent.keyDown(rightHandle(), { key: "Enter" });
    expect(readPanelWidths()).toEqual({ left: 282, right: 320 });
    fireEvent.keyDown(leftHandle(), { key: "ArrowRight" });
    unmount();
    render(<Panels />);
    expect(leftHandle()).toHaveAttribute("aria-valuenow", "292");
  });

  it("clamps pointer dragging in both directions and only saves when released", () => {
    render(<Panels />);
    fireEvent.pointerDown(leftHandle(), { clientX: 282, pointerId: 7 });
    fireEvent.pointerMove(window, { clientX: 1000, pointerId: 8 });
    expect(leftHandle()).toHaveAttribute("aria-valuenow", "282");
    fireEvent.pointerMove(window, { clientX: 1000, pointerId: 7 });
    expect(leftHandle()).toHaveAttribute("aria-valuenow", "440");
    expect(localStorage.getItem(PANEL_WIDTHS_KEY)).toBeNull();
    fireEvent.pointerUp(window, { pointerId: 7 });
    expect(readPanelWidths().left).toBe(440);
    fireEvent.pointerDown(rightHandle(), { clientX: 1120 });
    fireEvent.pointerMove(window, { clientX: 3000 });
    fireEvent.pointerUp(window);
    expect(readPanelWidths().right).toBe(280);
    expect(screen.getByTestId("shell")).toHaveAttribute("data-resizing", "false");
  });

  it.each(["escape", "cancel", "blur"])("restores the previous size on %s without saving a half-finished drag", (reason) => {
    render(<Panels />);
    fireEvent.pointerDown(leftHandle(), { clientX: 282 });
    fireEvent.pointerMove(window, { clientX: 410 });
    if (reason === "escape") fireEvent.keyDown(window, { key: "Escape" });
    else if (reason === "cancel") fireEvent.pointerCancel(window);
    else fireEvent.blur(window);
    expect(leftHandle()).toHaveAttribute("aria-valuenow", "282");
    expect(localStorage.getItem(PANEL_WIDTHS_KEY)).toBeNull();
    expect(screen.getByTestId("shell")).toHaveAttribute("data-resizing", "false");
  });

  it("fits narrower windows and hidden panels without overwriting the saved preference", () => {
    localStorage.setItem(PANEL_WIDTHS_KEY, JSON.stringify({ left: 440, right: 480 }));
    const { rerender } = render(<Panels />);
    vi.spyOn(window, "innerWidth", "get").mockReturnValue(1200);
    act(() => measure());
    expect(Number(leftHandle().getAttribute("aria-valuenow")) + Number(rightHandle().getAttribute("aria-valuenow"))).toBe(720);
    expect(readPanelWidths()).toEqual({ left: 440, right: 480 });
    rerender(<Panels left={false} />);
    expect(screen.queryByRole("separator", { name: "Ajustar ancho de la barra lateral" })).toBeNull();
    expect(rightHandle()).toHaveAttribute("aria-valuenow", "480");
    vi.spyOn(window, "innerWidth", "get").mockReturnValue(1440);
    act(() => measure());
    rerender(<Panels />);
    expect(leftHandle()).toHaveAttribute("aria-valuenow", "440");
    expect(rightHandle()).toHaveAttribute("aria-valuenow", "480");
  });
});
