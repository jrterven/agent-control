import { expect, test } from "./fixtures";

test("shows dictation privacy information in settings without starting voice", async ({ page }, testInfo) => {
  const voiceRequests: string[] = [];
  page.on("request", (request) => {
    if (/transcription|live-session|live-voice-preview/.test(request.url())) voiceRequests.push(request.url());
  });
  await page.route("**/api/v1/integrations/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/openai/voice")) return route.fulfill({ json: { voiceId: "marin" } });
    if (path.endsWith("/openai")) return route.fulfill({ json: { configured: false, provider: "openai", modelId: "gpt-live-1" } });
    if (path.endsWith("/elevenlabs")) return route.fulfill({ json: { configured: false, provider: "elevenlabs", modelId: "scribe_v2_realtime" } });
    return route.fallback();
  });
  await page.goto("/settings");
  const privacy = page.locator("#privacy");
  await privacy.scrollIntoViewIfNeeded();
  await expect(privacy.getByText("Privacidad", { exact: true })).toBeVisible();
  await expect(privacy.getByRole("listitem")).toHaveCount(3);
  await expect(privacy.getByText("El audio se envía directamente a ElevenLabs con un token temporal.")).toBeVisible();
  await expect(privacy.getByText(/La conservación y el uso del audio/)).toBeVisible();
  await expect(privacy.getByText(/nunca se envía automáticamente al agente/)).toBeVisible();
  await privacy.screenshot({ path: testInfo.outputPath("privacy-settings.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  expect(voiceRequests).toEqual([]);
  await page.goto("/chats");
  await expect(page.getByRole("dialog", { name: "Activar dictado por voz" })).toHaveCount(0);
});
