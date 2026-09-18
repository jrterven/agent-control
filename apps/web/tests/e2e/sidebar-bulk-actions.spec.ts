import { bootstrapData, expect, test } from "./fixtures";

const sessions = [
  { ...bootstrapData.sessions[0], title: "Primer chat" },
  { ...bootstrapData.sessions[0], id: "session-second", storedSessionId: "stored-second", title: "Segundo chat" },
  { ...bootstrapData.sessions[0], id: "session-third", storedSessionId: "stored-third", title: "Tercer chat" },
];

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/bootstrap", (route) => route.fulfill({ json: {
    ...bootstrapData,
    profiles: bootstrapData.profiles.map((profile) => ({ ...profile, capabilitySet: { protocol: "dashboard-rest", version: "0.21.2", methods: ["session.delete"], features: [] } })),
    workspaces: [{ ...bootstrapData.workspaces[0], sessionCount: 3 }, { ...bootstrapData.workspaces[0], id: "workspace-destination", name: "Destino", sessionCount: 0 }],
    sessions,
  } }));
  await page.goto("/chats");
  if (page.viewportSize()!.width < 780) await page.getByRole("button", { name: "Abrir navegación" }).click();
  await page.getByRole("button", { name: "Seleccionar conversaciones" }).click();
  await page.getByRole("checkbox", { name: "Seleccionar “Primer chat”" }).check();
  await page.getByRole("checkbox", { name: "Seleccionar “Segundo chat”" }).check();
});

test("mueve únicamente los chats seleccionados y mantiene accesible la selección en móvil", async ({ page }) => {
  const moved: string[] = [];
  await page.route(/\/api\/v1\/sessions\/[^/]+$/, async (route) => {
    const id = route.request().url().split("/").at(-1)!;
    expect(route.request().method()).toBe("PATCH");
    expect(route.request().postDataJSON()).toEqual({ workspaceId: "workspace-destination" });
    moved.push(id);
    await route.fulfill({ json: { ...sessions.find((session) => session.id === id), workspaceId: "workspace-destination" } });
  });
  await expect(page.getByText("2 seleccionadas", { exact: true })).toBeVisible();
  await page.locator(".sidebar-selection").getByRole("button", { name: "Mover", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Mover conversaciones" });
  await dialog.getByRole("combobox").selectOption("workspace-destination");
  await dialog.getByRole("button", { name: "Mover", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(moved.sort()).toEqual(["session-e2e", "session-second"]);
  await expect(page.locator("#left-sidebar")).toBeVisible();
  await page.screenshot({ path: `test-results/sidebar-bulk-move-${test.info().project.name}.png` });
});

test("renueva una sola vez el CSRF desactualizado y mueve las tres conversaciones sin duplicar cambios", async ({ page }) => {
  const attempts: { id: string; csrf: string; key: string; body: unknown }[] = [];
  let refreshes = 0;
  let releaseRefresh!: () => void;
  const staleRequestsReceived = new Promise<void>((resolve) => { releaseRefresh = resolve; });
  await page.route("**/api/v1/auth/me", async (route) => {
    refreshes += 1;
    // Keep the refresh pending until all three concurrent moves have reached
    // the server, reproducing one stale token shared by the entire selection.
    await staleRequestsReceived;
    await route.fulfill({ json: { id: "admin-e2e", name: "Admin E2E", csrfToken: "csrf-refreshed" } });
  });
  await page.route(/\/api\/v1\/sessions\/[^/]+$/, async (route) => {
    const request = route.request();
    const id = request.url().split("/").at(-1)!;
    const csrf = request.headers()["x-csrf-token"];
    expect(request.method()).toBe("PATCH");
    attempts.push({ id, csrf, key: request.headers()["idempotency-key"], body: request.postDataJSON() });
    if (csrf === "csrf-e2e") {
      if (attempts.filter((attempt) => attempt.csrf === "csrf-e2e").length === 3) releaseRefresh();
      await route.fulfill({ status: 403, json: { detail: "Invalid CSRF token" } });
      return;
    }
    expect(csrf).toBe("csrf-refreshed");
    await route.fulfill({ json: { ...sessions.find((session) => session.id === id), workspaceId: "workspace-destination" } });
  });
  await page.getByRole("checkbox", { name: "Seleccionar “Tercer chat”" }).check();
  await page.locator(".sidebar-selection").getByRole("button", { name: "Mover", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Mover conversaciones" });
  await dialog.getByRole("combobox").selectOption("workspace-destination");
  await dialog.getByRole("button", { name: "Mover", exact: true }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText("0 seleccionadas", { exact: true })).toBeVisible();
  expect(refreshes).toBe(1);
  expect(attempts).toHaveLength(6);
  for (const session of sessions) {
    const requests = attempts.filter((attempt) => attempt.id === session.id);
    expect(requests).toHaveLength(2);
    expect(requests.map((request) => request.csrf)).toEqual(["csrf-e2e", "csrf-refreshed"]);
    expect(requests[0].key).toBeTruthy();
    expect(requests[1].key).toBe(requests[0].key);
    expect(requests.map((request) => request.body)).toEqual([
      { workspaceId: "workspace-destination" },
      { workspaceId: "workspace-destination" },
    ]);
  }
  expect(new Set(attempts.map((attempt) => attempt.key)).size).toBe(3);
});

test("confirma la eliminación del lote sin tocar conversaciones no seleccionadas", async ({ page }) => {
  const deleted: string[] = [];
  await page.route(/\/api\/v1\/sessions\/[^/]+$/, async (route) => {
    const id = route.request().url().split("/").at(-1)!;
    expect(route.request().method()).toBe("DELETE");
    expect(route.request().headers()["x-confirm-delete"]).toBe(sessions.find((session) => session.id === id)!.storedSessionId);
    deleted.push(id);
    await route.fulfill({ status: 204 });
  });
  await page.locator(".sidebar-selection").getByRole("button", { name: "Eliminar…", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Eliminar 2 conversaciones" });
  await expect(dialog.getByText("Primer chat", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Segundo chat", { exact: true })).toBeVisible();
  expect(deleted).toEqual([]);
  await dialog.getByRole("button", { name: "Cancelar", exact: true }).click();
  expect(deleted).toEqual([]);
  await page.locator(".sidebar-selection").getByRole("button", { name: "Eliminar…", exact: true }).click();
  await dialog.getByRole("button", { name: "Eliminar de Hermes", exact: true }).click();
  await expect(dialog).toBeHidden();
  expect(deleted.sort()).toEqual(["session-e2e", "session-second"]);
  await expect(page.locator("#left-sidebar").getByText("Tercer chat", { exact: true })).toBeVisible();
});
