import { expect, test } from "./fixtures";

async function writeBootRecoveryMarker(page: import("@playwright/test").Page) {
  await page.evaluate(async () => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("agent-control-boot-recovery-e2e", 1);
      request.onupgradeneeded = () => request.result.createObjectStore("markers");
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result);
    });
    await new Promise<void>((resolve, reject) => {
      const transaction = database.transaction("markers", "readwrite");
      transaction.objectStore("markers").put("preserved", "draft");
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error);
    });
    database.close();
    const cache = await caches.open("agent-control-boot-recovery-e2e");
    await cache.put("/stale-shell-marker", new Response("stale"));
  });
}

async function bootRecoveryMarkerExists(page: import("@playwright/test").Page) {
  return page.evaluate(async () => {
    const database = await new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open("agent-control-boot-recovery-e2e", 1);
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result);
    });
    const marker = await new Promise<unknown>((resolve, reject) => {
      const request = database.transaction("markers", "readonly").objectStore("markers").get("draft");
      request.onerror = () => reject(request.error);
      request.onsuccess = () => resolve(request.result);
    });
    database.close();
    return marker === "preserved";
  });
}

async function bootRecoveryCacheExists(page: import("@playwright/test").Page) {
  return page.evaluate(async () => (await caches.keys()).includes("agent-control-boot-recovery-e2e"));
}

test("ofrece recuperación visible si el bundle no puede iniciar y conserva IndexedDB", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium-mobile", "El arranque fallido se valida una vez en Chromium móvil");

  await page.route("**/assets/*.js", (route) => route.abort());
  await page.goto("/chats", { waitUntil: "domcontentloaded" });

  await expect(page.getByText("La app no pudo iniciar. Tus borradores locales se conservarán.")).toBeVisible();
  const repair = page.getByRole("button", { name: "Reparar y recargar" });
  await expect(repair).toBeVisible();

  await writeBootRecoveryMarker(page);
  await repair.click();
  await page.waitForURL((url) => url.searchParams.has("app-recovery"));
  await expect.poll(() => bootRecoveryMarkerExists(page)).toBe(true);
  await expect.poll(() => bootRecoveryCacheExists(page)).toBe(false);
});

test("reactiva la recuperación tras un fallo fatal posterior al montaje", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium-mobile", "La recuperación fatal se valida una vez en Chromium móvil");

  await page.goto("/chats");
  await expect(page.getByText("La sesión está aislada y lista para continuar.")).toBeVisible();
  await expect(page.locator("[data-agent-control-boot]")).toBeHidden();

  await page.evaluate(() => window.dispatchEvent(new Event("agent-control:fatal")));

  await expect(page.getByText("La app no pudo iniciar. Tus borradores locales se conservarán.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Reparar y recargar" })).toBeVisible();
});

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
    expect.objectContaining({ src: "/icon-192.png", sizes: "192x192", purpose: "any" }),
    expect.objectContaining({ src: "/icon-512.png", sizes: "512x512", purpose: "any" }),
    expect.objectContaining({ src: "/icon-maskable-512.png", sizes: "512x512", purpose: "maskable" }),
  ]));

  await expect(page.locator('link[rel="icon"][href="/favicon.svg"]')).toHaveCount(1);
  await expect(page.locator('link[rel="apple-touch-icon"][href="/apple-touch-icon.png"]')).toHaveCount(1);

  await page.waitForFunction(async () => Boolean((await navigator.serviceWorker.ready).active));
  const registrations = await page.evaluate(async () => (await navigator.serviceWorker.getRegistrations()).map((registration) => registration.scope));
  expect(registrations).toContain(`${new URL(page.url()).origin}/`);

  const cachedUrls = await page.evaluate(async () => {
    const names = await caches.keys();
    return (await Promise.all(names.map(async (name) => (await caches.open(name)).keys()))).flat().map((request) => request.url);
  });
  expect(cachedUrls.length).toBeGreaterThan(0);
  expect(cachedUrls.some((url) => new URL(url).pathname === "/boot-recovery.js")).toBe(true);
  expect(cachedUrls.some((url) => new URL(url).pathname.startsWith("/api/"))).toBe(false);
});

test("arranca sin red desde el shell y conserva un borrador sin reenviarlo", async ({ context, deterministicApi, page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium-mobile", "El escenario cold-start se fija en el viewport móvil de aceptación");

  await page.goto("/settings");
  await expect(page.getByRole("heading", { name: "Ajustes" })).toBeVisible();
  await expect(page.getByText("Versión instalada")).toBeVisible();
  await expect(page.getByText(/^v0\.1\.0(?:\+[a-f0-9]+)?$/)).toBeVisible();
  await expect(page.getByRole("button", { name: "Buscar actualizaciones" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
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
