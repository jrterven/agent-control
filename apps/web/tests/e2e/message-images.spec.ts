import { readFileSync } from "node:fs";
import { expect, test } from "./fixtures";

test.use({ serviceWorkers: "block" });
const first = "a".repeat(32);
const second = "b".repeat(32);
// Deterministic raster fixtures exercise real decoding and responsive containment.
const diagram = readFileSync(new URL("./fixtures/images/research-diagram.png", import.meta.url));
const chart = readFileSync(new URL("./fixtures/images/research-chart.png", import.meta.url));
const content = `## Briefing visual\n\nContexto anterior.\n\n![Arquitectura](ac-media:${first})\n![Resultados](ac-media:${second})\n\nConclusión después de las figuras.\n\n![No cargar](https://external.invalid/tracker.png)`;

async function installImages(page: import("@playwright/test").Page, pending = false) {
  let metadataAttempts = 0;
  await page.route("**/api/v1/sessions/session-e2e/live-transcripts", (route) => route.fulfill({ json: { items: [], nextCursor: null } }));
  await page.route("**/api/v1/sessions/session-e2e/messages", (route) => route.fulfill({ json: { items: [
    { id: "visual-briefing", role: "assistant", content, controlMedia: [{ id: first, kind: "image", mediaType: "image/png" }, { id: second, kind: "image", mediaType: "image/png" }] },
  ] } }));
  await page.route(/\/api\/v1\/sessions\/session-e2e\/media\/[ab]{32}\/metadata$/, (route) => {
    metadataAttempts += 1;
    if (pending && metadataAttempts <= 2) return route.fulfill({ status: 404, json: { detail: "Not published" } });
    return route.fulfill({ json: { id: route.request().url().split("/").at(-2), kind: "image", status: "ready", mediaType: "image/png", width: 800, height: 500, alt: "Figura", caption: "Evidencia del artículo", provenance: "web", sourceUrl: "https://example.com/article", sourceTitle: "Artículo original" } });
  });
  await page.route(/\/api\/v1\/sessions\/session-e2e\/media\/[ab]{32}\?variant=(thumbnail|full)$/, (route) => route.fulfill({ contentType: "image/png", body: route.request().url().includes(first) ? diagram : chart }));
  return { attempts: () => metadataAttempts };
}

test("presenta la galería en su posición, amplía, navega y conserva imágenes al reabrir", async ({ page }) => {
  await installImages(page);
  const remoteRequests: string[] = [];
  page.on("request", (request) => { if (request.url().includes("external.invalid")) remoteRequests.push(request.url()); });
  await page.goto("/chats");
  const gallery = page.getByRole("region", { name: "Imágenes de la respuesta" });
  await expect(gallery.getByRole("img")).toHaveCount(2);
  await expect(gallery.getByRole("img", { name: "Arquitectura" })).toBeVisible();
  await expect(page.locator("audio")).toHaveCount(0);
  expect(remoteRequests).toEqual([]);
  const markdown = page.locator(".markdown-body");
  expect(await markdown.evaluate((element) => Array.from(element.children).map((child) => child.tagName))).toEqual(["H2", "P", "SECTION", "P", "P"]);
  const opener = gallery.getByRole("button", { name: "Ampliar imagen: Arquitectura" });
  await opener.click();
  const viewer = page.getByRole("dialog", { name: "Visor de imágenes" });
  await expect(viewer).toBeVisible();
  await expect(viewer.getByRole("img", { name: "Arquitectura" })).toBeVisible();
  await viewer.getByRole("button", { name: "Ampliar", exact: true }).click();
  await expect(viewer.getByRole("button", { name: "Ajustar a pantalla" })).toBeVisible();
  await viewer.getByRole("button", { name: "Imagen siguiente" }).click();
  await expect(viewer.getByRole("img", { name: "Resultados" })).toBeVisible();
  await expect(viewer.getByRole("link", { name: "Descargar imagen" })).toHaveAttribute("href", `/api/v1/sessions/session-e2e/media/${second}?variant=full`);
  await expect(viewer.getByRole("link", { name: "Artículo original" })).toHaveAttribute("href", "https://example.com/article");
  await page.screenshot({ path: `test-results/message-images-viewer-${test.info().project.name}.png` });
  await viewer.getByRole("button", { name: "Cerrar visor" }).click();
  await expect(viewer).toBeHidden();
  await expect(opener).toBeFocused();
  await page.reload();
  await expect(gallery.getByRole("img")).toHaveCount(2);
  await expect(gallery.getByRole("img", { name: "Resultados" })).toBeVisible();
  await page.screenshot({ path: `test-results/message-images-gallery-${test.info().project.name}.png` });
  expect(remoteRequests).toEqual([]);
});

