import { create } from "zustand";
import { subscribeToMediaQuery } from "./mediaQuery";

type InstallChoice = { outcome: "accepted" | "dismissed" };
type InstallPromptEvent = Event & {
  prompt: () => Promise<InstallChoice | void>;
  userChoice?: Promise<InstallChoice>;
};
type InstallMode = "native" | "ios" | null;
type InstallState = {
  mode: InstallMode;
  installed: boolean;
  dismissedUntil: number;
};

const DISMISS_KEY = "agent-control:pwa-install-dismissed-until";
const INSTALLED_KEY = "agent-control:pwa-installed";
const SNOOZE_MS = 7 * 24 * 60 * 60 * 1000;
export const usePwaInstallStore = create<InstallState>(() => ({ mode: null, installed: false, dismissedUntil: 0 }));
let deferredPrompt: InstallPromptEvent | null = null;
let dispose: (() => void) | undefined;

function readPreference(key: string) {
  try { return localStorage.getItem(key); } catch { return null; }
}
function writePreference(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* Browsing with blocked storage must still work. */ }
}
function standalone() {
  return Boolean((navigator as Navigator & { standalone?: boolean }).standalone)
    || ["standalone", "minimal-ui", "window-controls-overlay"].some((mode) => window.matchMedia?.(`(display-mode: ${mode})`).matches);
}
function iosBrowser() {
  return /iPad|iPhone|iPod/.test(navigator.userAgent)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
}

function markInstalled() {
  deferredPrompt = null;
  writePreference(INSTALLED_KEY, "1");
  usePwaInstallStore.setState({ installed: true, mode: null });
}

/** Register before auth/bootstrap so an early browser event is not lost. */
export function initializePwaInstall() {
  if (dispose) return dispose;
  const dismissedUntil = Number(readPreference(DISMISS_KEY));
  const installed = standalone() || readPreference(INSTALLED_KEY) === "1";
  usePwaInstallStore.setState({
    installed,
    dismissedUntil: Number.isFinite(dismissedUntil) ? dismissedUntil : 0,
    mode: !installed && iosBrowser() && window.isSecureContext ? "ios" : null,
  });
  if (standalone()) markInstalled();
  const onPrompt = (raw: Event) => {
    const event = raw as InstallPromptEvent;
    if (typeof event.prompt !== "function" || standalone()) return;
    event.preventDefault();
    deferredPrompt = event;
    // A fresh browser event is authoritative if the user uninstalled the app.
    writePreference(INSTALLED_KEY, "0");
    usePwaInstallStore.setState({ mode: "native", installed: false });
  };
  const onDisplayChange = () => { if (standalone()) markInstalled(); };
  const mediaDisposers = ["standalone", "minimal-ui", "window-controls-overlay"].map((mode) => {
    const media = window.matchMedia?.(`(display-mode: ${mode})`);
    return media ? subscribeToMediaQuery(media, onDisplayChange) : () => undefined;
  });
  const onStorage = (event: StorageEvent) => {
    if (event.key === INSTALLED_KEY && event.newValue === "1") markInstalled();
    if (event.key === DISMISS_KEY) {
      const until = Number(event.newValue);
      usePwaInstallStore.setState({ dismissedUntil: Number.isFinite(until) ? until : 0 });
    }
  };
  window.addEventListener("beforeinstallprompt", onPrompt);
  window.addEventListener("appinstalled", markInstalled);
  window.addEventListener("storage", onStorage);
  dispose = () => {
    window.removeEventListener("beforeinstallprompt", onPrompt);
    window.removeEventListener("appinstalled", markInstalled);
    window.removeEventListener("storage", onStorage);
    mediaDisposers.forEach((cleanup) => cleanup());
    deferredPrompt = null;
    dispose = undefined;
  };
  return dispose;
}

export function dismissPwaInstall() {
  const dismissedUntil = Date.now() + SNOOZE_MS;
  writePreference(DISMISS_KEY, String(dismissedUntil));
  usePwaInstallStore.setState({ dismissedUntil });
}

/** Called directly by a user gesture; each browser event can be used only once. */
export async function requestPwaInstall(): Promise<"accepted" | "dismissed" | "unavailable" | "error"> {
  const event = deferredPrompt;
  if (!event || standalone() || usePwaInstallStore.getState().installed) return "unavailable";
  deferredPrompt = null;
  usePwaInstallStore.setState({ mode: null });
  try {
    const result = await event.prompt();
    const choice = result ?? await event.userChoice;
    if (choice?.outcome === "accepted") {
      // appinstalled, not acceptance alone, confirms a completed installation.
      dismissPwaInstall();
      return "accepted";
    }
    dismissPwaInstall();
    return "dismissed";
  } catch {
    dismissPwaInstall();
    return "error";
  }
}
