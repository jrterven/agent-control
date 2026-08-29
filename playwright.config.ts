import { defineConfig } from "@playwright/test";

const baseURL = process.env.PLAYWRIGHT_BASE_URL ?? "http://127.0.0.1:4174";

export default defineConfig({
  testDir: "./apps/web/tests/e2e",
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 2 : undefined,
  reporter: process.env.CI ? [["line"], ["html", { open: "never" }]] : "list",
  timeout: 30_000,
  expect: { timeout: 7_000 },
  outputDir: "test-results/playwright",
  use: {
    baseURL,
    locale: "es-MX",
    timezoneId: "America/Mexico_City",
    serviceWorkers: "allow",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  webServer: process.env.PLAYWRIGHT_BASE_URL
    ? undefined
    : {
        command: "npm run build --workspace @hermes-control/web && npm run preview --workspace @hermes-control/web -- --host 127.0.0.1 --port 4174 --strictPort",
        url: `${baseURL}/manifest.webmanifest`,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
      },
  projects: [
    {
      name: "chromium-mobile",
      use: { browserName: "chromium", viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true },
    },
    {
      name: "chromium-tablet",
      use: { browserName: "chromium", viewport: { width: 1024, height: 1366 }, hasTouch: true },
    },
    {
      name: "chromium-desktop",
      use: { browserName: "chromium", viewport: { width: 1440, height: 900 } },
    },
    {
      name: "firefox-desktop",
      use: { browserName: "firefox", viewport: { width: 1440, height: 900 } },
    },
    {
      name: "webkit-mobile",
      use: { browserName: "webkit", viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true },
    },
  ],
});
