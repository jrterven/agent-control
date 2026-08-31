import { useEffect, useRef } from "react";
import { getCurrentLanguage } from "../i18n";
import { useAppStore } from "../store/appStore";
import { api } from "./api";

const readInFlight = new Set<string>();

export function notificationSupportAvailable() {
  return typeof window !== "undefined"
    && "Notification" in window
    && "serviceWorker" in navigator
    && "PushManager" in window;
}

function applicationServerKey(value: string) {
  const padding = "=".repeat((4 - value.length % 4) % 4);
  const binary = window.atob((value + padding).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

export async function currentPushSubscription(): Promise<PushSubscription | null> {
  if (!notificationSupportAvailable()) return null;
  const registration = await navigator.serviceWorker.getRegistration();
  return registration ? registration.pushManager.getSubscription() : null;
}

export async function enablePushNotifications(csrfToken?: string) {
  if (!notificationSupportAvailable()) throw new Error("Push notifications are unavailable");
  const permission = await Notification.requestPermission();
  if (permission !== "granted") throw new DOMException("Notification permission denied", "NotAllowedError");
  const [config, registration] = await Promise.all([
    api.notificationConfig(),
    navigator.serviceWorker.ready,
  ]);
  if (!config.available) throw new Error("Push notifications are unavailable");
  const existing = await registration.pushManager.getSubscription();
  const subscription = existing ?? await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: applicationServerKey(config.applicationServerKey),
  });
  const serialized = subscription.toJSON();
  if (!serialized.endpoint || !serialized.keys?.p256dh || !serialized.keys.auth) {
    if (!existing) await subscription.unsubscribe().catch(() => false);
    throw new Error("Browser returned an incomplete push subscription");
  }
  try {
    await api.subscribeNotifications({
      endpoint: serialized.endpoint,
      keys: { p256dh: serialized.keys.p256dh, auth: serialized.keys.auth },
      locale: getCurrentLanguage(),
    }, csrfToken);
  } catch (error) {
    if (!existing) await subscription.unsubscribe().catch(() => false);
    throw error;
  }
  return subscription;
}

export async function disablePushNotifications(csrfToken?: string) {
  const subscription = await currentPushSubscription();
  if (!subscription) return;
  await api.unsubscribeNotifications(subscription.endpoint, csrfToken);
  await subscription.unsubscribe();
}

async function syncExistingPushSubscription(csrfToken?: string) {
  if (!notificationSupportAvailable() || Notification.permission !== "granted") return;
  const subscription = await currentPushSubscription();
  const serialized = subscription?.toJSON();
  if (!serialized?.endpoint || !serialized.keys?.p256dh || !serialized.keys.auth) return;
  await api.subscribeNotifications({
    endpoint: serialized.endpoint,
    keys: { p256dh: serialized.keys.p256dh, auth: serialized.keys.auth },
    locale: getCurrentLanguage(),
  }, csrfToken);
}

export async function markSessionRead(sessionId: string) {
  const state = useAppStore.getState();
  const session = state.sessions.find((item) => item.id === sessionId);
  if (!session?.unread || readInFlight.has(sessionId)) return;
  state.updateSession(sessionId, { unread: false });
  if (state.demoMode || state.authState !== "authenticated") return;
  readInFlight.add(sessionId);
  try {
    await api.markSessionRead(sessionId, state.csrfToken);
  } catch {
    useAppStore.getState().updateSession(sessionId, { unread: true });
  } finally {
    readInFlight.delete(sessionId);
  }
}

export function recordSessionCompletion(sessionId: string, occurredAt?: string) {
  const state = useAppStore.getState();
  if (!state.sessions.some((session) => session.id === sessionId)) return;
  state.updateSession(sessionId, {
    unread: true,
    updatedAt: occurredAt || new Date().toISOString(),
  });
  if (
    document.visibilityState === "visible"
    && window.location.pathname === "/chats"
    && state.selectedSessionId === sessionId
  ) {
    void markSessionRead(sessionId);
  }
}

type NotificationWorkerMessage = {
  type?: string;
  sessionId?: string;
  occurredAt?: string;
};

const OPEN_SESSION_EVENT = "agent-control:open-session";

export function requestOpenSession(sessionId: string) {
  window.dispatchEvent(new CustomEvent(OPEN_SESSION_EVENT, { detail: { sessionId } }));
}

export function useChatNotificationRuntime(
  openSession: (sessionId: string) => void,
) {
  const authState = useAppStore((state) => state.authState);
  const bootstrapLoaded = useAppStore((state) => state.bootstrapLoaded);
  const selectedSessionId = useAppStore((state) => state.selectedSessionId);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const initialLinkHandled = useRef(false);

  useEffect(() => {
    if (authState !== "authenticated" || !bootstrapLoaded) return;
    // Re-register an already-authorized browser subscription after a Control
    // restore or server-side endpoint disablement, without prompting again.
    void syncExistingPushSubscription(csrfToken).catch(() => undefined);
  }, [authState, bootstrapLoaded, csrfToken]);

  useEffect(() => {
    if (authState !== "authenticated" || !bootstrapLoaded || initialLinkHandled.current) return;
    const target = new URLSearchParams(window.location.search).get("session");
    initialLinkHandled.current = true;
    if (target && useAppStore.getState().sessions.some((session) => session.id === target)) {
      openSession(target);
      void markSessionRead(target);
      window.history.replaceState(window.history.state, "", "/chats");
    }
  }, [authState, bootstrapLoaded, openSession]);

  useEffect(() => {
    const onOpenSession = (event: Event) => {
      const sessionId = (event as CustomEvent<{ sessionId?: string }>).detail?.sessionId;
      if (!sessionId || !useAppStore.getState().sessions.some((session) => session.id === sessionId)) return;
      openSession(sessionId);
      void markSessionRead(sessionId);
    };
    window.addEventListener(OPEN_SESSION_EVENT, onOpenSession);
    return () => window.removeEventListener(OPEN_SESSION_EVENT, onOpenSession);
  }, [openSession]);

  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;
    const onMessage = (event: MessageEvent<NotificationWorkerMessage>) => {
      const message = event.data;
      if (message?.type === "agent-control:notification" && message.sessionId) {
        recordSessionCompletion(message.sessionId, message.occurredAt);
      }
      if (message?.type === "agent-control:open-session" && message.sessionId) {
        requestOpenSession(message.sessionId);
      }
    };
    navigator.serviceWorker.addEventListener("message", onMessage);
    return () => navigator.serviceWorker.removeEventListener("message", onMessage);
  }, [openSession]);

  useEffect(() => {
    if (authState !== "authenticated" || !selectedSessionId) return;
    const acknowledgeVisibleChat = () => {
      if (document.visibilityState === "visible" && window.location.pathname === "/chats") {
        void markSessionRead(selectedSessionId);
      }
    };
    acknowledgeVisibleChat();
    document.addEventListener("visibilitychange", acknowledgeVisibleChat);
    window.addEventListener("focus", acknowledgeVisibleChat);
    return () => {
      document.removeEventListener("visibilitychange", acknowledgeVisibleChat);
      window.removeEventListener("focus", acknowledgeVisibleChat);
    };
  }, [authState, selectedSessionId]);
}
