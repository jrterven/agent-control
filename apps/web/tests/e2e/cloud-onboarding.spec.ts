import type { WebSocketRoute } from "@playwright/test";
import { bootstrapData, expect, test } from "./fixtures";

const pairing = { code: "ABCD-EFGH", name: "Mac de prueba", profiles: ["default", "research"], expiresAt: new Date(Date.now() + 600_000).toISOString() };
const computer = { id: "computer-e2e", name: "Mac de prueba", profiles: ["default"], status: "offline", version: "0.1.0", lastSeenAt: null };

test.beforeEach(async ({ page }) => {
  await page.route("**/api/v1/auth/methods", (route) => route.fulfill({ json: { mode: "cloud", googleEnabled: true } }));
  await page.route("**/api/v1/bootstrap", (route) => route.fulfill({ json: { gateways: [], profiles: [], sessions: [], workspaces: [], automations: [] } }));
  await page.route("**/api/v1/connectors", (route) => route.fulfill({ json: { items: [computer], installCommand: "curl --proto '=https' --tlsv1.2 -fsSL https://control.example/connector/install.sh | sh -s -- --server https://control.example" } }));
  await page.route("**/api/v1/connectors/pair/inspect", (route) => route.fulfill({ json: pairing }));
  await page.route("**/downloads/agent-control/latest.json", (route) => route.fulfill({ status: 404, json: {} }));
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

test("ofrece instaladores publicados y conserva la opción de conectar Hermes existente", async ({ page }) => {
  await page.route("**/downloads/agent-control/latest.json", (route) => route.fulfill({ json: {
    schemaVersion: 1, version: "a".repeat(40), downloads: {
      macosArm64: { url: "/downloads/agent-control/releases/release/Agent-Control.dmg", sha256: "b".repeat(64), minOsVersion: "13" },
      linux: { installerUrl: "/downloads/agent-control/install.sh" },
    },
  } }));
  await page.goto("/connect");
  await expect(page.getByRole("radio", { name: /Instalar Agent Control con Hermes/ })).toBeChecked();
  await expect(page.getByRole("link", { name: "Descargar para Mac (.dmg)" })).toHaveAttribute("href", /\/downloads\/agent-control\/releases\/release\/Agent-Control.dmg$/);
  await expect(page.locator(".connector-command")).toContainText("/downloads/agent-control/install.sh");
  await expect(page.getByText(/Google te identifica en Agent Control/)).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: `test-results/managed-onboarding-${test.info().project.name}.png`, fullPage: true });
  await page.getByRole("link", { name: "Descargar para Mac (.dmg)" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: `test-results/managed-downloads-${test.info().project.name}.png` });
  await page.getByRole("radio", { name: /Conectar mi Hermes existente/ }).check();
  await expect(page.locator(".connector-command")).toContainText("https://control.example/connector/install.sh");
});

test("oculta descargas pendientes cuando no hay una publicación disponible", async ({ page }) => {
  await page.goto("/connect");
  await expect(page.getByText("Este instalador aún no está publicado. Vuelve a comprobar su disponibilidad más tarde.")).toHaveCount(2);
  await expect(page.getByRole("link", { name: "Descargar para Mac (.dmg)" })).toHaveCount(0);
  await expect(page.locator(".connector-command")).toHaveCount(0);
});

test("espera al equipo y abre el primer chat solo con una acción explícita", async ({ page }) => {
  let online = false;
  let created = 0;
  let prompts = 0;
  let socket: WebSocketRoute | undefined;
  const gatewayId = bootstrapData.gateways[0].id;
  const linked = { ...computer, gatewayId };
  await page.route("**/api/v1/realtime/tickets", (route) => route.fulfill({ json: { ticket: "readiness-test" } }));
  await page.routeWebSocket("**/api/v1/realtime?*", (connection) => { socket = connection; });
  await page.route("**/api/v1/connectors", (route) => route.fulfill({ json: { items: [{ ...linked, status: online ? "online" : "offline" }], installCommand: "" } }));
  await page.route("**/api/v1/bootstrap", (route) => route.fulfill({ json: online ? { ...bootstrapData, sessions: [], workspaces: [] } : { gateways: [], profiles: [], sessions: [], workspaces: [], automations: [] } }));
  await page.route("**/api/v1/connectors/pair/approve", (route) => route.fulfill({ json: linked }));
  await page.route("**/api/v1/sessions", (route) => { created += 1; return route.fulfill({ json: { ...bootstrapData.sessions[0], id: "first-chat", workspaceId: null, storedSessionId: "stored-first", title: "Nueva conversación", status: "ready" } }); });
  await page.route("**/api/v1/sessions/first-chat/messages", (route) => route.fulfill({ json: { items: [] } }));
  await page.route("**/api/v1/sessions/*/prompts*", (route) => { prompts += 1; return route.fulfill({ status: 503, json: {} }); });
  await page.goto("/connect?code=ABCD-EFGH");
  await page.getByRole("button", { name: "Revisar equipo" }).click();
  await page.getByRole("button", { name: "Conectar perfiles seleccionados" }).click();
  const readiness = page.locator(".connector-readiness").getByRole("status");
  await expect(readiness).toHaveText("Equipo vinculado. Esperando que el conector se conecte…");
  expect(created).toBe(0);
  online = true;
  await expect(readiness).toHaveText("Tu equipo está listo para conversar.");
  expect(created).toBe(0);
  expect(socket).toBeDefined();
  socket!.send(JSON.stringify({ type: "control.connection", gatewayId, profileName: "default", data: { state: "connected" } }));
  await expect(page.getByRole("button", { name: "Newton Conectado", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Abrir un chat nuevo" }).click();
  await expect(page).toHaveURL(/\/chats$/);
  await expect(page.getByRole("textbox", { name: "Mensaje a Newton…" })).toBeEmpty();
  expect(created).toBe(1);
  expect(prompts).toBe(0);
});
