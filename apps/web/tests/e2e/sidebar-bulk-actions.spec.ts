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
