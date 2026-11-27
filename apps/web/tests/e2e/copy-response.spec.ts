import type { Locator } from "@playwright/test";
import { expect, test } from "./fixtures";

test.use({ serviceWorkers: "block" });

async function scrollIntoReply(reply: Locator, offset: number) {
  await reply.evaluate((element, offset) => {
    const viewport = element.closest<HTMLElement>(".message-scroll")!;
    viewport.scrollTop += element.getBoundingClientRect().top - viewport.getBoundingClientRect().top + offset;
  }, offset);
}

test("fija solo el icono de copiar mientras se recorre una respuesta larga", async ({ page }) => {
  const paragraphs = Array.from({ length: 60 }, (_, i) => `Párrafo ${i + 1} del informe de prueba.`).join("\n\n");
  await page.route("**/api/v1/sessions/session-e2e/messages", (route) => route.fulfill({ json: {
    sessionStatus: "ready",
    items: [
      { id: "first-answer", role: "assistant", timestamp: "2026-11-24T18:00:00Z", content: `## Informe largo\n\n${paragraphs}` },
      { id: "next-answer", role: "assistant", timestamp: "2026-11-24T18:01:00Z", content: `## Siguiente respuesta\n\n${paragraphs}` },
    ],
  } }));
  await page.goto("/chats");

  const reply = page.getByRole("article").filter({ has: page.getByRole("heading", { name: "Informe largo" }) });
  const buttons = reply.getByRole("button", { name: "Copiar respuesta" });
  await expect(buttons).toHaveCount(2);
  const top = buttons.first();
  const bottom = buttons.last();
  const timestamp = reply.locator("header time");

  await scrollIntoReply(reply, -12);
  await expect(top).toBeInViewport({ ratio: 0.95 });
  await expect(timestamp).toBeInViewport();
  await scrollIntoReply(reply, 500);
  await expect(top).toBeInViewport({ ratio: 0.95 });
  await expect(reply.locator("header")).not.toBeInViewport();
  await expect(bottom).not.toBeInViewport();
  await expect.poll(() => top.evaluate((button) => {
    const viewport = button.closest(".message-scroll")!;
    return Math.abs(button.getBoundingClientRect().top - viewport.getBoundingClientRect().top);
  })).toBeLessThan(2);

  await bottom.scrollIntoViewIfNeeded();
  await expect(bottom).toBeInViewport({ ratio: 0.95 });
  const nextReply = page.getByRole("article").filter({ has: page.getByRole("heading", { name: "Siguiente respuesta" }) });
  await scrollIntoReply(nextReply, 300);
  await expect(top).not.toBeInViewport();
  await expect(nextReply.getByRole("button", { name: "Copiar respuesta" }).first()).toBeInViewport({ ratio: 0.95 });
});
