import type { WebSocketRoute } from "@playwright/test";
import { expect, test } from "./fixtures";

test.use({ serviceWorkers: "block" });

test("no revive respuestas detenidas al recuperar el historial durante otra tarea", async ({ page }) => {
  let socket: WebSocketRoute | undefined;
  let operation: string | null = null;
  let previousOperation = "";
  let prompts = 0;
  let reads = 0;
  let sequence = 0;
  const messages: Record<string, unknown>[] = [];
  const emit = (type: string, correlationId?: string, data: Record<string, unknown> = {}) => socket!.send(JSON.stringify({
    type, correlationId, data, controlSessionId: "session-e2e", gatewayId: "gateway-e2e", profileName: "default",
    eventId: `history-fixture-${++sequence}`, seq: sequence, occurredAt: new Date().toISOString(),
  }));
  await page.route("**/api/v1/realtime/tickets", (route) => route.fulfill({ json: { ticket: "fixture-ticket", expiresAt: "2099-01-01T00:00:00Z" } }));
  await page.routeWebSocket("**/api/v1/realtime?*", (connection) => { socket = connection; });
  await page.route("**/api/v1/sessions/session-e2e/messages", (route) => {
    reads += 1;
    return route.fulfill({ json: {
      items: messages, sessionStatus: operation ? "streaming" : "ready",
      activeOperation: operation ? { operationId: operation, status: "streaming" } : null,
    } });
  });
  await page.route("**/api/v1/sessions/session-e2e/prompts", (route) => {
    prompts += 1;
    operation = route.request().headers()["idempotency-key"];
    messages.push({ id: `user-${prompts}`, role: "user", content: route.request().postDataJSON().content });
    return route.fulfill({ json: { operationId: operation, status: "accepted" } });
  });
  await page.route("**/api/v1/sessions/session-e2e/interrupt", (route) => {
    previousOperation = operation!;
    operation = null;
    return route.fulfill({ status: 204 });
  });
  await page.goto("/chats");
  await expect.poll(() => Boolean(socket)).toBe(true);
  const composer = page.getByRole("textbox", { name: "Mensaje a Newton…" });
  await composer.fill("Primera tarea");
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect.poll(() => prompts).toBe(1);
  await page.getByRole("button", { name: "Detener", exact: true }).click();
  await expect(page.getByText("Ejecución detenida.", { exact: true })).toBeVisible();
  await composer.fill("Segunda tarea");
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect.poll(() => prompts).toBe(2);

  messages.push({ id: "tool-current", role: "tool", tool_name: "search", content: "Consulta actual" });
  const beforeRefresh = reads;
  emit("control.stream.overflow");
  await expect.poll(() => reads).toBeGreaterThan(beforeRefresh);
  await expect(page.getByText("Ejecución detenida.", { exact: true })).toHaveCount(0);
  await expect(page.getByText("Segunda tarea", { exact: true })).toHaveCount(1);
  emit("message.delta", previousOperation, { delta: "Contenido anterior que llega tarde" });
  emit("message.completed", previousOperation);
  await expect(page.getByRole("button", { name: "Detener", exact: true })).toBeVisible();
  await expect(page.getByText("Contenido anterior que llega tarde", { exact: true })).toHaveCount(0);

  messages.push({ id: "answer-current", role: "assistant", content: "Resultado de la segunda tarea" });
  const completedOperation = operation!;
  operation = null;
  emit("message.completed", completedOperation);
  await expect(page.getByRole("button", { name: "Enviar mensaje" })).toBeVisible();
  await expect(page.getByText("Resultado de la segunda tarea", { exact: true })).toHaveCount(1);
  await page.reload();
  await expect(page.getByText("Resultado de la segunda tarea", { exact: true })).toHaveCount(1);
  await expect(page.getByText("Ejecución detenida.", { exact: true })).toHaveCount(0);
});
