import { bootstrapData, expect, test } from "./fixtures";

test.describe("shell responsive con estado autenticado determinista", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/chats");
    await expect(page.getByRole("region", { name: "Prueba de reconexión" })).toBeVisible();
  });

  test("mantiene el chat como foco y adapta navegación y contexto", async ({ page }) => {
    const viewport = page.viewportSize();
    if (!viewport) throw new Error("El proyecto Playwright requiere un viewport explícito");

    await expect(page.getByText("La sesión está aislada y lista para continuar.")).toBeVisible();
    await expect(page.getByText("Newton", { exact: true }).first()).toBeVisible();

    const sidebar = page.locator("#left-sidebar");
    const activity = page.locator("#activity-panel");
    const bottomNav = page.getByRole("navigation", { name: "Navegación principal" });

    if (viewport.width < 780) {
      await expect(bottomNav).toBeVisible();
      await expect(sidebar).toBeHidden();
      await page.getByRole("button", { name: "Abrir navegación" }).click();
      await expect(sidebar).toBeVisible();
      await expect(sidebar.getByText("Gateway E2E")).toBeVisible();
      await expect(sidebar.getByText("Operación móvil")).toBeVisible();
      await sidebar.getByRole("button", { name: "Cerrar navegación" }).click();

      await expect(activity).toBeHidden();
      await page.getByRole("button", { name: "Abrir actividad y contexto" }).click();
      await expect(activity).toBeVisible();
      await expect(activity.getByRole("heading", { name: "Detalles de sesión" })).toBeVisible();
      const box = await activity.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.width).toBeGreaterThanOrEqual(viewport.width - 2);
      await expect.poll(async () => {
        const settledBox = await activity.boundingBox();
        return settledBox ? Math.abs(settledBox.y + settledBox.height - viewport.height) : Number.POSITIVE_INFINITY;
      }).toBeLessThanOrEqual(3);

      const touchTargets = await bottomNav.locator("a").evaluateAll((links) => links.map((link) => link.getBoundingClientRect().height));
      expect(touchTargets.every((height) => height >= 44)).toBe(true);
      return;
    }

    await expect(sidebar).toBeVisible();
    await expect(bottomNav).toBeHidden();
    await expect(sidebar.getByText("Gateway E2E")).toBeVisible();

    if (viewport.width < 1200) {
      await expect(activity).toBeHidden();
      await page.getByRole("button", { name: "Mostrar contexto" }).click();
      await expect(activity).toBeVisible();
      const box = await activity.boundingBox();
      expect(box).not.toBeNull();
      expect(box!.width).toBeLessThanOrEqual(361);
      expect(box!.x + box!.width).toBeGreaterThanOrEqual(viewport.width - 2);
    } else {
      await expect(activity).toBeVisible();
      await expect(activity.getByRole("heading", { name: "Detalles de sesión" })).toBeVisible();
      const columns = await page.locator(".app-shell").evaluate((element) => getComputedStyle(element).gridTemplateColumns);
      expect(columns.split(" ")).toHaveLength(3);
    }
  });

  test("limita el menú del equipo al selector y permite cerrarlo fuera y con Escape", async ({ page }) => {
    const sidebar = page.locator("#left-sidebar");
    if (page.viewportSize()!.width < 780) await page.getByRole("button", { name: "Abrir navegación" }).click();
    const trigger = sidebar.locator(".gateway-select");
    await trigger.click();
    const menu = sidebar.getByRole("menu");
    await expect(menu).toBeVisible();
    const selectorBox = (await trigger.boundingBox())!;
    const menuBox = (await menu.boundingBox())!;
    expect(Math.abs(menuBox.x - selectorBox.x)).toBeLessThanOrEqual(1);
    expect(Math.abs(menuBox.width - selectorBox.width)).toBeLessThanOrEqual(1);
    expect(menuBox.y).toBeGreaterThanOrEqual(selectorBox.y + selectorBox.height);
    expect(menuBox.x + menuBox.width).toBeLessThan(page.viewportSize()!.width);
    await page.screenshot({ path: `test-results/equipment-menu-${test.info().project.name}.png` });
    await sidebar.locator(".sidebar-brand").click();
    await expect(menu).toBeHidden();
    await trigger.click();
    await page.keyboard.press("Escape");
    await expect(menu).toBeHidden();
    await expect(trigger).toBeFocused();
    await expect(sidebar).toBeVisible();
  });

  test("oculta la barra lateral, amplía el chat y conserva la preferencia al recargar", async ({ page }) => {
    test.skip(page.viewportSize()!.width < 780, "Control de navegación acoplada");
    const sidebar = page.locator("#left-sidebar");
    const main = page.locator(".app-center");
    const initialWidth = (await main.boundingBox())!.width;
    await page.getByRole("button", { name: "Ocultar barra lateral" }).click();
    await expect(sidebar).toBeHidden();
    await expect.poll(async () => (await main.boundingBox())!.width).toBeGreaterThan(initialWidth + 250);
    await page.reload();
    await expect(sidebar).toBeHidden();
    await page.getByRole("button", { name: "Mostrar barra lateral" }).click();
    await expect(sidebar).toBeVisible();
    await expect.poll(async () => (await main.boundingBox())!.width).toBeLessThan(initialWidth + 2);

    if (page.viewportSize()!.width >= 1200) {
      await page.getByRole("button", { name: "Ocultar contexto" }).click();
      await page.getByRole("button", { name: "Ocultar barra lateral" }).click();
      await expect(sidebar).toBeHidden();
      await expect(page.locator("#activity-panel")).toBeHidden();
      await expect.poll(async () => (await main.boundingBox())!.width).toBe(page.viewportSize()!.width);
    }

    await page.setViewportSize({ width: 390, height: 844 });
    await page.getByRole("button", { name: "Abrir navegación" }).click();
    await expect(sidebar).toBeVisible();
    await sidebar.getByRole("button", { name: "Cerrar navegación" }).click();
    await expect(sidebar).toBeHidden();
  });

  test("mantiene separados los controles en el ancho mínimo de tablet", async ({ page }) => {
    test.skip(page.viewportSize()!.width < 780, "Controles de tablet");
    await page.setViewportSize({ width: 780, height: 900 });
    await page.getByRole("button", { name: "Elegir espacio de trabajo: Operación móvil" }).click();
    await page.getByRole("menuitemradio", { name: /Sin espacio de trabajo/ }).click();
    const identity = (await page.locator(".identity-button").boundingBox())!;
    const workspace = (await page.locator(".workspace-switcher-wrap").boundingBox())!;
    expect(identity.x + identity.width).toBeLessThanOrEqual(workspace.x);
    await expect(page.getByRole("button", { name: "Ocultar barra lateral" })).toBeVisible();
    await page.screenshot({ path: `test-results/sidebar-tablet-minimum-${test.info().project.name}.png` });
  });

  test("abre un chat vacío desde Ajustes con el mismo agente y espacio de trabajo", async ({ page }) => {
    let createCount = 0;
    await page.route("**/api/v1/sessions", async (route) => {
      expect(route.request().method()).toBe("POST");
      expect(route.request().postDataJSON()).toEqual({ profileId: "profile-newton-e2e", workspaceId: "workspace-e2e" });
      createCount += 1;
      await route.fulfill({ json: { ...bootstrapData.sessions[0], id: "session-new-e2e", storedSessionId: "stored-new", title: "Nueva conversación", status: "ready" } });
    });
    await page.route("**/api/v1/sessions/session-new-e2e/messages", (route) => route.fulfill({ json: { items: [] } }));
    await page.goto("/settings");
    if (page.viewportSize()!.width < 780) await page.getByRole("button", { name: "Abrir navegación" }).click();
    await page.locator("#left-sidebar").getByRole("button", { name: "Nuevo chat", exact: true }).click();
    await expect(page).toHaveURL(/\/chats$/);
    await expect(page.getByRole("heading", { name: "Inicia una conversación con Newton" })).toBeVisible();
    await expect(page.getByRole("textbox", { name: "Mensaje a Newton…" })).toBeEmpty();
    await expect(page.getByText("La sesión está aislada y lista para continuar.")).toHaveCount(0);
    expect(createCount).toBe(1);
  });
});
