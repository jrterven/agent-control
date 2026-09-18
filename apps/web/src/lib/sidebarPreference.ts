const storageKey = "agent-control.desktop-sidebar-open";

export function readDesktopSidebarOpen(): boolean {
  try {
    return localStorage.getItem(storageKey) !== "false";
  } catch {
    return true;
  }
}

export function saveDesktopSidebarOpen(open: boolean): void {
  try {
    localStorage.setItem(storageKey, String(open));
  } catch {
    // The navigation control also works when browser storage is unavailable.
  }
}
