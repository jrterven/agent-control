import { bootstrapData, expect, test } from "./fixtures";
import type { LiveFragment } from "../../src/lib/openaiLiveClient";

test.use({ serviceWorkers: "block" });

test("shows conversation without internal delegation instructions in saved voice history", async ({ page }, testInfo) => {
  await page.route("**/api/v1/sessions/*/live-transcripts**", (route) => route.fulfill({ json: { items: [], nextCursor: null } }));
  await page.route("**/api/v1/sessions/session-e2e/messages", (route) => route.fulfill({ json: {
    items: [
      { id: "voice-question", role: "user", content: "This is a live voice request in your current conversation. Keep your own identity, personality, configured instructions, memory, tools and permissions.\n\nLive conversation:\nUser: Hola, ¿quién eres?\nVoice assistant: Soy Newton, tu agente.\nUser: ¿Qué puedes hacer por mí?", timestamp: Date.now() / 1000 - 10 },
      { id: "voice-answer", role: "assistant", content: "Puedo ayudarte con tareas digitales y aprender nuevos procedimientos.", timestamp: Date.now() / 1000 },
    ], sessionStatus: "ready", activeOperation: null,
  } }));
  await page.goto("/chats");
  for (let attempt = 0; attempt < 2; attempt++) {
    await expect(page.getByText("Hola, ¿quién eres?", { exact: true })).toBeVisible();
    await expect(page.getByText("Soy Newton, tu agente.", { exact: true })).toBeVisible();
    await expect(page.getByText("¿Qué puedes hacer por mí?", { exact: true })).toBeVisible();
    await expect(page.getByText("Puedo ayudarte con tareas digitales y aprender nuevos procedimientos.")).toBeVisible();
    await expect(page.getByText(/This is a live voice request|Keep your own identity|Live conversation:|Voice assistant:/)).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    if (attempt === 0) await page.reload();
  }
  await page.screenshot({ path: testInfo.outputPath("voice-conversation-only.png"), fullPage: true });
});

