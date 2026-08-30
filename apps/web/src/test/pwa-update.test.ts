import { beforeEach, describe, expect, it, vi } from "vitest";

const sw = vi.hoisted(() => ({
  options: undefined as undefined | Record<string, (...args: unknown[]) => void>,
  registration: {
    waiting: null as ServiceWorker | null,
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
  usePwaUpdateStore,
} from "../lib/pwaUpdate";
import { useAppStore } from "../store/appStore";

describe("PWA update coordination", () => {
  beforeEach(() => {
    resetPwaUpdateStateForTests();
    sw.registration.waiting = null;
    sw.registration.update.mockClear();
    sw.updateServiceWorker.mockClear();
    sessionStorage.clear();
    Object.defineProperty(navigator, "serviceWorker", { configurable: true, value: {} });
    initializePwaUpdates();
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
});
