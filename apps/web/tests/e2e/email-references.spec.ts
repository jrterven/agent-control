import {
  expect,
  test,
  emailReferenceOpenPath,
  emailReferenceProviderTarget,
  emailReferenceSubject,
  emailSummarySubject,
} from "./fixtures";

test("abre una tarjeta de correo en una vista previa responsive y de texto plano", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== "chromium-mobile" && testInfo.project.name !== "chromium-desktop",
    "La aceptación responsive se valida en Chromium móvil y escritorio",
  );
  const viewport = page.viewportSize();
  if (!viewport) throw new Error("El proyecto Playwright requiere un viewport explícito");

  await page.goto("/chats");
  await expect(page.getByText("La sesión está aislada y lista para continuar.")).toBeVisible();
  const initialUrl = page.url();
  const card = page.getByRole("button", { name: `Ver correo: ${emailReferenceSubject}` });
  await expect(card).toBeVisible();
  await expect(page.getByText("Google Ads", { exact: true })).toBeVisible();
  await expect(page.getByText("Completa la verificación antes del plazo indicado.")).toBeVisible();
  const cardTrust = card.getByText("Referencia de Newton · confirma en tu buzón");
  await expect(cardTrust).toBeVisible();
  const cardBox = await card.boundingBox();
  const cardTrustBox = await cardTrust.boundingBox();
  expect(cardBox).not.toBeNull();
  expect(cardTrustBox).not.toBeNull();
  expect(cardTrustBox!.x).toBeGreaterThanOrEqual(cardBox!.x);
  expect(cardTrustBox!.x + cardTrustBox!.width).toBeLessThanOrEqual(cardBox!.x + cardBox!.width + 1);

  const searchAction = page.getByRole("link", { name: `Buscar en Gmail: ${emailReferenceSubject}` });
  await expect(searchAction).toHaveAttribute("href", emailReferenceOpenPath);
  await expect(searchAction).toHaveAttribute("target", "_blank");
  await expect(searchAction).toHaveAttribute("rel", /noopener/);
  await expect(searchAction).toHaveAttribute("referrerpolicy", "no-referrer");
  const searchHref = await searchAction.getAttribute("href");
  const resolvedSearchUrl = new URL(String(searchHref), initialUrl);
  expect(resolvedSearchUrl.origin).toBe(new URL(initialUrl).origin);
  expect(resolvedSearchUrl.pathname).toBe(emailReferenceOpenPath);

  await card.click();
  const dialog = page.getByRole("dialog", { name: emailReferenceSubject });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("Start advertiser verification now.")).toBeVisible();
  const plainTextBody = dialog.locator(".email-preview-sheet__message");
  await expect(plainTextBody).toContainText('<img src="https://tracker.invalid/pixel.png" onerror="alert(1)">');
  await expect(plainTextBody).toContainText("<strong>Tu cuenta se pausará en 10 días.</strong>");
  await expect(plainTextBody.locator("*")).toHaveCount(0);
  await expect(dialog.locator("img, script")).toHaveCount(0);
  await expect(dialog.getByText("Vista en texto plano; no carga imágenes ni contenido remoto.")).toBeVisible();
  await expect(dialog.getByText("Referencia de Newton · confirma en tu buzón")).toBeVisible();
  const dialogSearchAction = dialog.getByRole("link", { name: "Buscar en Gmail" });
  await expect(dialogSearchAction).toHaveAttribute("href", emailReferenceOpenPath);

  const sheet = dialog.locator(".email-preview-sheet");
  const box = await sheet.boundingBox();
  expect(box).not.toBeNull();
  if (testInfo.project.name === "chromium-mobile") {
    expect(box!.x).toBeLessThanOrEqual(1);
    expect(box!.width).toBeGreaterThanOrEqual(viewport.width - 2);
    expect(Math.abs(box!.y + box!.height - viewport.height)).toBeLessThanOrEqual(2);
    expect(box!.height).toBeLessThanOrEqual(viewport.height * 0.89);
  } else {
    expect(box!.width).toBeLessThanOrEqual(722);
    expect(Math.abs(box!.x + box!.width / 2 - viewport.width / 2)).toBeLessThanOrEqual(2);
    expect(box!.height).toBeLessThanOrEqual(viewport.height * 0.87);
  }

  const closeButton = dialog.locator(".email-preview-sheet__close");
  await expect(closeButton).toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(dialogSearchAction).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(closeButton).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(dialogSearchAction).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(closeButton).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(card).toBeFocused();
  expect(page.url()).toBe(initialUrl);

  await card.click();
  await expect(dialog).toBeVisible();
  await dialog.locator(".email-preview-sheet__close").click();
  await expect(dialog).toHaveCount(0);
  expect(page.url()).toBe(initialUrl);
});

