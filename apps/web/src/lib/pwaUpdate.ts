import { registerSW } from "virtual:pwa-register";
import { create } from "zustand";
import { useAppStore } from "../store/appStore";

export const APP_VERSION = typeof __APP_VERSION__ === "string" ? __APP_VERSION__ : "0.1.0";

export type PwaUpdateBlocker = "dictation" | "draft" | "streaming" | "speech";
export type PwaUpdateStatus = "idle" | "checking" | "current" | "available" | "applying" | "error";

type PwaUpdateState = {
  status: PwaUpdateStatus;
  deferred: boolean;
  checkedAt?: string;
  error?: string;
  blockers: Record<PwaUpdateBlocker, boolean>;
  setBlocker: (blocker: PwaUpdateBlocker, active: boolean) => void;
};

const emptyBlockers: Record<PwaUpdateBlocker, boolean> = {
  dictation: false,
  draft: false,
  streaming: false,
  speech: false,
};

export const usePwaUpdateStore = create<PwaUpdateState>((set) => ({
  status: "idle",
  deferred: false,
  blockers: { ...emptyBlockers },
  setBlocker: (blocker, active) => set((state) => ({
    blockers: state.blockers[blocker] === active
      ? state.blockers
      : { ...state.blockers, [blocker]: active },
  })),
}));

const RETURN_CONTEXT_KEY = "agent-control:pwa-update-return";
const RETURN_CONTEXT_TTL_MS = 5 * 60 * 1000;
const CHECK_INTERVAL_MS = 60 * 60 * 1000;
const VISIBILITY_CHECK_COOLDOWN_MS = 5 * 60 * 1000;

let registration: ServiceWorkerRegistration | undefined;
let updateServiceWorker: ((reloadPage?: boolean) => Promise<void>) | undefined;
let initialized = false;
let lastAutomaticCheck = 0;
let checkInterval: number | undefined;
let visibilityListener: (() => void) | undefined;

export function hasPwaUpdateBlockers(blockers = usePwaUpdateStore.getState().blockers) {
  return Object.values(blockers).some(Boolean);
}

function captureReturnContext() {
  if (typeof sessionStorage === "undefined") return;
  const state = useAppStore.getState();
  try {
    sessionStorage.setItem(RETURN_CONTEXT_KEY, JSON.stringify({
      expiresAt: Date.now() + RETURN_CONTEXT_TTL_MS,
      selectedGatewayId: state.selectedGatewayId,
      selectedProfileId: state.selectedProfileId,
      selectedWorkspaceId: state.selectedWorkspaceId,
      selectedSessionId: state.selectedSessionId,
    }));
  } catch {
    // A privacy-restricted browser can reject sessionStorage. Updating still works.
  }
}

export function restorePwaUpdateContext() {
  if (typeof sessionStorage === "undefined") return;
  try {
    const raw = sessionStorage.getItem(RETURN_CONTEXT_KEY);
    sessionStorage.removeItem(RETURN_CONTEXT_KEY);
    if (!raw) return;
    const context = JSON.parse(raw) as Partial<{
      expiresAt: number;
      selectedGatewayId: string;
      selectedProfileId: string;
      selectedWorkspaceId: string;
      selectedSessionId: string;
    }>;
    if (typeof context.expiresAt !== "number" || context.expiresAt < Date.now()) return;
    useAppStore.setState({
      selectedGatewayId: typeof context.selectedGatewayId === "string" ? context.selectedGatewayId : "",
      selectedProfileId: typeof context.selectedProfileId === "string" ? context.selectedProfileId : "",
      selectedWorkspaceId: typeof context.selectedWorkspaceId === "string" ? context.selectedWorkspaceId : "",
      selectedSessionId: typeof context.selectedSessionId === "string" ? context.selectedSessionId : "",
    });
  } catch {
    // Updating must still start when storage is disabled or contains bad data.
  }
}

function markAvailable() {
  usePwaUpdateStore.setState({ status: "available", checkedAt: new Date().toISOString(), error: undefined });
}

export async function checkForPwaUpdate(options: { silent?: boolean } = {}) {
  if (!registration) {
    if (!options.silent) usePwaUpdateStore.setState({ status: "error", error: "service-worker-unavailable" });
    return false;
  }
  if (!options.silent) usePwaUpdateStore.setState({ status: "checking", error: undefined });
  try {
    await registration.update();
    lastAutomaticCheck = Date.now();
    if (registration.waiting) {
      markAvailable();
      return true;
    }
    if (!options.silent && usePwaUpdateStore.getState().status === "checking") {
      usePwaUpdateStore.setState({ status: "current", checkedAt: new Date().toISOString(), error: undefined });
    }
    return false;
  } catch (error) {
    if (!options.silent) {
      usePwaUpdateStore.setState({
        status: "error",
        error: error instanceof Error ? error.message : "update-check-failed",
      });
    }
    return false;
  }
}

export async function requestPwaUpdate(): Promise<"deferred" | "applying" | "unavailable"> {
  const state = usePwaUpdateStore.getState();
  if (state.status !== "available" || !updateServiceWorker) return "unavailable";
  if (hasPwaUpdateBlockers(state.blockers)) {
    usePwaUpdateStore.setState({ deferred: true });
    return "deferred";
  }
  captureReturnContext();
  usePwaUpdateStore.setState({ status: "applying", deferred: false, error: undefined });
  try {
    await updateServiceWorker(true);
    return "applying";
  } catch (error) {
    usePwaUpdateStore.setState({
      status: "error",
      deferred: false,
      error: error instanceof Error ? error.message : "update-apply-failed",
    });
    return "unavailable";
  }
}

/**
 * Registers the prompt-style service worker. Detection is automatic, while
 * activation remains user-controlled so an in-progress conversation is never
 * interrupted by a surprise reload.
 */
export function initializePwaUpdates() {
  if (initialized || typeof window === "undefined" || !("serviceWorker" in navigator)) return;
  initialized = true;
  updateServiceWorker = registerSW({
    immediate: true,
    onNeedRefresh: markAvailable,
    onRegisteredSW: (_serviceWorkerUrl, serviceWorkerRegistration) => {
      registration = serviceWorkerRegistration;
      if (registration?.waiting) markAvailable();
      else void checkForPwaUpdate({ silent: true });
    },
    onRegisterError: (error) => {
      usePwaUpdateStore.setState({
        status: "error",
        error: error instanceof Error ? error.message : "service-worker-registration-failed",
      });
    },
  });

  checkInterval = window.setInterval(() => void checkForPwaUpdate({ silent: true }), CHECK_INTERVAL_MS);
  visibilityListener = () => {
    if (document.visibilityState !== "visible" || Date.now() - lastAutomaticCheck < VISIBILITY_CHECK_COOLDOWN_MS) return;
    void checkForPwaUpdate({ silent: true });
  };
  document.addEventListener("visibilitychange", visibilityListener);
}

export function resetPwaUpdateStateForTests() {
  if (checkInterval !== undefined && typeof window !== "undefined") window.clearInterval(checkInterval);
  if (visibilityListener && typeof document !== "undefined") document.removeEventListener("visibilitychange", visibilityListener);
  registration = undefined;
  updateServiceWorker = undefined;
  initialized = false;
  lastAutomaticCheck = 0;
  checkInterval = undefined;
  visibilityListener = undefined;
  usePwaUpdateStore.setState({
    status: "idle",
    deferred: false,
    checkedAt: undefined,
    error: undefined,
    blockers: { ...emptyBlockers },
  });
}