test("recupera una publicación pendiente sin bloquear el briefing", async ({ page }) => {
  const calls = await installImages(page, true);
  await page.goto("/chats");
  await expect(page.getByText("Publicando imagen…")).toHaveCount(2);
  await expect(page.getByText("Conclusión después de las figuras.")).toBeVisible();
  await expect(page.getByRole("region", { name: "Imágenes de la respuesta" }).getByRole("img")).toHaveCount(2);
  expect(calls.attempts()).toBe(4);
});

test("muestra la miniatura enviada, abre el original y conserva el texto al recargar", async ({ page }) => {
  await installImages(page);
  const question = 'En este sensor de luz, ¿qué dan los pines A0 y D0?';
  await page.route('**/api/v1/sessions/session-e2e/messages', route => route.fulfill({json:{items:[
    {id:'sent-photo', role:'user', content:question, controlAttachments:[{kind:'image',name:'sensor.png',mediaType:'image/png',size:8000,mediaId:first}]},
    {id:'reply',role:'assistant',content:'A0 es una salida analógica y D0 una salida digital.'},
  ]}}));
  await page.goto('/chats');
  const bubble = page.locator('.user-bubble');
  const thumbnail = bubble.getByRole('img', {name:'sensor.png'});
  await expect(thumbnail).toBeVisible();
  await expect(thumbnail).toHaveJSProperty('naturalWidth', 800);
  await expect(bubble.locator('p')).toHaveText(question);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const bounds = await thumbnail.boundingBox();
  expect(bounds!.width).toBeGreaterThan(100);
  expect(bounds!.width).toBeLessThanOrEqual(320);
  await page.screenshot({path:`test-results/sent-image-${test.info().project.name}.png`});
  const opener = bubble.getByRole('button',{name:'Ampliar imagen: sensor.png'});
  await opener.click();
  const viewer = page.getByRole('dialog',{name:'Visor de imágenes'});
  await expect(viewer.getByRole('img',{name:'sensor.png'})).toHaveAttribute('src',`/api/v1/sessions/session-e2e/media/${first}?variant=full`);
  await expect(viewer.getByRole('img',{name:'sensor.png'})).toHaveJSProperty('naturalWidth',800);
  await viewer.getByRole('button',{name:'Cerrar visor'}).click();
  await expect(opener).toBeFocused();
  await page.reload();
  await expect(thumbnail).toBeVisible();
  await expect(bubble.locator('p')).toHaveText(question);
});

test("la imagen enviada recibe su miniatura sin esperar la respuesta del agente", async ({page}) => {
  await installImages(page);
  await page.route('**/api/v1/sessions/session-e2e/messages', route => route.fulfill({json:{items:[]}}));
  await page.route('**/api/v1/sessions/session-e2e/prompts-with-attachments', route => route.fulfill({status:202,json:{
    operationId:route.request().headers()['idempotency-key'],status:'accepted',
    attachments:[{kind:'image',name:'sensor.png',mediaType:'image/png',size:diagram.length,mediaId:first}],
  }}));
  await page.goto('/chats');
  await page.locator('input[type=file][accept="image/jpeg,image/png,image/webp,image/gif"]').setInputFiles({name:'sensor.png',mimeType:'image/png',buffer:diagram});
  await page.getByRole('textbox',{name:'Mensaje a Newton…'}).fill('¿Qué hacen A0 y D0?');
  await page.getByRole('button',{name:'Enviar mensaje',exact:true}).click();
  await expect(page.locator('.user-bubble').getByRole('img',{name:'sensor.png'})).toBeVisible();
});
