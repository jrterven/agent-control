import { expect, test } from "./fixtures";

const pairing = { code: "ABCD-EFGH", name: "Mac de prueba", profiles: ["default", "research"], expiresAt: new Date(Date.now() + 600_000).toISOString() };
const computer = { id: "computer-e2e", name: "Mac de prueba", profiles: ["default"], status: "offline", version: "0.1.0", lastSeenAt: null };

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/auth/methods", (route) => route.fulfill({ json: { mode: "cloud", googleEnabled: true } }));
  await page.route("**/api/v1/bootstrap", (route) => route.fulfill({ json: { gateways: [], profiles: [], sessions: [], workspaces: [], automations: [] } }));
  await page.route("**/api/v1/connectors", (route) => route.fulfill({ json: { items: [computer], installCommand: "curl --proto '=https' --tlsv1.2 -fsSL https://control.example/connector/install.sh | sh -s -- --server https://control.example" } }));
  await page.route("**/api/v1/connectors/pair/inspect", (route) => route.fulfill({ json: pairing }));
});

test("vincula perfiles revisados desde una pantalla móvil sin desbordamientos", async ({ page }) => {
  let approved: unknown;
  await page.route("**/api/v1/connectors/pair/approve", async (route) => {
    approved = route.request().postDataJSON();
    expect(route.request().headers()["x-csrf-token"]).toBe("csrf-e2e");
    expect(route.request().headers()["idempotency-key"]).toBeTruthy();
    await route.fulfill({ json: computer });
  });
  await page.goto("/connect?code=ABCD-EFGH");
  await expect(page.getByRole("heading", { name: "Conectar un equipo", exact: true })).toBeVisible();
  await expect(page.getByText(/tus credenciales de Hermes permanecen/i)).toBeVisible();
  await expect(page.getByLabel("Código de vinculación")).toHaveValue("ABCD-EFGH");
  await page.getByRole("button", { name: "Revisar equipo" }).click();
  await expect(page.getByRole("heading", { name: "Mac de prueba" })).toBeVisible();
  await page.getByRole("checkbox", { name: "research", exact: true }).uncheck();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: `test-results/cloud-pairing-${test.info().project.name}.png`, fullPage: true });
  await page.getByRole("button", { name: "Conectar perfiles seleccionados" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Equipo vinculado" })).toBeVisible();
  expect(approved).toEqual({ code: "ABCD-EFGH", profiles: ["default"] });
});

test("conserva el código al redirigir al acceso de Google", async ({ page }) => {
  await page.route("**/api/v1/auth/me", (route) => route.fulfill({ status: 401, json: { detail: "Not authenticated" } }));
  await page.goto("/connect?code=ABCD-EFGH");
  await expect(page.getByRole("link", { name: "Continuar con Google" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Continuar con Google" })).toHaveAttribute("href", "/api/v1/auth/google/start?returnTo=%2Fconnect%3Fcode%3DABCD-EFGH");
  await expect(page.getByLabel("Contraseña")).toHaveCount(0);
});

test("muestra el estado del equipo y confirma revocar el acceso", async ({ page }) => {
  let revoked = false;
  await page.route("**/api/v1/connectors/computer-e2e", async (route) => {
    revoked = true;
    await route.fulfill({ status: 204 });
  });
  await page.goto("/computers");
  await expect(page.getByRole("heading", { name: "Mis equipos" })).toBeVisible();
  await expect(page.getByText("Aún no se ha conectado")).toBeVisible();
  await page.getByRole("button", { name: "Revocar acceso", exact: true }).click();
  expect(revoked).toBe(false);
  await page.getByRole("button", { name: "Sí, revocar acceso", exact: true }).click();
  await expect.poll(() => revoked).toBe(true);
});

test("ofrece conectar un equipo al abrir conversaciones sin agentes", async ({ page }) => {
  await page.goto("/chats");
  await expect(page.getByRole("heading", { name: "Conecta tu primer equipo" })).toBeVisible();
  await expect(page.getByRole("link", { name: "Conectar un equipo", exact: true })).toHaveAttribute("href", "/connect");
});
