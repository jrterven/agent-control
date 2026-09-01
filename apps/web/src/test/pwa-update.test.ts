import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const sw = vi.hoisted(() => ({
  options: undefined as undefined | Record<string, (...args: unknown[]) => void>,
  registration: {
    waiting: null as ServiceWorker | null,
    installing: null as ServiceWorker | null,
    update: vi.fn(async () => undefined),
  },
  updateServiceWorker: vi.fn(async () => undefined),
}));

vi.mock("virtual:pwa-register", () => ({
  registerSW: vi.fn((options: Record<string, (...args: unknown[]) => void>) => {
    sw.options = options;
    options.onRegisteredSW?.("/sw.js", sw.registration);
    return sw.updateServiceWorker;
  }),
}));

import {
  checkForPwaUpdate,
  initializePwaUpdates,
  requestPwaUpdate,
  resetPwaUpdateStateForTests,
  restorePwaUpdateContext,
  setPwaUpdateReloadForTests,
  usePwaUpdateStore,
} from "../lib/pwaUpdate";
import { useAppStore } from "../store/appStore";

describe("PWA update coordination", () => {
  let serviceWorkerEvents: EventTarget;

  beforeEach(() => {
    vi.useFakeTimers();
    resetPwaUpdateStateForTests();
    sw.registration.waiting = null;
    sw.registration.installing = null;
    sw.registration.update.mockClear();
    sw.updateServiceWorker.mockClear();
    sessionStorage.clear();
    serviceWorkerEvents = new EventTarget();
    Object.defineProperty(navigator, "serviceWorker", {
      configurable: true,
      value: {
        controller: {} as ServiceWorker,
        addEventListener: serviceWorkerEvents.addEventListener.bind(serviceWorkerEvents),
        removeEventListener: serviceWorkerEvents.removeEventListener.bind(serviceWorkerEvents),
        getRegistration: vi.fn(async () => sw.registration),
      },
    });
    initializePwaUpdates();
  });

  afterEach(() => {
    resetPwaUpdateStateForTests();
    vi.useRealTimers();
  });

  it("queues activation while the user has unfinished work", async () => {
    sw.options?.onNeedRefresh?.();
    usePwaUpdateStore.getState().setBlocker("draft", true);

    await expect(requestPwaUpdate()).resolves.toBe("deferred");

    expect(usePwaUpdateStore.getState().deferred).toBe(true);
    expect(sw.updateServiceWorker).not.toHaveBeenCalled();

    usePwaUpdateStore.getState().setBlocker("draft", false);
    await expect(requestPwaUpdate()).resolves.toBe("applying");
    expect(sw.updateServiceWorker).toHaveBeenCalledWith(true);
  });

  it("preserves the selected conversation across the update reload", async () => {
    useAppStore.setState({
      selectedGatewayId: "gateway-a",
      selectedProfileId: "profile-a",
      selectedWorkspaceId: "",
      selectedSessionId: "session-a",
    });
    sw.options?.onNeedRefresh?.();

    await requestPwaUpdate();
    useAppStore.setState({
      selectedGatewayId: "",
      selectedProfileId: "",
      selectedWorkspaceId: "workspace-other",
      selectedSessionId: "",
    });
    restorePwaUpdateContext();

    expect(useAppStore.getState()).toMatchObject({
      selectedGatewayId: "gateway-a",
      selectedProfileId: "profile-a",
      selectedWorkspaceId: "",
      selectedSessionId: "session-a",
    });
  });

  it("reports an already waiting worker during a manual check", async () => {
    sw.registration.waiting = {} as ServiceWorker;

    await expect(checkForPwaUpdate()).resolves.toBe(true);

    expect(usePwaUpdateStore.getState().status).toBe("available");
  });

  it("retries the activation message when Android exposes the waiting worker late", async () => {
    const waitingWorker = { postMessage: vi.fn(), state: "installed" } as unknown as ServiceWorker;
    sw.options?.onNeedRefresh?.();

    await expect(requestPwaUpdate()).resolves.toBe("applying");
    await vi.advanceTimersByTimeAsync(0);
    sw.registration.waiting = waitingWorker;
    await vi.advanceTimersByTimeAsync(250);

    expect(waitingWorker.postMessage).toHaveBeenCalledWith({ type: "SKIP_WAITING" });
    expect(usePwaUpdateStore.getState().status).toBe("applying");
  });

  it("does not let a duplicate Workbox notification cancel activation", async () => {
    sw.options?.onNeedRefresh?.();

    await expect(requestPwaUpdate()).resolves.toBe("applying");
    sw.options?.onNeedRefresh?.();

    expect(usePwaUpdateStore.getState().status).toBe("applying");
  });

  it("returns to a visible retry state instead of staying stuck", async () => {
    sw.options?.onNeedRefresh?.();

    await expect(requestPwaUpdate()).resolves.toBe("applying");
    await vi.advanceTimersByTimeAsync(12_000);

    expect(usePwaUpdateStore.getState()).toMatchObject({
      status: "available",
      deferred: false,
      error: "update-activation-timeout",
    });
  });

  it("reloads after the replacement worker takes control", async () => {
    const reload = vi.fn();
    setPwaUpdateReloadForTests(reload);
    sw.options?.onNeedRefresh?.();
    await requestPwaUpdate();

    serviceWorkerEvents.dispatchEvent(new Event("controllerchange"));
    expect(usePwaUpdateStore.getState().status).toBe("current");
    await vi.advanceTimersByTimeAsync(250);

    expect(reload).toHaveBeenCalledOnce();
  });
});
