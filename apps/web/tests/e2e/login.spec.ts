import { expect, test } from "@playwright/test";
import { installMockApi, seedLanguagePreference } from "./fixtures";

test("una sesión expirada restaurada desde chats siempre muestra el acceso", async ({ context, page, baseURL }) => {
  await seedLanguagePreference(context, baseURL, "es");
  await installMockApi(context, { authenticated: false });

  await page.goto("/chats", { waitUntil: "domcontentloaded" });

  await expect(page).toHaveURL(/\/login$/);
  await expect(page.getByRole("heading", { name: "Tus agentes. Un solo lugar." })).toBeVisible();
  await expect(page.locator("#root > *")).not.toHaveCount(0);
  await expect(page.locator("[data-agent-control-boot]")).toBeHidden();
});

test("autentica al administrador sin persistir la contraseña en almacenamiento web", async ({ context, page, baseURL }) => {
  await seedLanguagePreference(context, baseURL, "es");
  await installMockApi(context, { authenticated: false });
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: "Tus agentes. Un solo lugar." })).toBeVisible();
  await expect(page.getByLabel("Usuario")).toHaveValue("");
  await page.getByLabel("Usuario").fill("admin");
  await page.getByLabel("Contraseña").fill("correcta-e2e-segura");
  await page.getByRole("button", { name: "Entrar a Agent Control" }).click();

  await expect(page).toHaveURL(/\/chats$/);
  await expect(page.getByRole("button", { name: "Elegir espacio de trabajo: Operación móvil", exact: true })).toBeVisible();
  const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(storage).not.toContain("correcta-e2e-segura");
  expect(storage).not.toContain("csrf-e2e");
});

for (const locale of ["es-MX", "fr-FR"]) {
  test.describe(`first visit with ${locale} browser preferences`, () => {
    test.use({ locale, serviceWorkers: "block" });

    test("opens cloud login in English without a saved language", async ({ context, page }, testInfo) => {
      await installMockApi(context, { authenticated: false });
      await page.route("**/api/v1/auth/methods", (route) => route.fulfill({ json: {
        mode: "cloud", googleEnabled: true, registrationMode: "open", betaMaxUsers: 20,
      } }));
      await page.goto("/login");
      expect(await page.evaluate(() => navigator.language)).toBe(locale);
      await expect(page.locator("html")).toHaveAttribute("lang", "en");
      await expect(page.getByRole("heading", { name: "Your agents, wherever you are" })).toBeVisible();
      await expect(page.getByRole("link", { name: "Continue with Google" })).toBeVisible();
      await page.reload();
      await expect(page.locator("html")).toHaveAttribute("lang", "en");
      await expect(page.getByRole("link", { name: "Continue with Google" })).toBeVisible();
      await page.screenshot({ path: testInfo.outputPath(`login-english-${locale}.png`), fullPage: true });
    });
  });
}

test.describe("saved language preference", () => {
  test.use({ locale: "fr-FR", serviceWorkers: "block" });

  test("preserves saved Spanish on cloud login and reload", async ({ context, page, baseURL }, testInfo) => {
    await seedLanguagePreference(context, baseURL, "es");
    await installMockApi(context, { authenticated: false });
    await page.route("**/api/v1/auth/methods", (route) => route.fulfill({ json: {
      mode: "cloud", googleEnabled: true, registrationMode: "open", betaMaxUsers: 20,
    } }));
    await page.goto("/login");
    expect(await page.evaluate(() => navigator.language)).toBe("fr-FR");
    for (let visit = 0; visit < 2; visit++) {
      await expect(page.locator("html")).toHaveAttribute("lang", "es");
      await expect(page.getByRole("heading", { name: "Tus agentes, donde estés" })).toBeVisible();
      await expect(page.getByRole("link", { name: "Continuar con Google" })).toBeVisible();
      if (visit === 0) await page.reload();
    }
    await page.screenshot({ path: testInfo.outputPath("login-saved-spanish.png"), fullPage: true });
  });
});
