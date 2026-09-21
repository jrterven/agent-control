import { test, expect } from "./fixtures";

test("company pages stay public in every language with an installed PWA", async ({ page, context }, info) => {
  // Install the app worker first to exercise its navigation fallback boundary.
  test.skip(info.project.name.startsWith("webkit"), "Service worker support differs in Playwright WebKit.");
  await page.goto("/settings#plugins");
  await expect(page.locator('#plugins a[href="/about-es.html"]')).toBeVisible();
  await page.evaluate(async () => { await navigator.serviceWorker.ready; });
  if (!await page.evaluate(() => Boolean(navigator.serviceWorker.controller))) await page.reload();
  await page.waitForFunction(() => Boolean(navigator.serviceWorker.controller));
  await context.clearCookies();
  for (const lang of ["en", "es", "fr", "de", "pt"]) {
    const path = `/about${lang === "en" ? "" : `-${lang}`}.html`;
    await page.goto(path);
    await expect(page).toHaveURL(new RegExp(`${path}$`));
    await expect(page.locator("html")).toHaveAttribute("lang", lang);
    await expect(page.getByRole("heading", { level: 1 })).toBeVisible();
    await expect(page.getByRole("link", { name: "support@jemailabs.com" })).toHaveAttribute("href", "mailto:support@jemailabs.com");
    await expect(page.locator('a[href="/login"]')).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  }
  await page.goto("/about-es.html");
  await page.screenshot({ path: info.outputPath("company.png"), fullPage: true });
});
