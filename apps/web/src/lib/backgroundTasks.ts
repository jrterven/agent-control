import type { BackgroundTask, BackgroundTaskSnapshot, ControlTurnOrigin } from "../types";

const states = new Set<BackgroundTask["state"]>(["queued", "running", "completed", "failed", "cancelled", "unknown"]);
const deliveryStates = new Set<BackgroundTask["deliveryState"]>(["pending", "delivered", "dropped", "unknown"]);

function timestamp(value: unknown): string | undefined {
  return typeof value === "string" && value.length <= 64 && Number.isFinite(Date.parse(value)) ? value : undefined;
}

export function controlTurnOrigin(value: unknown): ControlTurnOrigin | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const origin = value as Record<string, unknown>;
  if (origin.kind !== "background_task") return undefined;
  return {
    kind: "background_task",
    ...(typeof origin.taskId === "string" && origin.taskId.length > 0 && origin.taskId.length <= 200 ? { taskId: origin.taskId } : {}),
  };
}

export function normalizeBackgroundTasks(value: unknown): BackgroundTaskSnapshot | undefined {
  if (!value || typeof value !== "object" || Array.isArray(value)) return undefined;
  const snapshot = value as Record<string, unknown>;
  const observedAt = timestamp(snapshot.observedAt);
  if (!Array.isArray(snapshot.items) || !observedAt || typeof snapshot.available !== "boolean" || typeof snapshot.complete !== "boolean") return undefined;
  const items = snapshot.items.slice(0, 200).flatMap((value): BackgroundTask[] => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const task = value as Record<string, unknown>;
    const createdAt = timestamp(task.createdAt);
    const updatedAt = timestamp(task.updatedAt);
    if (typeof task.id !== "string" || !task.id || task.id.length > 200 || !createdAt || !updatedAt) return [];
    return [{
      id: task.id,
      state: states.has(task.state as BackgroundTask["state"]) ? task.state as BackgroundTask["state"] : "unknown",
      deliveryState: deliveryStates.has(task.deliveryState as BackgroundTask["deliveryState"]) ? task.deliveryState as BackgroundTask["deliveryState"] : "unknown",
      title: typeof task.title === "string" ? task.title.slice(0, 160) : "",
      createdAt,
      updatedAt,
      ...(timestamp(task.completedAt) ? { completedAt: timestamp(task.completedAt) } : {}),
    }];
  });
  const count = (value: unknown) => typeof value === "number" && Number.isSafeInteger(value) && value >= 0 ? value : null;
  return {
    items: [...new Map(items.map((item) => [item.id, item])).values()],
    complete: snapshot.complete && items.length === snapshot.items.length,
    activeCount: count(snapshot.activeCount),
    pendingDeliveryCount: count(snapshot.pendingDeliveryCount),
    available: snapshot.available,
    observedAt,
  };
}