test("waits for readiness and pauses the microphone without a new call", async ({ page }, testInfo) => {
  let calls = 0;
  const history = new Map<string, { id: string; createdAt: string; fragments: LiveFragment[] }>();
  let allowSave!: () => void;
  const saveGate = new Promise<void>((resolve) => { allowSave = resolve; });
  await page.route("**/api/v1/sessions/*/live-transcripts**", async (route) => {
    if (route.request().method() === "PUT") {
      const id = new URL(route.request().url()).pathname.split("/").at(-1)!;
      const payload = route.request().postDataJSON();
      const previous = history.get(id);
      history.set(id, { id, createdAt: previous?.createdAt ?? new Date().toISOString(), fragments: [...(previous?.fragments.slice(0, payload.offset) ?? []), ...payload.fragments] });
      await saveGate;
      return route.fulfill({ status: 204 });
    }
    return route.fulfill({ json: { items: [...history.values()], nextCursor: null } });
  });
  await page.route("**/api/v1/bootstrap", (route) => route.fulfill({ json: {
    ...bootstrapData,
    features: { dictation: { available: true, provider: "elevenlabs", modelId: "scribe_v2_realtime" }, voice: { provider: "elevenlabs" }, live: { available: true, provider: "openai", modelId: "gpt-live-1" } },
  } }));
  await page.route("**/api/v1/realtime/live-session", (route) => {
    calls += 1;
    return route.fulfill({ json: { session: { id: "fake-live" }, transport: { sdp: "fake-answer" } } });
  });
  await page.addInitScript(() => {
    const track = Object.assign(new EventTarget(), { enabled: true, muted: false, readyState: "live", stop() { this.readyState = "ended"; } });
    class Channel extends EventTarget {
      readyState = "open";
      send(data: string) {
        if (JSON.parse(data).type === "session.close") queueMicrotask(() => {
          this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "session.input_transcript.delta", event_id: "tail", delta: " Gracias.", start_ms: 4000, end_ms: 4500 }) }));
          this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "session.closed", reason: "close_requested" }) }));
        });
      }
      close() { this.readyState = "closed"; }
    }
    const channel = new Channel();
    class Peer extends EventTarget {
      connectionState = "connected";
      iceGatheringState = "complete";
      localDescription = { type: "offer", sdp: "fake-offer" };
      addTrack() {}
      createDataChannel() { return channel; }
      async createOffer() { return this.localDescription; }
      async setLocalDescription() {}
      async setRemoteDescription() {}
      close() {}
    }
    Object.defineProperty(window, "RTCPeerConnection", { value: Peer });
    Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia: async () => ({ getTracks: () => [track], getAudioTracks: () => [track] }) } });
    Object.assign(window, { liveTest: {
      ready: () => channel.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "session.started" }) })),
      enabled: () => track.enabled,
      event: (event: Record<string, unknown>) => channel.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(event) })),
    } });
  });
  const micEnabled = () => page.evaluate(() => (window as unknown as { liveTest: { enabled(): boolean } }).liveTest.enabled());
  await page.goto("/chats");
  await expect(page.getByRole("button", { name: "Dictar por voz" })).toBeEnabled();
  await page.locator(".composer").screenshot({ path: testInfo.outputPath("both-voice-buttons.png") });
  await page.getByRole("button", { name: "Conversar con GPT-Live-1" }).click();
  await expect(page.getByRole("button", { name: "Dictar por voz" })).toBeDisabled();
  await expect(page.getByText("Conectando… espera para hablar")).toBeVisible();
  await expect.poll(micEnabled).toBe(false);
  await expect.poll(() => calls).toBe(1);
  await page.evaluate(() => (window as unknown as { liveTest: { ready(): void } }).liveTest.ready());
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await expect.poll(micEnabled).toBe(true);
  await page.evaluate(() => {
    const { event } = (window as unknown as { liveTest: { event(event: Record<string, unknown>): void } }).liveTest;
    event({ type: "session.input_transcript.delta", event_id: "user-1", delta: "Quiero ver", start_ms: 100, end_ms: 500 });
    event({ type: "session.output_transcript.delta", event_id: "assistant-1", delta: "Sí, puedes seguir nuestra conversación aquí.", start_ms: 800, end_ms: 1500 });
    event({ type: "session.input_transcript.delta", event_id: "user-2", delta: " lo que estás entendiendo.", start_ms: 500, end_ms: 1000 });
    event({ type: "session.input_transcript.delta", event_id: "user-2", delta: " lo que estás entendiendo.", start_ms: 500, end_ms: 1000 });
  });
  await expect(page.getByText("Quiero ver lo que estás entendiendo.")).toBeVisible();
  await expect(page.getByText("Sí, puedes seguir nuestra conversación aquí.")).toBeVisible();
  await expect.poll(() => history.size).toBe(1);
  await page.screenshot({ path: testInfo.outputPath("live-transcript.png"), fullPage: true });
  await page.locator(".composer").screenshot({ path: testInfo.outputPath("live-listening.png") });
  await page.getByRole("button", { name: "Pausar micrófono" }).click();
  await expect(page.getByText("Micrófono en pausa")).toBeVisible();
  await expect(page.getByRole("button", { name: "Dictar por voz" })).toBeDisabled();
  await expect.poll(micEnabled).toBe(false);
  allowSave();
  await page.locator(".composer").screenshot({ path: testInfo.outputPath("live-paused.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Reanudar micrófono" }).click();
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await expect.poll(micEnabled).toBe(true);
  expect(calls).toBe(1);
  await page.getByRole("button", { name: "Pausar micrófono" }).click();
  await page.getByRole("button", { name: "Terminar conversación de voz" }).click();
  await expect(page.getByRole("button", { name: "Conversar con GPT-Live-1" })).toBeEnabled();
  await expect(page.getByRole("button", { name: "Dictar por voz" })).toBeEnabled();
  await expect(page.locator(".live-voice-state")).toHaveCount(0);
  await expect(page.getByText("Gracias.", { exact: true })).toBeVisible();
  await expect.poll(() => [...history.values()][0]?.fragments.length).toBe(4);
  await page.reload();
  await expect(page.getByText("Quiero ver lo que estás entendiendo.")).toBeVisible();
  await expect(page.getByText("Gracias.", { exact: true })).toBeVisible();
  expect(calls).toBe(1);
});

