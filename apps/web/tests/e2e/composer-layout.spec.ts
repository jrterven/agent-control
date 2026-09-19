import type { Locator, Page } from "@playwright/test";
import { bootstrapData, expect, test } from "./fixtures";

test.use({ serviceWorkers: "block" });

const draft = "Quiero revisar el informe sin perder espacio para escribir.";
const filename = "informe-de-investigacion-con-un-nombre-largo-para-movil.txt";

async function mockComposer(page: Page, busy = false) {
  let working = busy;
  let interruptions = 0;
  let transcriptionRequests = 0;
  await page.route("**/api/v1/bootstrap", (route) => route.fulfill({ json: {
    ...bootstrapData,
    features: {
      dictation: { available: true, provider: "elevenlabs", modelId: "scribe_v2_realtime" },
      voice: { provider: "elevenlabs" },
      live: { available: true, provider: "openai", modelId: "gpt-live-1" },
    },
  } }));
  await page.route("**/api/v1/vision/preferences", (route) => route.fulfill({ json: {
    modelId: "gpt-5.6-luna", intervalSeconds: 5, configured: true,
  } }));
  for (const endpoint of ["vision/observations", "live-transcripts"]) {
    await page.route(`**/api/v1/sessions/*/${endpoint}**`, (route) => route.fulfill({ json: { items: [], nextCursor: null } }));
  }
  await page.route("**/api/v1/sessions/session-e2e/messages", (route) => route.fulfill({ json: {
    items: [{ id: "composer-history", role: "assistant", content: "Podemos continuar aquí.", timestamp: 1_790_000_000 }],
    sessionStatus: working ? "streaming" : "ready",
    activeOperation: working ? { operationId: "composer-operation", status: "streaming", acceptedAt: new Date().toISOString() } : null,
  } }));
  await page.route("**/api/v1/sessions/session-e2e/interrupt", (route) => {
    interruptions += 1;
    working = false;
    return route.fulfill({ status: 204 });
  });
  // A real UI error state exercises the status row without microphone access
  // or a provider connection. No store injection or synthetic DOM is needed.
  await page.route("**/api/v1/realtime/transcription-token", (route) => {
    transcriptionRequests += 1;
    return route.fulfill({ status: 503, json: { detail: "Dictation temporarily unavailable" } });
  });
  await page.addInitScript(() => {
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: {
      enumerateDevices: async () => [],
      getUserMedia: async () => { throw new DOMException("No capture in layout tests", "NotAllowedError"); },
    } });
  });
  return { interruptions: () => interruptions, transcriptionRequests: () => transcriptionRequests };
}

const editor = (page: Page) => page.getByRole("textbox", { name: "Mensaje a Newton…" });
const controls = (page: Page, busy = false) => [
  "Agregar al chat", "Activar cámara", "Conversar con GPT-Live-1", "Dictar por voz",
  busy ? "Detener" : "Enviar mensaje",
].map((name) => page.getByRole("button", { name, exact: true }));

async function bounds(locator: Locator) {
  await expect(locator).toBeVisible();
  const box = await locator.boundingBox();
  expect(box).not.toBeNull();
  return box!;
}

async function noHorizontalOverflow(page: Page) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  const composer = await bounds(page.locator(".composer"));
  expect(composer.x).toBeGreaterThanOrEqual(0);
  expect(composer.x + composer.width).toBeLessThanOrEqual(page.viewportSize()!.width + 1);
}

async function mobileGeometry(page: Page, busy = false) {
  const composer = await bounds(page.locator(".composer"));
  const text = await bounds(editor(page));
  // The usable writing area must span nearly the whole composer, even with
  // camera, both voice controls and Send/Stop all present.
  expect(text.width).toBeGreaterThanOrEqual(composer.width - 20);
  expect(text.x - composer.x).toBeLessThanOrEqual(10);
  const buttons = await Promise.all(controls(page, busy).map(bounds));
  for (const [index, button] of buttons.entries()) {
    expect(button.width).toBeGreaterThanOrEqual(44);
    expect(button.height).toBeGreaterThanOrEqual(44);
    expect(button.y).toBeGreaterThanOrEqual(text.y + text.height - 1);
    expect(button.x).toBeGreaterThanOrEqual(composer.x);
    expect(button.x + button.width).toBeLessThanOrEqual(composer.x + composer.width + 1);
    await expect(controls(page, busy)[index]).toBeInViewport({ ratio: 1 });
  }
  // None of the touch targets may overlap, including a wider Stop button.
  for (let i = 0; i < buttons.length; i++) for (let j = i + 1; j < buttons.length; j++) {
    const a = buttons[i], b = buttons[j];
    expect(a.x + a.width <= b.x + 1 || b.x + b.width <= a.x + 1
      || a.y + a.height <= b.y + 1 || b.y + b.height <= a.y + 1).toBe(true);
  }
  await noHorizontalOverflow(page);
}

