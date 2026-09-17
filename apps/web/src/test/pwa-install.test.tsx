import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PwaInstallInvitation } from "../components/PwaInstallInvitation";
import { dismissPwaInstall, initializePwaInstall, requestPwaInstall, usePwaInstallStore } from "../lib/pwaInstall";
import { usePwaUpdateStore } from "../lib/pwaUpdate";
import { useAppStore } from "../store/appStore";
import i18n from "../i18n";

vi.mock("virtual:pwa-register", () => ({ registerSW: vi.fn() }));

const DISMISS_KEY = "agent-control:pwa-install-dismissed-until";
const INSTALLED_KEY = "agent-control:pwa-installed";
const WEEK = 7 * 24 * 60 * 60 * 1_000;
const title = "Lleva Agent Control contigo";
let dispose: (() => void) | undefined;
let restores: (() => void)[] = [];
let standaloneMode = false;

function property(target: object, key: PropertyKey, value: unknown) {
  const previous = Object.getOwnPropertyDescriptor(target, key);
  Object.defineProperty(target, key, { configurable: true, value });
  restores.push(() => {
    if (previous) Object.defineProperty(target, key, previous);
    else Reflect.deleteProperty(target, key);
  });
}

function initialize() { dispose = initializePwaInstall(); }
function nativeOffer(outcome: "accepted" | "dismissed" = "accepted") {
  const prompt = vi.fn(async () => ({ outcome }));
  const event = Object.assign(new Event("beforeinstallprompt", { cancelable: true }), { prompt });
  act(() => window.dispatchEvent(event));
  return { event, prompt };
}
async function advance(milliseconds = 8_000) {
  await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds); });
}
function invitation() { return screen.queryByRole("heading", { name: title }); }

beforeEach(async () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2026-09-17T12:00:00Z"));
  await i18n.changeLanguage("es");
  localStorage.clear();
  standaloneMode = false;
  property(navigator, "userAgent", "Mozilla/5.0 Chrome/130.0.0.0 Safari/537.36");
  property(navigator, "platform", "Linux x86_64");
  property(navigator, "maxTouchPoints", 0);
  property(navigator, "standalone", false);
  property(window, "isSecureContext", true);
  property(document, "visibilityState", "visible");
  property(window, "matchMedia", vi.fn((query: string) => ({
    matches: query === "(min-width: 0px)" || (standaloneMode && query === "(display-mode: standalone)"),
    media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(),
    addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  })));
  useAppStore.setState({ authState: "authenticated", streamingBySession: {} });
  usePwaUpdateStore.setState({ status: "idle", blockers: { draft: false, streaming: false, dictation: false, speech: false } });
  usePwaInstallStore.setState({ installed: false, dismissedUntil: 0, mode: null });
});

afterEach(() => {
  cleanup();
  dispose?.();
  dispose = undefined;
  restores.reverse().forEach((restore) => restore());
  restores = [];
  vi.restoreAllMocks();
  vi.useRealTimers();
});

