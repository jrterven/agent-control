import type { Page } from "@playwright/test";
import { bootstrapData, expect, test } from "./fixtures";

// These selection tests mock document API requests. An activated service
// worker can bypass Playwright routing when the second page reopens.
test.use({ serviceWorkers: "block" });

const otherGateway = { ...bootstrapData.gateways[0], id: "gateway-jarvis-e2e", name: "Equipo de Jarvis" };
const otherProfile = {
  ...bootstrapData.profiles[0],
  id: "profile-jarvis-e2e",
  gatewayId: otherGateway.id,
  // Both computers expose `default`; persistence must identify the actual
  // profile and gateway, not choose whichever technical name appears first.
  technicalName: "default",
  displayName: "Jarvis",
};
const otherSession = {
  ...bootstrapData.sessions[0],
  id: "session-jarvis-e2e",
  gatewayId: otherGateway.id,
  profileId: otherProfile.id,
  title: "Conversación con Jarvis",
  storedSessionId: "stored-jarvis-e2e",
  runtimeSessionId: "runtime-jarvis-e2e",
};

async function selectJarvis(page: Page) {
  await page.goto("/chats");
  await expect(page.locator(".identity-button__name")).toHaveText("Newton");
  await page.locator(".identity-button").click();
  const sidebar = page.locator("#left-sidebar");
  await sidebar.locator(".gateway-select").click();
  await sidebar.getByRole("menuitemradio", { name: /Equipo de Jarvis/ }).click();
  await expect(page.locator(".identity-button__name")).toHaveText("Jarvis");
  await expect(sidebar.locator(".profile-strip .is-active")).toHaveAccessibleName("Jarvis");
}

test.describe("último agente usado", () => {
  for (const removeRememberedProfile of [false, true]) {
    test(removeRememberedProfile
      ? "elige un agente disponible si el recordado se revocó mientras la app estaba cerrada"
      : "restaura el agente y su equipo al cerrar y volver a abrir la app", async ({ page, context }) => {
      let profileAvailable = true;
      await context.route("**/api/v1/bootstrap", (route) => route.fulfill({ json: {
        ...bootstrapData,
        gateways: [...bootstrapData.gateways, otherGateway],
        profiles: profileAvailable ? [...bootstrapData.profiles, otherProfile] : bootstrapData.profiles,
        sessions: profileAvailable ? [...bootstrapData.sessions, otherSession] : bootstrapData.sessions,
      } }));
      await context.route("**/api/v1/sessions/session-jarvis-e2e/messages", (route) => route.fulfill({ json: { items: [] } }));

      await selectJarvis(page);
      await page.close();
      profileAvailable = !removeRememberedProfile;

      // A new page has no in-memory Zustand state or PWA-update return context.
      // Only persistent browser storage survives, as when reopening the PWA.
      const reopened = await context.newPage();
      await reopened.goto("/chats");
      const expectedAgent = removeRememberedProfile ? "Newton" : "Jarvis";
      await expect(reopened.locator(".identity-button__name")).toHaveText(expectedAgent);
      await expect(reopened.getByRole("textbox", { name: `Mensaje a ${expectedAgent}…` })).toBeVisible();
      await reopened.locator(".identity-button").click();
      const sidebar = reopened.locator("#left-sidebar");
      await expect(sidebar.locator(".gateway-select")).toContainText(removeRememberedProfile ? "Gateway E2E" : "Equipo de Jarvis");
      await expect(sidebar.locator(".profile-strip .is-active")).toHaveAccessibleName(expectedAgent);
      await reopened.close();
    });
  }
});
