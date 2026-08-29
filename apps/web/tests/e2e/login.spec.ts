import { expect, test } from "@playwright/test";
import { installMockApi } from "./fixtures";

test("autentica al administrador sin persistir la contraseña en almacenamiento web", async ({ context, page }) => {
  await installMockApi(context, { authenticated: false });
  await page.goto("/login");

  await expect(page.getByRole("heading", { name: "Tus agentes. Un solo lugar." })).toBeVisible();
  await expect(page.getByLabel("Usuario")).toHaveValue("");
  await page.getByLabel("Usuario").fill("admin");
  await page.getByLabel("Contraseña").fill("correcta-e2e-segura");
  await page.getByRole("button", { name: "Entrar a Agent Control" }).click();

  await expect(page).toHaveURL(/\/chats$/);
  await expect(page.getByRole("button", { name: "Operación móvil", exact: true })).toBeVisible();
  const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(storage).not.toContain("correcta-e2e-segura");
  expect(storage).not.toContain("csrf-e2e");
});
