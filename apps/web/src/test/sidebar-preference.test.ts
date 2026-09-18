import { afterEach, describe, expect, it, vi } from "vitest";
import { readDesktopSidebarOpen, saveDesktopSidebarOpen } from "../lib/sidebarPreference";

afterEach(() => {
  vi.restoreAllMocks();
  localStorage.removeItem("agent-control.desktop-sidebar-open");
});

describe("desktop navigation preference", () => {
  it("starts expanded and restores the saved preference", () => {
    localStorage.removeItem("agent-control.desktop-sidebar-open");
    expect(readDesktopSidebarOpen()).toBe(true);
    saveDesktopSidebarOpen(false);
    expect(readDesktopSidebarOpen()).toBe(false);
    saveDesktopSidebarOpen(true);
    expect(readDesktopSidebarOpen()).toBe(true);
  });

  it("works when browser storage is blocked", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Blocked", "SecurityError"); });
    expect(readDesktopSidebarOpen()).toBe(true);
    expect(() => saveDesktopSidebarOpen(false)).not.toThrow();
  });
});
