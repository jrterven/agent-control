import { BellRinging, BellSlash, FolderSimple, X } from "@phosphor-icons/react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  currentPushSubscription,
  disablePushNotifications,
  enablePushNotifications,
  notificationSupportAvailable,
  requestOpenSession,
} from "../lib/chatNotifications";
import { useAppStore } from "../store/appStore";
import type { SessionSummary } from "../types";

type RecentGroup = {
  key: string;
  label: string;
  sessions: SessionSummary[];
};

function dayKey(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "unknown" : `${date.getFullYear()}-${date.getMonth()}-${date.getDate()}`;
}

function dayLabel(value: string, language: string, t: (key: string) => string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return t("notifications.recentChats");
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(today.getDate() - 1);
  if (dayKey(value) === dayKey(today.toISOString())) return t("notifications.today");
  if (dayKey(value) === dayKey(yesterday.toISOString())) return t("notifications.yesterday");
  return new Intl.DateTimeFormat(language, { weekday: "long", day: "numeric", month: "short" }).format(date);
}

export function NotificationMenu() {
  const { t, i18n } = useTranslation();
  const open = useAppStore((state) => state.notificationsOpen);
  const setOpen = useAppStore((state) => state.setNotificationsOpen);
  const sessions = useAppStore((state) => state.sessions);
  const workspaces = useAppStore((state) => state.workspaces);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const selectedSessionId = useAppStore((state) => state.selectedSessionId);
  const panelRef = useRef<HTMLElement>(null);
  const [subscribed, setSubscribed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const supported = notificationSupportAvailable();
  const permission = supported ? Notification.permission : "unsupported";

  const groups = useMemo(() => {
    const recent = sessions
      .filter((session) => !session.archived)
      .sort((left, right) => Date.parse(right.updatedAt) - Date.parse(left.updatedAt))
      .slice(0, 10);
    const result: RecentGroup[] = [];
    for (const session of recent) {
      const key = dayKey(session.updatedAt);
      const existing = result.find((group) => group.key === key);
      if (existing) existing.sessions.push(session);
      else result.push({
        key,
        label: dayLabel(session.updatedAt, i18n.resolvedLanguage ?? i18n.language, t),
        sessions: [session],
      });
    }
    return result;
  }, [i18n.language, i18n.resolvedLanguage, sessions, t]);

  useEffect(() => {
    if (!open) return;
    let active = true;
    void currentPushSubscription()
      .then((subscription) => { if (active) setSubscribed(Boolean(subscription)); })
      .catch(() => { if (active) setSubscribed(false); });
    const onPointerDown = (event: PointerEvent) => {
      if (!panelRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      active = false;
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open, setOpen]);

  if (!open) return null;

  const toggleSystemNotifications = async () => {
    setBusy(true);
    setError("");
    try {
      if (subscribed) {
        await disablePushNotifications(csrfToken);
        setSubscribed(false);
      } else {
        await enablePushNotifications(csrfToken);
        setSubscribed(true);
      }
    } catch {
      setError(t(subscribed ? "notifications.disableError" : "notifications.enableError"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button className="notification-scrim" type="button" aria-label={t("notifications.close")} onClick={() => setOpen(false)} />
      <aside ref={panelRef} id="notification-menu" className="notification-menu" aria-label={t("notifications.title")}>
        <header className="notification-menu__header">
          <div>
            <span className="eyebrow">{t("notifications.recentChats")}</span>
            <h2>{t("notifications.title")}</h2>
          </div>
          <button className="notification-menu__close" type="button" aria-label={t("notifications.close")} onClick={() => setOpen(false)}>
            <X size={20} />
          </button>
        </header>

        <div className="notification-menu__list" data-testid="recent-notification-chats">
          {groups.length ? groups.map((group) => (
            <section className="notification-day" key={group.key}>
              <h3>{group.label}</h3>
              {group.sessions.map((session) => {
                const workspace = workspaces.find((item) => item.id === session.workspaceId);
                return (
                  <button
                    className={`notification-chat${session.id === selectedSessionId ? " is-current" : ""}`}
                    type="button"
                    key={session.id}
                    aria-label={t("notifications.openChat", { title: session.title })}
                    onClick={() => requestOpenSession(session.id)}
                  >
                    <span className="notification-chat__title">{session.title}</span>
                    <span className="notification-chat__workspace"><FolderSimple size={16} />{workspace?.name ?? t("notifications.noWorkspace")}</span>
                    {session.unread ? <span className="notification-chat__unread" aria-label={t("notifications.unread")} /> : null}
                  </button>
                );
              })}
            </section>
          )) : <p className="notification-menu__empty">{t("notifications.empty")}</p>}
        </div>

        <footer className="notification-menu__footer">
          {!supported ? (
            <span><BellSlash size={18} />{t("notifications.unsupported")}</span>
          ) : permission === "denied" ? (
            <span><BellSlash size={18} />{t("notifications.denied")}</span>
          ) : (
            <button type="button" disabled={busy} onClick={toggleSystemNotifications}>
              <BellRinging size={18} />
              {busy ? t("notifications.enabling") : subscribed ? t("notifications.disable") : t("notifications.enable")}
            </button>
          )}
          {error ? <small role="alert">{error}</small> : subscribed ? <small>{t("notifications.enabled")}</small> : null}
        </footer>
      </aside>
    </>
  );
}
