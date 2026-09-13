import { bootstrapData, expect, test } from "./fixtures";

test("saves and resets independent Live voices without starting audio", async ({ page }, testInfo) => {
  const overrides = new Map<string, string>();
  const writes: string[] = [];
  const voiceStarts: string[] = [];
  await page.route("**/api/v1/bootstrap", (route) => route.fulfill({ json: {
    ...bootstrapData,
    profiles: [...bootstrapData.profiles, { ...bootstrapData.profiles[0], id: "profile-jarvis-e2e", displayName: "Jarvis", technicalName: "jarvis" }],
  } }));
  await page.route("**/api/v1/integrations/**", (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/integrations/voice") return route.fulfill({ json: { provider: "elevenlabs" } });
    if (path === "/api/v1/integrations/openai") return route.fulfill({ json: { configured: false, provider: "openai", modelId: "gpt-live-1" } });
    if (path === "/api/v1/integrations/elevenlabs") return route.fulfill({ json: { configured: false, provider: "elevenlabs", modelId: "scribe_v2_realtime" } });
    if (path === "/api/v1/integrations/openai/voice") return route.fulfill({ json: { voiceId: "stone" } });
    const match = path.match(/^\/api\/v1\/integrations\/openai\/profiles\/([^/]+)\/voice$/);
    if (!match) return route.fallback();
    const profileId = match[1];
    if (request.method() !== "GET") {
      expect(request.headers()["x-csrf-token"]).toBe("csrf-e2e");
      expect(request.headers()["idempotency-key"]).toBeTruthy();
      writes.push(`${request.method()}:${profileId}`);
      if (request.method() === "PUT") overrides.set(profileId, request.postDataJSON().voiceId);
      if (request.method() === "DELETE") overrides.delete(profileId);
    }
    return route.fulfill({ json: { profileId, voiceId: overrides.get(profileId) ?? "stone", inherited: !overrides.has(profileId) } });
  });
  page.on("request", (request) => {
    if (/live-session|live-voice-preview/.test(request.url())) voiceStarts.push(request.url());
  });
  await page.goto("/settings");
  const scope = page.getByRole("combobox", { name: "Configurar voz de" });
  await scope.selectOption("profile-jarvis-e2e");
  await expect(page.getByText("Usa la voz general: Stone")).toBeVisible();
  await page.getByRole("combobox", { name: "Voz de GPT-Live-1" }).selectOption("cedar");
  await page.getByRole("button", { name: "Guardar voz", exact: true }).click();
  await expect(page.getByText("Voz de este agente: Cedar")).toBeVisible();
  await scope.selectOption("profile-newton-e2e");
  await expect(page.getByText("Usa la voz general: Stone")).toBeVisible();
  await scope.selectOption("profile-jarvis-e2e");
  await expect(page.getByText("Voz de este agente: Cedar")).toBeVisible();
  await page.locator('.integration-settings[aria-labelledby="voice-settings-title"]').screenshot({ path: testInfo.outputPath("profile-live-voice.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Usar voz general" }).click();
  await expect(page.getByText("Usa la voz general: Stone")).toBeVisible();
  expect(writes).toEqual(["PUT:profile-jarvis-e2e", "DELETE:profile-jarvis-e2e"]);
  expect(voiceStarts).toEqual([]);
});