async function attachFile(page: Page) {
  await page.getByRole("button", { name: "Agregar al chat", exact: true }).click();
  const menu = page.getByRole("menu", { name: "Opciones para agregar" });
  await expect(menu).toBeInViewport({ ratio: 1 });
  await expect(menu.getByRole("menuitem", { name: /^Imagen/ })).toBeVisible();
  const chooser = page.waitForEvent("filechooser");
  await menu.getByRole("menuitem", { name: /^Archivo/ }).click();
  await (await chooser).setFiles({ name: filename, mimeType: "text/plain", buffer: Buffer.from("Informe de prueba.") });
  await expect(menu).toHaveCount(0);
  await expect(page.locator(".composer-attachments").getByText(filename, { exact: true })).toBeVisible();
}

test("keeps full-width writing and touch controls at 320, 390 and 599px", async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 320, height: 844 });
  await mockComposer(page);
  await page.goto("/chats");
  for (const width of [320, 390, 599]) {
    await page.setViewportSize({ width, height: 844 });
    await editor(page).fill("");
    for (const button of controls(page).slice(0, -1)) await expect(button).toBeEnabled();
    await mobileGeometry(page);
    await editor(page).fill(`${draft}\nUna segunda línea para seguir escribiendo.`);
    await expect(editor(page)).toBeEditable();
    await mobileGeometry(page);
    await attachFile(page);
    const attachments = await bounds(page.locator(".composer-attachments"));
    const text = await bounds(editor(page));
    expect(attachments.y + attachments.height).toBeLessThanOrEqual(text.y + 1);
    await mobileGeometry(page);
    await page.locator(".composer").screenshot({ path: testInfo.outputPath(`composer-${width}-attached.png`) });
    await page.getByRole("button", { name: `Quitar ${filename}`, exact: true }).click();
    await expect(page.locator(".composer-attachments")).toHaveCount(0);
    await expect(editor(page)).toHaveValue(`${draft}\nUna segunda línea para seguir escribiendo.`);
    await mobileGeometry(page);
  }
});

test("preserves the single row at the 600px boundary, tablet and desktop", async ({ page }) => {
  await page.setViewportSize({ width: 600, height: 900 });
  await mockComposer(page);
  await page.goto("/chats");
  for (const width of [600, 768, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await editor(page).fill("");
    for (const button of controls(page).slice(0, -1)) await expect(button).toBeEnabled();
    const text = await bounds(editor(page));
    const [add, ...actions] = await Promise.all(controls(page).map(bounds));
    expect(add.x + add.width).toBeLessThanOrEqual(text.x + 1);
    expect(text.x + text.width).toBeLessThanOrEqual(Math.min(...actions.map((box) => box.x)) + 1);
    for (const box of [add, ...actions]) {
      expect(box.y).toBeLessThan(text.y + text.height);
      expect(box.y + box.height).toBeGreaterThan(text.y);
    }
    await editor(page).fill(draft);
    await expect(page.getByRole("button", { name: "Enviar mensaje" })).toBeEnabled();
    await noHorizontalOverflow(page);
  }
});

test("keeps the draft and Stop reachable in a short mobile viewport", async ({ page }, testInfo) => {
  // A reduced viewport exercises the available space while a keyboard is open;
  // browser automation does not reproduce a physical iOS/Android keyboard.
  await page.setViewportSize({ width: 320, height: 400 });
  const mock = await mockComposer(page, true);
  await page.goto("/chats");
  await expect(page.getByRole("button", { name: "Detener", exact: true })).toBeEnabled();
  await editor(page).fill(draft);
  await editor(page).focus();
  await expect(editor(page)).toBeFocused();
  await expect(editor(page)).toBeInViewport({ ratio: 1 });
  await mobileGeometry(page, true);
  await page.screenshot({ path: testInfo.outputPath("composer-short-viewport-busy.png") });
  await page.getByRole("button", { name: "Detener", exact: true }).click();
  await expect.poll(mock.interruptions).toBe(1);
  await expect(page.getByRole("button", { name: "Enviar mensaje", exact: true })).toBeEnabled();
  await expect(editor(page)).toHaveValue(draft);
  await mobileGeometry(page);
  await page.setViewportSize({ width: 390, height: 640 });
  // An actual failed token request also covers feedback below the controls,
  // first without an attachment and then after choosing a real file input.
  await page.getByRole("button", { name: "Dictar por voz", exact: true }).click();
  await expect.poll(mock.transcriptionRequests).toBe(1);
  await expect(page.getByRole("alert").filter({ hasText: "Se perdió la conexión durante el dictado." })).toBeVisible();
  for (const attached of [false, true]) {
    if (attached) await attachFile(page);
    const status = await bounds(page.locator(".dictation-state"));
    for (const button of controls(page)) {
      const box = await bounds(button);
      expect(status.y).toBeGreaterThanOrEqual(box.y + box.height - 1);
    }
    await expect(editor(page)).toBeEditable();
    await expect(editor(page)).toHaveValue(draft);
    await mobileGeometry(page);
  }
});