test("usa un resumen compacto cuando el proveedor no entregó el cuerpo", async ({ page }, testInfo) => {
  test.skip(
    testInfo.project.name !== "chromium-mobile" && testInfo.project.name !== "chromium-desktop",
    "La aceptación responsive se valida en Chromium móvil y escritorio",
  );
  const viewport = page.viewportSize();
  if (!viewport) throw new Error("El proyecto Playwright requiere un viewport explícito");

  await page.goto("/chats");
  await page.getByRole("button", { name: `Ver correo: ${emailSummarySubject}` }).click();

  const dialog = page.getByRole("dialog", { name: emailSummarySubject });
  await expect(dialog).toBeVisible();
  await expect(dialog.getByText("Solicita una breve asesoría para exponer el estado de su protocolo.")).toBeVisible();
  await expect(dialog.getByText("Resumen proporcionado por el agente; no es el cuerpo completo del correo.")).toBeVisible();
  await expect(dialog.getByText("Este correo no tiene contenido de texto disponible.")).toHaveCount(0);
  await expect(dialog.locator(".email-preview-sheet")).toHaveClass(/is-summary-only/);
  const box = await dialog.locator(".email-preview-sheet").boundingBox();
  expect(box).not.toBeNull();
  expect(box!.height).toBeLessThanOrEqual(viewport.height * 0.75);
});

test("la PWA Android conserva la búsqueda exacta con Gmail Web y copia manual", async ({ page }, testInfo) => {
  test.skip(testInfo.project.name !== "chromium-mobile", "Este flujo corresponde a la PWA móvil instalada");
  await page.addInitScript(() => {
    const browserMatchMedia = window.matchMedia.bind(window);
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      value: (query: string) => query === "(display-mode: standalone)" ? {
        matches: true,
        media: query,
        onchange: null,
        addListener() {},
        removeListener() {},
        addEventListener() {},
        removeEventListener() {},
        dispatchEvent() { return false; },
      } : browserMatchMedia(query),
    });
    Object.defineProperty(window.navigator, "userAgent", {
      configurable: true,
      get: () => "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/128.0 Mobile Safari/537.36",
    });
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: {
        writeText(value: string) {
          (window as typeof window & { __copiedGmailSearch?: string }).__copiedGmailSearch = value;
          return Promise.resolve();
        },
      },
    });
  });

  await page.goto("/chats");
  await page.getByRole("link", { name: `Buscar en Gmail: ${emailReferenceSubject}` }).click();

  const dialog = page.getByRole("dialog", { name: emailReferenceSubject });
  await expect(dialog).toBeVisible();
  const gmailWebLink = dialog.getByRole("link", { name: "Buscar correo en Gmail Web" });
  await expect(gmailWebLink).toHaveAttribute("href", emailReferenceProviderTarget);
  await expect(gmailWebLink).toHaveAttribute("target", "_blank");
  await expect(dialog.locator('a[href^="intent:"]')).toHaveCount(0);

  await expect(dialog.getByRole("textbox", { name: "Búsqueda exacta para Gmail" })).toHaveValue(
    "rfc822msgid:e2e@example.com",
  );
  await dialog.getByRole("button", { name: "Copiar búsqueda" }).click();
  await expect(dialog.getByRole("status")).toContainText("Búsqueda copiada");
  await expect.poll(() => page.evaluate(
    () => (window as typeof window & { __copiedGmailSearch?: string }).__copiedGmailSearch,
  )).toBe("rfc822msgid:e2e@example.com");

  await page.setViewportSize({ width: 390, height: 560 });
  const copyStatus = dialog.getByRole("status");
  await copyStatus.scrollIntoViewIfNeeded();
  const sheetBox = await dialog.locator(".email-preview-sheet").boundingBox();
  const statusBox = await copyStatus.boundingBox();
  expect(sheetBox).not.toBeNull();
  expect(statusBox).not.toBeNull();
  expect(sheetBox!.y).toBeGreaterThanOrEqual(0);
  expect(sheetBox!.y + sheetBox!.height).toBeLessThanOrEqual(561);
  expect(statusBox!.y).toBeGreaterThanOrEqual(sheetBox!.y);
  expect(statusBox!.y + statusBox!.height).toBeLessThanOrEqual(sheetBox!.y + sheetBox!.height + 1);
});
