import { useEffect, useState } from "react";
import { CheckCircle, CircleNotch, Clock, WarningCircle, XCircle } from "@phosphor-icons/react";
import { useTranslation } from "react-i18next";
import { useAppStore } from "../store/appStore";
import type { BackgroundTaskSnapshot, ChatMessage } from "../types";

export function taskResultAnchor(messageId: string) {
  return `task-result-${encodeURIComponent(messageId)}`;
}

export function BackgroundTaskList({ snapshot, messages }: { snapshot: BackgroundTaskSnapshot; messages: ChatMessage[] }) {
  const { t } = useTranslation();
  const [now, setNow] = useState(Date.now);
  const hasActive = snapshot.items.some((task) => task.state === "running" || task.state === "queued");
  useEffect(() => {
    if (!hasActive) return;
    const timer = window.setInterval(() => setNow(Date.now()), 60_000);
    return () => window.clearInterval(timer);
  }, [hasActive]);
  if (!snapshot.items.length) return null;
  // Viewing a response is navigation, not acknowledgement of native delivery.
  // A stream or tool-only progress entry is not the task's finished response.
  const results = new Map(messages.filter((message) => message.role === "assistant"
    && message.controlTurnOrigin?.kind === "background_task" && message.controlTurnOrigin.taskId
    && !message.streaming && (message.content.trim() || message.media?.some((media) => media.kind === "audio") || message.emailReferences?.length))
    .map((message) => [message.controlTurnOrigin!.taskId, message]));
  return <details className="background-tasks">
    <summary>
      <span>{t("backgroundTasks.title")}</span>
      <span className="background-tasks__summary" role="status">{t(snapshot.activeCount ? "backgroundTasks.active" : "backgroundTasks.total", { count: snapshot.activeCount || snapshot.items.length })}{snapshot.pendingDeliveryCount ? ` · ${t("backgroundTasks.pending", { count: snapshot.pendingDeliveryCount })}` : ""}</span>
    </summary>
    {!snapshot.available ? <p className="background-tasks__notice">{t("backgroundTasks.unavailable")}</p> : !snapshot.complete ? <p className="background-tasks__notice">{t("backgroundTasks.incomplete")}</p> : null}
    <ul aria-label={t("backgroundTasks.title")}>
      {snapshot.items.map((task) => {
        const result = results.get(task.id);
        const end = Date.parse(task.completedAt ?? task.updatedAt);
        const minutes = Math.max(0, Math.floor(((task.state === "running" || task.state === "queued" ? now : end) - Date.parse(task.createdAt)) / 60_000));
        const Icon = task.state === "completed" ? CheckCircle : task.state === "failed" || task.state === "unknown" ? WarningCircle : task.state === "cancelled" ? XCircle : task.state === "queued" ? Clock : CircleNotch;
        return <li key={task.id} data-task-state={task.state}>
          <Icon aria-hidden="true" />
          <div className="background-tasks__task">
            <strong>{t("backgroundTasks.task")} <small>· {task.id.slice(0, 8)}</small></strong>
            <span>{t(`backgroundTasks.states.${task.state}`)} · {t("backgroundTasks.elapsed", { minutes })}</span>
            {task.deliveryState === "pending" && ["completed", "failed", "cancelled"].includes(task.state)
              ? <span>{t(result ? "backgroundTasks.confirmationPending" : "backgroundTasks.delivering")}</span>
              : null}
          </div>
          {result ? <a href={`#${taskResultAnchor(result.id)}`} onClick={(event) => {
            const target = document.getElementById(taskResultAnchor(result.id));
            if (target) { event.preventDefault(); target.scrollIntoView({ block: "center", behavior: "smooth" }); target.focus({ preventScroll: true }); }
          }}>{t("backgroundTasks.result")}</a> : null}
        </li>;
      })}
    </ul>
  </details>;
}

export function BackgroundTasks({ sessionId }: { sessionId: string }) {
  const snapshot = useAppStore((state) => state.backgroundTasksBySession[sessionId]);
  const messages = useAppStore((state) => state.messages);
  return snapshot ? <BackgroundTaskList snapshot={snapshot} messages={messages.filter((message) => message.sessionId === sessionId)} /> : null;
}
