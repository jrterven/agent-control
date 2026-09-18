import type { WebSocketRoute } from "@playwright/test";
import { expect, test } from "./fixtures";

test.use({ serviceWorkers: "block" });

test("mantiene tareas separadas, conserva borrador manual y recupera resultados al reabrir", async ({ page }) => {
  let socket: WebSocketRoute | undefined;
  let sequence = 0;
  let operation: string | null = null;
  let activeTurnId: string | null = null;
  let prompts = 0;
  let interrupts = 0;
  const createdAt = new Date(Date.now() - 120_000).toISOString();
  let items = [
    { id: "task-research", state: "running", deliveryState: "pending", title: "Tarea delegada", createdAt, updatedAt: createdAt },
    { id: "task-comparison", state: "running", deliveryState: "pending", title: "Tarea delegada", createdAt, updatedAt: createdAt },
  ];
  const messages: Record<string, unknown>[] = [{ id: "initial-answer", role: "assistant", content: "Las dos tareas siguen en curso. Podemos continuar conversando." }];
  const snapshot = () => ({ items, complete: true, available: true, activeCount: items.filter((item) => item.state === "running").length, pendingDeliveryCount: 0, observedAt: new Date().toISOString() });
  const emit = (type: string, data: Record<string, unknown>) => socket!.send(JSON.stringify({ type, data, controlSessionId: "session-e2e", gatewayId: "gateway-e2e", profileName: "default", runtimeSessionId: "runtime-e2e", seq: ++sequence, eventId: `fixture-${sequence}`, occurredAt: new Date().toISOString() }));
  await page.route("**/api/v1/realtime/tickets", (route) => route.fulfill({ json: { ticket: "fixture-ticket", expiresAt: "2099-01-01T00:00:00Z" } }));
  await page.routeWebSocket("**/api/v1/realtime?*", (connection) => { socket = connection; });
  await page.route("**/api/v1/sessions/session-e2e/live-transcripts**", (route) => route.fulfill({ json: { items: [], nextCursor: null } }));
  await page.route("**/api/v1/sessions/session-e2e/background-tasks", (route) => route.fulfill({ json: snapshot() }));
  await page.route("**/api/v1/sessions/session-e2e/messages", (route) => route.fulfill({ json: {
    items: messages, activeTurnId, sessionStatus: activeTurnId || operation ? "streaming" : "ready",
    activeOperation: operation ? { operationId: operation, status: "streaming" } : null,
  } }));
  await page.route("**/api/v1/sessions/session-e2e/prompts", (route) => {
    prompts += 1;
    operation = route.request().headers()["idempotency-key"];
    messages.push({ id: `user-${prompts}`, role: "user", content: route.request().postDataJSON().content });
    return route.fulfill({ json: { operationId: operation, status: "accepted" } });
  });
  await page.route("**/api/v1/sessions/session-e2e/interrupt", (route) => { interrupts += 1; return route.fulfill({ status: 204 }); });
  await page.goto("/chats");
  await expect.poll(() => Boolean(socket)).toBe(true);
  await expect(page.getByText("2 en curso", { exact: true })).toBeVisible();
  await page.locator(".background-tasks summary").click();
  await expect(page.locator(".background-tasks li")).toHaveCount(2);
  const composer = page.getByRole("textbox", { name: "Mensaje a Newton…" });
  await composer.fill("Pregunta independiente");
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect.poll(() => prompts).toBe(1);

  activeTurnId = "public-response";
  emit("message.start", { controlTurn: { correlation: "history", id: "wrapper" } });
  emit("message.start", { controlTurn: { correlation: "history", id: activeTurnId } });
  emit("message.delta", { delta: "Respondo tu pregunta independiente.", controlTurn: { correlation: "history", id: activeTurnId } });
  await expect(page.getByText("Respondo tu pregunta independiente.", { exact: true })).toHaveCount(1);
  await composer.fill("Siguiente pregunta pendiente");
  await composer.press("Enter");
  await expect(page.getByText("Newton está respondiendo; podrás enviar este borrador al terminar el turno.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Detener la respuesta y las tareas de esta conversación" })).toBeVisible();
  expect(prompts).toBe(1);
  expect(interrupts).toBe(0);
  messages.push({ id: "independent-answer", role: "assistant", content: "Respondo tu pregunta independiente." });
  operation = null;
  activeTurnId = null;
  emit("message.completed", { controlTurn: { correlation: "history", id: "public-response" } });
  await expect(page.getByRole("button", { name: "Enviar mensaje" })).toBeEnabled();
  await expect(composer).toHaveValue("Siguiente pregunta pendiente");
  expect(prompts).toBe(1);

  items = [{ ...items[0], state: "completed", deliveryState: "delivered" }, items[1]];
  emit("background.tasks", snapshot());
  activeTurnId = "task-notification";
  emit("message.start", { controlTurn: { correlation: "history", id: activeTurnId } });
  emit("message.delta", { delta: "La investigación terminó. Aquí está el resultado público.", controlTurn: { correlation: "history", id: activeTurnId } });
  messages.push({ id: "research-result", role: "assistant", content: "La investigación terminó. Aquí está el resultado público.", controlTurnOrigin: { kind: "background_task", taskId: "task-research" } });
  activeTurnId = null;
  emit("message.completed", { controlTurn: { correlation: "history", id: "task-notification" } });
  await expect(page.getByText("Respuesta de tarea · task-res", { exact: true })).toBeVisible();
  await expect(page.locator(".background-tasks").getByRole("link", { name: "Ver respuesta" })).toBeVisible();
  await page.locator(".background-tasks").getByRole("link", { name: "Ver respuesta" }).click();
  await expect(page.locator("#task-result-research-result")).toBeFocused();
  await expect(page.getByText("La investigación terminó. Aquí está el resultado público.", { exact: true })).toHaveCount(1);
  await page.screenshot({ path: `test-results/background-tasks-${test.info().project.name}.png` });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.reload();
  await expect(page.getByText("La investigación terminó. Aquí está el resultado público.", { exact: true })).toBeVisible();
  await expect(page.getByText("1 en curso", { exact: true })).toBeVisible();
  await expect(composer).toHaveValue("Siguiente pregunta pendiente");
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect.poll(() => prompts).toBe(2);
  expect(interrupts).toBe(0);
});