describe("PWA install invitation", () => {
  it("retains an offer captured before the UI mounts and opens the browser prompt only on a click", async () => {
    initialize();
    const { event, prompt } = nativeOffer();
    expect(event.defaultPrevented).toBe(true);
    render(<PwaInstallInvitation />);
    await advance(7_999);
    expect(invitation()).not.toBeInTheDocument();
    expect(prompt).not.toHaveBeenCalled();
    await advance(1);
    expect(invitation()).toBeInTheDocument();
    expect(prompt).not.toHaveBeenCalled();

    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Instalar app" })); });
    expect(prompt).toHaveBeenCalledOnce();
    await expect(requestPwaInstall()).resolves.toBe("unavailable");
    expect(prompt).toHaveBeenCalledOnce();
    expect(invitation()).not.toBeInTheDocument();
    expect(usePwaInstallStore.getState().installed).toBe(false);
    expect(localStorage.getItem(INSTALLED_KEY)).not.toBe("1");

    act(() => window.dispatchEvent(new Event("appinstalled")));
    expect(usePwaInstallStore.getState().installed).toBe(true);
    expect(localStorage.getItem(INSTALLED_KEY)).toBe("1");
  });

  it("supports browsers that return the outcome through userChoice", async () => {
    initialize();
    const prompt = vi.fn(async () => undefined);
    window.dispatchEvent(Object.assign(new Event("beforeinstallprompt"), {
      prompt, userChoice: Promise.resolve({ outcome: "dismissed" }),
    }));
    await expect(requestPwaInstall()).resolves.toBe("dismissed");
    expect(prompt).toHaveBeenCalledOnce();
    expect(usePwaInstallStore.getState()).toMatchObject({ mode: null, installed: false, dismissedUntil: Date.now() + WEEK });
  });

  it("persists dismissal across a new page and offers again after seven days with a fresh delay", async () => {
    initialize();
    const { prompt } = nativeOffer();
    const view = render(<PwaInstallInvitation />);
    await advance();
    fireEvent.click(screen.getByRole("button", { name: "Ahora no" }));
    const until = Number(localStorage.getItem(DISMISS_KEY));
    expect(until).toBe(Date.now() + WEEK);
    expect(prompt).not.toHaveBeenCalled();
    expect(invitation()).not.toBeInTheDocument();

    view.unmount();
    dispose?.();
    usePwaInstallStore.setState({ dismissedUntil: 0 });
    initialize();
    nativeOffer();
    const reopened = render(<PwaInstallInvitation />);
    await advance(60_000);
    expect(invitation()).not.toBeInTheDocument();

    reopened.unmount();
    dispose?.();
    vi.setSystemTime(until + 1);
    initialize();
    nativeOffer();
    render(<PwaInstallInvitation />);
    expect(invitation()).not.toBeInTheDocument();
    await advance();
    expect(invitation()).toBeInTheDocument();
  });

  it("can dismiss safely when storage is denied and does not reappear on remount", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => { throw new DOMException("Denied", "SecurityError"); });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => { throw new DOMException("Denied", "SecurityError"); });
    initialize();
    nativeOffer();
    const view = render(<PwaInstallInvitation />);
    await advance();
    fireEvent.click(screen.getByRole("button", { name: "Cerrar invitación de instalación" }));
    expect(usePwaInstallStore.getState().dismissedUntil).toBe(Date.now() + WEEK);
    view.unmount();
    render(<PwaInstallInvitation />);
    await advance(60_000);
    expect(invitation()).not.toBeInTheDocument();
  });

  it.each(["display-mode", "navigator.standalone"])("does not offer installation when already launched as %s", async (source) => {
    if (source === "display-mode") standaloneMode = true;
    else property(navigator, "standalone", true);
    initialize();
    const { prompt } = nativeOffer();
    render(<PwaInstallInvitation />);
    await advance();
    expect(invitation()).not.toBeInTheDocument();
    await expect(requestPwaInstall()).resolves.toBe("unavailable");
    expect(prompt).not.toHaveBeenCalled();
    expect(usePwaInstallStore.getState().installed).toBe(true);
  });

  it.each([
    { name: "iPhone", userAgent: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit Safari/604.1", platform: "iPhone", touches: 5 },
    { name: "iPad desktop user agent", userAgent: "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15) AppleWebKit Safari/605.1", platform: "MacIntel", touches: 5 },
  ])("shows manual instructions for $name without invoking a native prompt", async ({ userAgent, platform, touches }) => {
    property(navigator, "userAgent", userAgent);
    property(navigator, "platform", platform);
    property(navigator, "maxTouchPoints", touches);
    initialize();
    render(<PwaInstallInvitation />);
    await advance();
    expect(screen.queryByRole("button", { name: "Instalar app" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cómo instalar" }));
    const dialog = screen.getByRole("dialog", { name: "Instalar en iPhone o iPad" });
    expect(within(dialog).getByText(/Compartir.*Añadir a pantalla de inicio/)).toBeInTheDocument();
    await expect(requestPwaInstall()).resolves.toBe("unavailable");
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await advance(60_000);
    expect(invitation()).not.toBeInTheDocument();
    expect(usePwaInstallStore.getState().installed).toBe(false);
  });

  it("does not treat a non-touch Mac as an iPad or invent a native browser offer", async () => {
    property(navigator, "platform", "MacIntel");
    initialize();
    render(<PwaInstallInvitation />);
    await advance();
    expect(invitation()).not.toBeInTheDocument();
    expect(usePwaInstallStore.getState().mode).toBeNull();
  });

  it.each(["draft", "streaming", "dictation", "speech"] as const)("waits for the %s blocker to clear before starting the invitation delay", async (blocker) => {
    usePwaUpdateStore.getState().setBlocker(blocker, true);
    initialize();
    nativeOffer();
    render(<PwaInstallInvitation />);
    await advance(20_000);
    expect(invitation()).not.toBeInTheDocument();
    act(() => usePwaUpdateStore.getState().setBlocker(blocker, false));
    await advance(7_999);
    expect(invitation()).not.toBeInTheDocument();
    await advance(1);
    expect(invitation()).toBeInTheDocument();
  });

  it("waits for live session streaming and hides an existing offer if streaming begins", async () => {
    useAppStore.setState({ streamingBySession: { "session-one": "message-one" } });
    initialize();
    nativeOffer();
    render(<PwaInstallInvitation />);
    await advance();
    expect(invitation()).not.toBeInTheDocument();
    act(() => useAppStore.setState({ streamingBySession: {} }));
    await advance();
    expect(invitation()).toBeInTheDocument();
    act(() => useAppStore.setState({ streamingBySession: { "session-two": "message-two" } }));
    expect(invitation()).not.toBeInTheDocument();
  });

  it("defers the invitation while a form field has focus", async () => {
    initialize();
    nativeOffer();
    render(<><textarea aria-label="Borrador" /><PwaInstallInvitation /></>);
    const input = screen.getByRole("textbox", { name: "Borrador" });
    input.focus();
    await advance(20_000);
    expect(invitation()).not.toBeInTheDocument();
    expect(input).toHaveFocus();
    input.blur();
    await advance(1_000);
    expect(invitation()).toBeInTheDocument();
  });

  it("defers the invitation until another modal closes", async () => {
    initialize();
    nativeOffer();
    const view = render(<><div role="dialog" aria-modal="true">Confirmar equipo</div><PwaInstallInvitation /></>);
    await advance(20_000);
    expect(invitation()).not.toBeInTheDocument();
    view.rerender(<>{null}<PwaInstallInvitation /></>);
    await advance(1_000);
    expect(invitation()).toBeInTheDocument();
  });

  it("offers only after authentication and hides when signing out", async () => {
    useAppStore.setState({ authState: "unauthenticated" });
    initialize();
    nativeOffer();
    render(<PwaInstallInvitation />);
    await advance();
    expect(invitation()).not.toBeInTheDocument();
    act(() => useAppStore.setState({ authState: "authenticated" }));
    await advance();
    expect(invitation()).toBeInTheDocument();
    act(() => useAppStore.setState({ authState: "unauthenticated" }));
    expect(invitation()).not.toBeInTheDocument();
  });

  it("keeps browser failures recoverable without falsely marking the app installed", async () => {
    initialize();
    const { prompt } = nativeOffer();
    prompt.mockRejectedValueOnce(new Error("Prompt unavailable"));
    render(<PwaInstallInvitation />);
    await advance();
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "Instalar app" })); });
    expect(screen.getByText(/No se pudo abrir la instalación/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Instalar app" })).not.toBeInTheDocument();
    expect(usePwaInstallStore.getState().installed).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Entendido" }));
    expect(invitation()).not.toBeInTheDocument();
  });

  it("disposes browser listeners and permits a clean reinitialization", async () => {
    initialize();
    expect(initializePwaInstall()).toBe(dispose);
    dispose?.();
    const ignored = nativeOffer();
    expect(ignored.event.defaultPrevented).toBe(false);
    await expect(requestPwaInstall()).resolves.toBe("unavailable");
    initialize();
    const active = nativeOffer();
    expect(active.event.defaultPrevented).toBe(true);
    act(() => dismissPwaInstall());
    expect(usePwaInstallStore.getState().dismissedUntil).toBe(Date.now() + WEEK);
  });
});
