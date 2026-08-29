import { expect, test } from "./fixtures";

async function databaseRecordExists(page: import("@playwright/test").Page, storeName: string, key: string) {
  return page.evaluate(async ({ storeName, key }) => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("hermes-control-client");
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result);
    });
    if (!database.objectStoreNames.contains(storeName)) {
      database.close();
      return false;
    }
    const exists = await new Promise<boolean>((resolve, reject) => {
      const request = database.transaction(storeName, "readonly").objectStore(storeName).get(key);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(Boolean(request.result));
    });
    database.close();
    return exists;
  }, { storeName, key });
}

test("expone un manifiesto instalable, registra el service worker y no cachea APIs", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium-mobile", "La auditoría de instalación se ejecuta una vez en Chromium móvil");

  await page.goto("/chats");
  const manifestHref = await page.locator('link[rel="manifest"]').getAttribute("href");
  expect(manifestHref).toBeTruthy();
  const manifest = await page.evaluate(async (href) => fetch(String(href)).then((response) => response.json()), manifestHref);
  expect(manifest).toMatchObject({
    name: "Agent Control",
    short_name: "Agent Control",
    display: "standalone",
    start_url: "/chats",
    scope: "/",
  });
  expect(manifest.icons).toEqual(expect.arrayContaining([
    expect.objectContaining({ src: "/icon-192.png", sizes: "192x192" }),
    expect.objectContaining({ src: "/icon-512.png", sizes: "512x512" }),
  ]));

  await page.waitForFunction(async () => Boolean((await navigator.serviceWorker.ready).active));
  const registrations = await page.evaluate(async () => (await navigator.serviceWorker.getRegistrations()).map((registration) => registration.scope));
  expect(registrations).toContain(`${new URL(page.url()).origin}/`);

  const cachedUrls = await page.evaluate(async () => {
    const names = await caches.keys();
    return (await Promise.all(names.map(async (name) => (await caches.open(name)).keys()))).flat().map((request) => request.url);
  });
  expect(cachedUrls.length).toBeGreaterThan(0);
  expect(cachedUrls.some((url) => new URL(url).pathname.startsWith("/api/"))).toBe(false);
});

test("arranca sin red desde el shell y conserva un borrador sin reenviarlo", async ({ context, deterministicApi, page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium-mobile", "El escenario cold-start se fija en el viewport móvil de aceptación");

  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Ajustes" })).toBeVisible();
  const cacheSwitch = page.getByRole("switch", { name: "Caché cifrada del último workspace" });
  await cacheSwitch.click();
  await expect(cacheSwitch).toHaveAttribute("aria-checked", "true");

  await page.goto("/chats");
  await expect(page.getByText("La sesión está aislada y lista para continuar.")).toBeVisible();
  await page.waitForFunction(async () => Boolean((await navigator.serviceWorker.ready).active));
  if (!await page.evaluate(() => Boolean(navigator.serviceWorker.controller))) {
    await page.reload();
    await expect(page.getByText("La sesión está aislada y lista para continuar.")).toBeVisible();
  }
  await page.waitForFunction(() => Boolean(navigator.serviceWorker.controller));

  const composer = page.getByRole("textbox", { name: /Mensaje a Newton/ });
  await composer.fill("Borrador local que no debe enviarse");
  await expect.poll(() => databaseRecordExists(page, "offlineSnapshots", "latest")).toBe(true);
  await expect.poll(() => databaseRecordExists(page, "drafts", "session-e2e")).toBe(true);
  await expect.poll(() => databaseRecordExists(page, "transcripts", "session-e2e")).toBe(true);

  await deterministicApi.remove();
  await context.setOffline(true);
  await page.reload({ waitUntil: "domcontentloaded" });

  await expect(page.getByText("Borrador offline", { exact: true })).toBeVisible();
  await expect(page.getByText("La sesión está aislada y lista para continuar.")).toBeVisible();
  await expect(page.getByRole("textbox", { name: /Mensaje a Newton/ })).toHaveValue("Borrador local que no debe enviarse");
  await expect(page.getByText("El borrador queda en este dispositivo y no se enviará al recuperar la conexión.")).toBeAttached();
  await expect(page.getByRole("button", { name: "Enviar mensaje" })).toHaveCount(0);
});

test("reabre el borrador offline aunque la caché de conversaciones esté desactivada", async ({ context, deterministicApi, page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium-mobile", "El cold-start mínimo se fija en el viewport móvil");

  await page.goto("/chats");
  await expect(page.getByText("La sesión está aislada y lista para continuar.")).toBeVisible();
  await page.waitForFunction(async () => Boolean((await navigator.serviceWorker.ready).active));
  if (!await page.evaluate(() => Boolean(navigator.serviceWorker.controller))) {
    await page.reload();
    await expect(page.getByText("La sesión está aislada y lista para continuar.")).toBeVisible();
  }
  const composer = page.getByRole("textbox", { name: /Mensaje a Newton/ });
  await composer.fill("Borrador sin caché opcional");
  await expect.poll(() => databaseRecordExists(page, "shellSnapshots", "latest")).toBe(true);
  expect(await databaseRecordExists(page, "offlineSnapshots", "latest")).toBe(false);

  await deterministicApi.remove();
  await context.setOffline(true);
  await page.reload({ waitUntil: "domcontentloaded" });

  await expect(page.getByText("Borrador offline", { exact: true })).toBeVisible();
  await expect(page.getByRole("textbox", { name: /Mensaje a Newton/ })).toHaveValue("Borrador sin caché opcional");
  await expect(page.getByRole("button", { name: "Enviar mensaje" })).toHaveCount(0);
});