test("shows a background voice result and two typed messages below the saved voice transcript", async ({ page }, testInfo) => {
  const callStart = Date.now() - 120_000;
  const items: { id: string; role: string; content: string; timestamp: number }[] = [
    { id: "voice-request", role: "user", content: "Solicitud delegada por voz", timestamp: (callStart + 10_000) / 1000 },
  ];
  let working = true;
  let submissions = 0;
  let interruptions = 0;
  await page.route("**/api/v1/sessions/*/live-transcripts**", (route) => route.fulfill({ json: { items: [{
    id: "saved-voice", createdAt: new Date(callStart).toISOString(), fragments: [
      { role: "user", text: "Revisa el encargo que te pedí. ".repeat(14), start: 0, end: 5000, order: 0 },
      { role: "assistant", text: "Claro, lo reviso.", start: 5000, end: 6000, order: 1 },
    ],
  }], nextCursor: null } }));
  await page.route("**/api/v1/sessions/session-e2e/messages", (route) => route.fulfill({ json: {
    items, sessionStatus: working ? "streaming" : "ready", activeOperation: working ? { operationId: "voice-operation", status: "streaming", acceptedAt: new Date(callStart + 10_000).toISOString() } : null,
  } }));
  await page.route("**/api/v1/sessions/session-e2e/interrupt", (route) => { interruptions += 1; return route.fulfill({ status: 204 }); });
  await page.route("**/api/v1/sessions/session-e2e/prompts", async (route) => {
    submissions += 1;
    items.push({ id: `text-${submissions}`, role: "user", content: route.request().postDataJSON().content, timestamp: Date.now() / 1000 });
    items.push({ id: `answer-${submissions}`, role: "assistant", content: `Respuesta al mensaje ${submissions}`, timestamp: Date.now() / 1000 });
    return route.fulfill({ json: { operationId: route.request().headers()["idempotency-key"], status: "completed" } });
  });
  await page.goto("/chats");
  await expect(page.getByRole("button", { name: "Detener", exact: true })).toBeVisible();
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  items.push({ id: "voice-result", role: "assistant", content: "Necesito tu confirmación para continuar.", timestamp: (callStart + 60_000) / 1000 });
  working = false;
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await expect(page.getByText("Necesito tu confirmación para continuar.")).toBeInViewport();
  await expect(page.getByRole("button", { name: "Detener", exact: true })).toHaveCount(0);
  for (const text of ["¿Ya quedó?", "Hola"]) {
    await page.getByRole("textbox", { name: "Mensaje a Newton…" }).fill(text);
    await page.getByRole("button", { name: "Enviar mensaje" }).click();
    await expect(page.getByText(text, { exact: true })).toBeInViewport();
    await expect(page.getByText(`Respuesta al mensaje ${submissions}`)).toBeInViewport();
  }
  expect(submissions).toBe(2);
  expect(interruptions).toBe(0);
  await page.reload();
  await expect(page.getByText("Respuesta al mensaje 2")).toBeInViewport();
  const order = await page.locator(".message-scroll").innerText();
  expect(order.indexOf("Claro, lo reviso.")).toBeLessThan(order.indexOf("Necesito tu confirmación"));
  expect(order.indexOf("Necesito tu confirmación")).toBeLessThan(order.indexOf("¿Ya quedó?"));
  expect(order.indexOf("¿Ya quedó?")).toBeLessThan(order.indexOf("Hola"));
  await page.screenshot({ path: testInfo.outputPath("voice-text-background-recovered.png"), fullPage: true });
});
