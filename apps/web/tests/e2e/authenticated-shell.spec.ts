import { expect, test } from "./fixtures";

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
});
