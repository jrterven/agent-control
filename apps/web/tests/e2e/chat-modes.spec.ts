import { bootstrapData, expect, test } from "./fixtures";

test.use({ serviceWorkers: "block" });

test("elige el tipo antes de enviar y cierra el temporal sin añadirlo al historial", async ({ page }, testInfo) => {
  let creates = 0;
  let closes = 0;
  let prompt = "";
  const id = "tmp_chat_mode_e2e";
  await page.route("**/api/v1/bootstrap", (route) => route.fulfill({ json: {
    ...bootstrapData,
    profiles: bootstrapData.profiles.map((profile) => ({ ...profile, capabilitySet: { protocol: "dashboard-rpc", version: "0.21.2", methods: ["session.create", "prompt.submit"], features: ["session.mode.temporary", "session.mode.memory_read_only"] } })),
  } }));
  await page.route("**/api/v1/sessions", (route) => {
    creates++;
    expect(route.request().postDataJSON().chatMode).toBe("temporary");
    return route.fulfill({ json: { ...bootstrapData.sessions[0], id, chatMode: "temporary", temporaryAccess: "tab-secret", title: "Temporal de prueba" } });
  });
  await page.route(`**/api/v1/sessions/${id}/messages`, (route) => route.fulfill({ json: { items: prompt ? [{ id: "private-msg", role: "user", content: prompt }] : [], sessionStatus: "ready" } }));
  await page.route(`**/api/v1/sessions/${id}/prompts`, (route) => {
    expect(route.request().headers()["x-temporary-chat"]).toBe("tab-secret");
    prompt = route.request().postDataJSON().content;
    return route.fulfill({ json: { operationId: "private-prompt", status: "accepted" } });
  });
  await page.route(`**/api/v1/sessions/${id}/temporary/close`, (route) => { closes++; return route.fulfill({ status: 204 }); });
  await page.goto("/chats");
  await page.locator(".identity-button").click();
  await page.locator("#left-sidebar").getByRole("button", { name: "Nuevo chat", exact: true }).click();
  await expect(page.getByRole("radio")).toHaveCount(3);
  expect(creates).toBe(0);
  await page.getByRole("radio", { name: /Temporal privado/ }).check();
  await page.screenshot({ path: testInfo.outputPath("chat-mode-selector.png"), fullPage: true });
  await page.getByRole("textbox", { name: "Mensaje a Newton…" }).fill("Mensaje temporal de prueba");
  await page.getByRole("button", { name: "Enviar mensaje", exact: true }).click();
  await expect(page.getByRole("button", { name: "Temporal privado", exact: true })).toBeVisible();
  await expect.poll(() => creates).toBe(1);
  await expect.poll(() => prompt).toBe("Mensaje temporal de prueba");
  await page.getByRole("button", { name: "Temporal privado", exact: true }).click();
  await expect(page.getByRole("tooltip")).toContainText("Se elimina al salir");
  await page.screenshot({ path: testInfo.outputPath("temporary-header.png"), fullPage: true });
  await page.locator(".identity-button").click();
  await expect(page.locator("#left-sidebar")).not.toContainText("Temporal de prueba");
  expect(closes).toBe(0); // Opening the sidebar keeps this conversation alive.
  await page.locator("#left-sidebar").getByRole("button", { name: "Nuevo chat", exact: true }).click();
  await expect.poll(() => closes).toBe(1);
  await expect(page.getByRole("radio")).toHaveCount(3);
  await page.reload();
  await expect(page.getByRole("button", { name: "Cerrar chat temporal" })).toHaveCount(0);
});
