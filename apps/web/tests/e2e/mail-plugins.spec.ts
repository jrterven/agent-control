import { expect, test, bootstrapData } from "./fixtures";
import type { MailAccount } from "../../src/lib/mail";

test("connects multiple mailboxes, preserves grants on reconnect and revokes access", async ({ page }, info) => {
  let accounts: MailAccount[] = [];
  const passwords: string[] = [];
  let tests = 0;
  await page.route("**/api/v1/mail/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path.endsWith("/providers")) return route.fulfill({ json: ["gmail", "outlook", "hostinger", "imap"].map((id) => ({ id, enabled: id !== "gmail" && id !== "outlook" })) });
    if (path.endsWith("/accounts") && request.method() === "GET") return route.fulfill({ json: accounts });
    expect(request.headers()["x-csrf-token"]).toBe("csrf-e2e");
    if (request.method() === "POST" && path.endsWith("/accounts")) {
      const input = request.postDataJSON(); passwords.push(input.password);
      const existing = accounts.find((a) => a.address === input.address);
      const account = existing ?? { id: `mail-${accounts.length}`, provider: input.provider, address: input.address, label: input.label, config: { service: input.service, username: input.username }, status: "connected", agents: [] };
      if (!existing) accounts.push(account);
      return route.fulfill({ json: account });
    }
    const id = path.split("/")[5];
    const account = accounts.find((a) => a.id === id)!;
    if (request.method() === "PATCH") {
      const input = request.postDataJSON(); account.label = input.label;
      account.agents = input.profileIds.map((profileId: string) => ({ profileId, state: "ready" }));
    }
    if (path.endsWith("/test")) tests++;
    if (request.method() === "DELETE") { accounts = accounts.filter((a) => a.id !== id); return route.fulfill({ status: 204 }); }
    return route.fulfill({ json: account });
  });
  await page.goto("/settings#plugins");
  const panel = page.locator("#plugins");
  await expect(panel.getByRole("button", { name: "Conectar Hostinger" })).toBeEnabled();
  await expect(panel.getByRole("button", { name: "Conectar Gmail" })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Conectar Outlook" })).toBeDisabled();
  await expect(panel.getByRole("button", { name: "Conectar Otro correo" })).toBeEnabled();
  for (const address of ["work@example.com", "personal@example.com"]) {
    await panel.getByRole("button", { name: "Conectar Hostinger" }).click();
    await panel.getByLabel("Dirección de correo", { exact: true }).fill(address);
    await panel.getByLabel("Contraseña o contraseña de aplicación").fill("temporary-test-password");
    await panel.getByRole("checkbox", { name: /Newton/ }).check();
    await panel.getByRole("button", { name: "Guardar", exact: true }).click();
    await expect(panel.getByRole("article", { name: address })).toBeVisible();
  }
  expect(accounts).toHaveLength(2);
  const first = panel.getByRole("article", { name: "work@example.com" });
  await first.getByRole("button", { name: "Reconectar", exact: true }).click();
  await expect(panel.getByLabel("Contraseña o contraseña de aplicación")).toHaveValue("");
  await expect(panel.getByRole("checkbox", { name: /Newton/ })).toBeChecked();
  await panel.getByLabel("Contraseña o contraseña de aplicación").fill("new-test-password");
  await panel.getByRole("button", { name: "Guardar", exact: true }).click();
  await expect(panel.getByRole("article")).toHaveCount(2);
  await first.getByRole("button", { name: "Probar conexión" }).click();
  await expect(panel.getByRole("status")).toHaveText("Conexión verificada.");
  expect(tests).toBe(1);
  await panel.locator("header").scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("plugins.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await first.getByRole("button", { name: "Desconectar", exact: true }).click();
  await first.getByRole("group", { name: "Desconectar" }).getByRole("button", { name: "Desconectar" }).click();
  await expect(panel.getByRole("article")).toHaveCount(1);
  expect(passwords).toHaveLength(3);
  expect(accounts[0].agents[0].profileId).toBe(bootstrapData.profiles[0].id);
});

test("returns from OAuth to Plugins and clears credentials when going offline", async ({ page }) => {
  await page.route("**/api/v1/mail/**", (route) => route.fulfill({ json: route.request().url().endsWith("providers") ? [{ id: "hostinger", enabled: true }] : [] }));
  await page.goto("/settings?mailResult=connected#plugins");
  const panel = page.locator("#plugins");
  await expect(panel.getByRole("status").filter({ hasText: "Conexión guardada" })).toHaveText("Conexión guardada. Elige los agentes que podrán utilizarla.");
  await expect(page).toHaveURL(/\/settings#plugins$/);
  await panel.getByRole("button", { name: "Conectar Hostinger" }).click();
  await panel.getByLabel("Contraseña o contraseña de aplicación").fill("never-retain-me");
  await page.context().setOffline(true);
  await expect(panel.locator('input[type="password"]')).toHaveCount(0);
  await expect(panel.getByRole("button", { name: "Conectar Hostinger" })).toBeDisabled();
  await page.context().setOffline(false);
  await panel.getByRole("button", { name: "Conectar Hostinger" }).click();
  await expect(panel.getByLabel("Contraseña o contraseña de aplicación")).toHaveValue("");
});
