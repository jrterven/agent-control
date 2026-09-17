import { expect, test } from "./fixtures";

test("invita a instalar sin robar el foco y solicita la instalación solo al pulsar", async ({ page }) => {
  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Ajustes", exact: true })).toBeVisible();
  await page.evaluate(() => {
    const event = new Event("beforeinstallprompt", { cancelable: true });
    Object.assign(event, { prompt: async () => {
      document.body.dataset.installCalls = String(Number(document.body.dataset.installCalls ?? "0") + 1);
      return { outcome: "dismissed" };
    } });
    window.dispatchEvent(event);
  });
  const invitation = page.getByRole("complementary", { name: "Lleva Agent Control contigo" });
  await expect(invitation).toBeVisible({ timeout: 12_000 });
  expect(await page.evaluate(() => document.body.dataset.installCalls)).toBeUndefined();
  expect(await invitation.evaluate((element) => element.contains(document.activeElement))).toBe(false);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: `test-results/pwa-install-${test.info().project.name}.png` });
  await invitation.getByRole("button", { name: "Instalar app", exact: true }).click();
  await expect(invitation).toHaveCount(0);
  expect(await page.evaluate(() => document.body.dataset.installCalls)).toBe("1");
});

test("en iOS ofrece instrucciones accesibles y permite cerrar con Escape", async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "userAgent", { configurable: true, value: "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1" });
    Object.defineProperty(navigator, "standalone", { configurable: true, value: false });
  });
  await page.goto("/settings");
  await page.getByRole("button", { name: "Cómo instalar", exact: true }).click({ timeout: 12_000 });
  const instructions = page.getByRole("dialog", { name: "Instalar en iPhone o iPad" });
  await expect(instructions).toBeVisible();
  await expect(instructions).toContainText("Añadir a pantalla de inicio");
  await expect(instructions.getByRole("button", { name: "Entendido" })).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: `test-results/pwa-install-ios-${test.info().project.name}.png` });
  await page.keyboard.press("Escape");
  await expect(instructions).toHaveCount(0);
  await expect(page.getByRole("complementary", { name: "Lleva Agent Control contigo" })).toHaveCount(0);
});
