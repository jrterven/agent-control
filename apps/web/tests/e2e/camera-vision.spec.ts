import type { Page } from "@playwright/test";
import type { VisionAnalysisInput, VisionIntentInput, VisionObservation, VisionPreferences } from "@hermes-control/shared-types";
import { bootstrapData, expect, test } from "./fixtures";

test.use({ serviceWorkers: "block" });

type SmokeState = { cameras: number; microphones: number; callsClosed: number; commentary: string[]; attachments: string[] };
type SmokeWindow = Window & { cameraSmoke: { state(): SmokeState; delegate(question?: string): void } };
const mediaState = (page: Page) => page.evaluate(() => (window as SmokeWindow).cameraSmoke.state());

async function mockCamera(page: Page) {
  let preferences: VisionPreferences = { modelId: "gpt-5.6-luna", intervalSeconds: 5, configured: true };
  const analyses: VisionAnalysisInput[] = [];
  const intents: VisionIntentInput[] = [];
  const observations: VisionObservation[] = [];
  const prompts: { content: string; bytes: Buffer | null }[] = [];
  const calls: unknown[] = [];
  let keepWorking = false;
  let operationId = "";
  const history: { id: string; role: string; content: string; timestamp: number }[] = [];
  await page.route("**/api/v1/bootstrap", (route) => route.fulfill({ json: {
    ...bootstrapData, features: { dictation: { available: false }, live: { available: true, provider: "openai", modelId: "gpt-live-1" } },
  } }));
  await page.route("**/api/v1/vision/preferences", (route) => {
    if (route.request().method() === "PUT") preferences = { ...route.request().postDataJSON(), configured: true };
    return route.fulfill({ json: preferences });
  });
  await page.route("**/api/v1/sessions/*/vision/observations**", (route) => route.fulfill({ json: { items: [...observations].reverse(), nextCursor: null } }));
  await page.route("**/api/v1/sessions/*/vision/intent", (route) => {
    const payload: VisionIntentInput = route.request().postDataJSON();
    intents.push(payload);
    return route.fulfill({ json: { intent: /ves|mira|c[aá]mara|taza|cuaderno/i.test(payload.text) ? "visual" : "nonvisual", question: payload.text } });
  });
  await page.route("**/api/v1/sessions/*/vision/analyses", (route) => {
    const payload: VisionAnalysisInput = route.request().postDataJSON();
    analyses.push(payload);
    const observation: VisionObservation = {
      id: `00000000-0000-4000-8000-${String(analyses.length).padStart(12, "0")}`,
      sessionId: "session-e2e", activationId: payload.activationId, capturedAt: payload.capturedAt,
      createdAt: new Date().toISOString(), modelId: preferences.modelId, mode: payload.mode,
      summary: payload.mode === "continuous" ? "Hay una taza junto al cuaderno azul." : "Veo una taza roja sobre una mesa.",
      meaningfulChange: analyses.length > 1, sceneReset: !payload.previousImage, uncertainties: [],
    };
    const published = payload.mode === "on_demand" || analyses.length > 1;
    if (published) observations.push(observation);
    return route.fulfill({ json: { observation, published } });
  });
  await page.route("**/api/v1/sessions/*/live-transcripts**", (route) => route.fulfill(route.request().method() === "PUT" ? { status: 204 } : { json: { items: [], nextCursor: null } }));
  await page.route("**/api/v1/realtime/live-session", (route) => {
    calls.push(route.request().postDataJSON());
    return route.fulfill({ json: { session: { id: `live-camera-${calls.length}` }, transport: { type: "webrtc", sdp: "fake-answer" } } });
  });
  await page.route("**/api/v1/sessions/session-e2e/messages", (route) => route.fulfill({ json: {
    items: history, sessionStatus: keepWorking && prompts.length ? "streaming" : "ready",
    activeOperation: keepWorking && prompts.length ? { operationId, status: "streaming", acceptedAt: new Date().toISOString() } : null,
  } }));
  await page.route("**/api/v1/sessions/session-e2e/prompts*", (route) => {
    const multipart = route.request().headers()["content-type"]?.includes("multipart");
    const bytes = route.request().postDataBuffer();
    const content = multipart ? "Captura adjunta" : route.request().postDataJSON().content;
    prompts.push({ content, bytes });
    operationId = route.request().headers()["idempotency-key"];
    history.push({ id: `user-${prompts.length}`, role: "user", content, timestamp: Date.now() / 1000 });
    if (!keepWorking) history.push({ id: `answer-${prompts.length}`, role: "assistant", content: "La captura muestra una taza roja.", timestamp: Date.now() / 1000 });
    return route.fulfill({ json: { operationId, status: keepWorking ? "accepted" : "completed" } });
  });
  await page.route("**/api/v1/integrations/**", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/openai/voice")) return route.fulfill({ json: { voiceId: "marin" } });
    if (path.includes("/openai/profiles/")) return route.fulfill({ json: { voiceId: "marin", inherited: true } });
    if (path.endsWith("/openai")) return route.fulfill({ json: { configured: true, provider: "openai", modelId: "gpt-live-1" } });
    if (path.endsWith("/elevenlabs")) return route.fulfill({ json: { configured: false, provider: "elevenlabs", modelId: "scribe_v2_realtime" } });
    return route.fallback();
  });
  await page.addInitScript(() => {
    const cameras: MediaStreamTrack[] = [];
    const microphones: { enabled: boolean; readyState: string }[] = [];
    const commentary: string[] = [];
    const attachments: string[] = [];
    const channels: Channel[] = [];
    let callsClosed = 0;
    let delegations = 0;
    const nativeFetch = window.fetch.bind(window);
    window.fetch = async (input, init) => {
      if (String(input).includes("/prompts-with-attachments") && init?.body instanceof FormData) {
        for (const file of init.body.getAll("attachments")) if (file instanceof Blob) {
          attachments.push(await new Promise<string>((resolve, reject) => {
            const reader = new FileReader(); reader.onload = () => resolve(String(reader.result)); reader.onerror = reject; reader.readAsDataURL(file);
          }));
        }
      }
      return nativeFetch(input, init);
    };
    class Channel extends EventTarget {
      readyState = "open";
      emit(event: Record<string, unknown>) { this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(event) })); }
      send(data: string) {
        const event = JSON.parse(data);
        if (event.type === "session.commentary.append") commentary.push(event.content);
        if (event.type === "session.close") { callsClosed++; queueMicrotask(() => this.emit({ type: "session.closed", reason: "close_requested" })); }
      }
      close() { this.readyState = "closed"; }
    }
    class Peer extends EventTarget {
      channel = new Channel(); connectionState = "connected"; iceGatheringState = "complete";
      localDescription = { type: "offer", sdp: "fake-offer" };
      addTrack() {}
      createDataChannel() { channels.push(this.channel); return this.channel; }
      async createOffer() { return this.localDescription; }
      async setLocalDescription() {}
      async setRemoteDescription() { queueMicrotask(() => this.channel.emit({ type: "session.started" })); }
      close() {}
    }
    Object.defineProperty(window, "RTCPeerConnection", { configurable: true, value: Peer });
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: {
      enumerateDevices: async () => [
        { kind: "videoinput", deviceId: "camera-front", label: "Cámara frontal de prueba" },
        { kind: "videoinput", deviceId: "camera-back", label: "Cámara trasera de prueba" },
      ],
      getUserMedia: async (constraints: MediaStreamConstraints) => {
        if (constraints.video) {
          const canvas = document.createElement("canvas"); canvas.width = 640; canvas.height = 480;
          const context = canvas.getContext("2d")!;
          const draw = () => {
            context.fillStyle = "#dfc69c"; context.fillRect(0, 0, 640, 480);
            context.fillStyle = "#2563eb"; context.fillRect(60, 130, 170, 230);
            context.fillStyle = "#b91c1c"; context.beginPath(); context.arc(430, 260, 82, 0, Math.PI * 2); context.fill();
            context.fillStyle = "#111827"; context.font = "22px sans-serif"; context.fillText("CAMARA DE PRUEBA", 180, 50);
          };
          draw();
          const stream = canvas.captureStream(12); cameras.push(...stream.getTracks());
          const timer = setInterval(() => { if (stream.getTracks().every((track) => track.readyState === "ended")) clearInterval(timer); else draw(); }, 80);
          return stream;
        }
        const track = Object.assign(new EventTarget(), { enabled: true, muted: false, readyState: "live", stop() { this.readyState = "ended"; } });
        microphones.push(track);
        return { getTracks: () => [track], getAudioTracks: () => [track] };
      },
    } });
    Object.assign(window, { cameraSmoke: {
      state: () => ({ cameras: cameras.filter((track) => track.readyState === "live").length, microphones: microphones.filter((track) => track.readyState === "live").length, callsClosed, commentary, attachments }),
      delegate: (question = "Prepara un informe detallado.") => {
        const channel = channels.at(-1)!;
        delegations += 1;
        const offset = delegations * 2000;
        channel.emit({ type: "session.input_transcript.delta", event_id: `camera-task-text-${delegations}`, delta: question, start_ms: offset + 100, end_ms: offset + 1000 });
        channel.emit({ type: "session.delegation.created", event_id: `camera-task-${delegations}`, delegation: { id: `camera-report-task-${delegations}`, target: "client" }, offset_ms: offset + 1100 });
      },
    } });
  });
  return { analyses, intents, observations, prompts, calls, preferences: () => preferences, keepWorking: () => { keepWorking = true; } };
}

async function activate(page: Page, mode: "on_demand" | "continuous" = "on_demand") {
  await page.getByRole("button", { name: "Cámara", exact: true }).click();
  await expect(page.getByText("Preguntar sobre la cámara", { exact: true })).toBeVisible();
  if (mode === "continuous") {
    await page.getByRole("button", { name: "Cambiar modo", exact: true }).click();
    await expect(page.getByText("Elegir modo de cámara")).toBeVisible();
    await page.getByRole("button", { name: /Seguimiento automático Analiza al activar/ }).click();
  }
  expect((await mediaState(page)).cameras).toBe(0);
  await page.getByRole("button", { name: "Activar cámara", exact: true }).click();
  await expect(page.getByText("Cámara activa", { exact: true })).toBeVisible();
  await expect.poll(async () => (await mediaState(page)).cameras).toBe(1);
}

test("defaults to questions, waits silently after activation, and looks only when requested", async ({ page }, testInfo) => {
  const mock = await mockCamera(page);
  await page.clock.install();
  await page.goto("/chats");
  await expect(page.getByRole("button", { name: "Cámara", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Cámara", exact: true }).click();
  await expect(page.getByText("Preguntar sobre la cámara", { exact: true })).toBeVisible();
  await expect(page.getByText("Elegir modo de cámara")).toHaveCount(0);
  expect((await mediaState(page)).cameras).toBe(0);
  await page.screenshot({ path: testInfo.outputPath("camera-setup.png"), fullPage: true });
  await page.getByRole("button", { name: "Activar cámara", exact: true }).click();
  await expect(page.getByText("Cámara activa", { exact: true })).toBeVisible();
  await expect(page.locator(".camera-vision__preview video")).toBeVisible();
  await page.clock.fastForward(30_000);
  expect(mock.analyses).toHaveLength(0);
  expect(mock.intents).toHaveLength(0);
  expect(mock.prompts).toHaveLength(0);
  expect((await mediaState(page)).commentary).toHaveLength(0);
  await expect(page.getByRole("article", { name: "Contexto visual" })).toHaveCount(0);
  await page.getByRole("button", { name: "Mirar ahora", exact: true }).click();
  await expect.poll(() => mock.analyses.length).toBe(1);
  await expect.poll(() => mock.prompts.length).toBe(1);
  await expect(page.getByRole("article", { name: "Contexto visual" })).toContainText("Veo una taza roja sobre una mesa.");
  expect(mock.analyses[0]).toMatchObject({ mode: "on_demand", question: "¿Qué ves?", image: expect.stringMatching(/^data:image\/jpeg;base64,/) });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("camera-on-demand.png"), fullPage: true });
  await page.getByRole("button", { name: "Apagar cámara", exact: true }).first().click();
  await expect.poll(async () => (await mediaState(page)).cameras).toBe(0);
  await expect(page.locator(".camera-vision__preview")).toHaveCount(0);
});

test("answers a custom written camera question with one fresh capture and one agent task", async ({ page }, testInfo) => {
  const mock = await mockCamera(page);
  await page.clock.install();
  await page.goto("/chats");
  await activate(page);
  await page.clock.fastForward(30_000);
  expect(mock.analyses).toHaveLength(0);
  expect(mock.intents).toHaveLength(0);
  expect(mock.prompts).toHaveLength(0);
  expect((await mediaState(page)).commentary).toHaveLength(0);
  const question = "¿De qué color es la taza que tengo enfrente?";
  await page.getByRole("textbox", { name: "Mensaje a Newton…" }).fill(question);
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect.poll(() => mock.prompts.length).toBe(1);
  expect(mock.intents).toHaveLength(1);
  expect(mock.intents[0]).toMatchObject({ text: question });
  expect(mock.intents[0]).not.toHaveProperty("image");
  expect(mock.analyses).toHaveLength(1);
  expect(mock.analyses[0]).toMatchObject({ mode: "on_demand", question, image: expect.stringMatching(/^data:image\/jpeg;base64,/) });
  expect(mock.prompts[0].content).toBe(question);
  await expect(page.getByRole("article", { name: "Contexto visual" })).toHaveCount(1);
  await page.clock.fastForward(30_000);
  expect(mock.analyses).toHaveLength(1);
  expect(mock.prompts).toHaveLength(1);
  expect((await mediaState(page)).commentary).toHaveLength(0);
  expect((await mediaState(page)).cameras).toBe(1);
  await page.screenshot({ path: testInfo.outputPath("camera-written-question.png"), fullPage: true });
});

test("waits for a custom Live question before capturing and delegating exactly once", async ({ page }, testInfo) => {
  const mock = await mockCamera(page);
  await page.clock.install();
  await page.goto("/chats");
  await page.getByRole("button", { name: "Conversar con GPT-Live-1" }).click();
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await activate(page);
  await page.clock.fastForward(30_000);
  expect(mock.analyses).toHaveLength(0);
  expect(mock.intents).toHaveLength(0);
  expect(mock.prompts).toHaveLength(0);
  expect((await mediaState(page)).commentary).toHaveLength(0);
  const question = "¿La taza está a la derecha o a la izquierda del cuaderno?";
  await page.evaluate((text) => (window as SmokeWindow).cameraSmoke.delegate(text), question);
  await page.clock.runFor(800);
  await expect.poll(() => mock.prompts.length).toBe(1);
  expect(mock.intents).toHaveLength(1);
  expect(mock.intents[0]).toMatchObject({ text: question });
  expect(mock.analyses).toHaveLength(1);
  expect(mock.analyses[0]).toMatchObject({ mode: "on_demand", question });
  expect(mock.prompts[0].content).toContain(question);
  await expect.poll(async () => (await mediaState(page)).commentary.join(" ")).toContain("Backend agent result");
  await page.clock.fastForward(30_000);
  expect(mock.analyses).toHaveLength(1);
  expect(mock.prompts).toHaveLength(1);
  expect(mock.calls).toHaveLength(1);
  expect((await mediaState(page)).commentary.some((content) => content.startsWith("Camera evidence"))).toBe(false);
  expect((await mediaState(page)).cameras).toBe(1);
  await page.screenshot({ path: testInfo.outputPath("camera-live-question.png"), fullPage: true });
});

test("continuous observation preserves the draft and attaches exactly the selected analyzed frame", async ({ page }, testInfo) => {
  const mock = await mockCamera(page);
  await page.goto("/chats");
  const composer = page.getByRole("textbox", { name: "Mensaje a Newton…" });
  await composer.fill("Conserva este borrador hasta enviarlo.");
  await activate(page, "continuous");
  await expect.poll(() => mock.analyses.length).toBe(1);
  await expect(page.getByRole("button", { name: "Adjuntar captura analizada" })).toBeEnabled();
  await expect(composer).toHaveValue("Conserva este borrador hasta enviarlo.");
  expect(mock.prompts).toHaveLength(0);
  await page.getByRole("button", { name: "Adjuntar captura analizada" }).click();
  await expect(page.locator(".composer-attachment")).toHaveCount(1);
  await expect(composer).toHaveValue("Conserva este borrador hasta enviarlo.");
  expect(mock.prompts).toHaveLength(0);
  await page.getByRole("button", { name: "Apagar cámara", exact: true }).first().click();
  await expect.poll(async () => (await mediaState(page)).cameras).toBe(0);
  await expect(page.locator(".composer-attachment")).toHaveCount(1);
  await page.screenshot({ path: testInfo.outputPath("camera-selected-attachment.png"), fullPage: true });
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect.poll(() => mock.prompts.length).toBe(1);
  // WebKit's network interception omits binary multipart bodies; inspect the
  // actual File handed to fetch so byte equality is checked in every browser.
  expect((await mediaState(page)).attachments).toEqual([mock.analyses[0].image]);
  expect(mock.analyses).toHaveLength(1);
});

test("keeps the camera through internal Live suspension, then releases it on explicit hangup", async ({ page }, testInfo) => {
  const mock = await mockCamera(page);
  mock.keepWorking();
  await page.clock.install();
  await page.goto("/chats");
  await page.getByRole("button", { name: "Conversar con GPT-Live-1" }).click();
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await activate(page, "continuous");
  await expect.poll(() => mock.analyses.length).toBe(1);
  await page.evaluate(() => (window as SmokeWindow).cameraSmoke.delegate());
  await page.clock.runFor(800);
  await expect.poll(() => mock.prompts.length).toBe(1);
  await page.clock.fastForward(16_000);
  await expect(page.getByText("Esperando respuesta…", { exact: true })).toBeVisible();
  await expect.poll(async () => { const state = await mediaState(page); return { cameras: state.cameras, microphones: state.microphones, callsClosed: state.callsClosed }; }).toEqual({ cameras: 1, microphones: 0, callsClosed: 1 });
  await expect(page.getByText("Cámara activa", { exact: true })).toBeVisible();
  const commentaryAfterClose = (await mediaState(page)).commentary.length;
  await page.clock.fastForward(6_000);
  await expect.poll(() => mock.analyses.length).toBeGreaterThan(1);
  expect((await mediaState(page)).commentary).toHaveLength(commentaryAfterClose);
  expect(mock.calls).toHaveLength(1);
  expect(mock.prompts).toHaveLength(1);
  await page.screenshot({ path: testInfo.outputPath("camera-live-suspended.png"), fullPage: true });
  await page.getByRole("button", { name: "Terminar conversación de voz" }).click();
  await expect.poll(async () => (await mediaState(page)).cameras).toBe(0);
  await expect(page.locator(".camera-vision__preview")).toHaveCount(0);
});

test("announces meaningful continuous observations only into an active Live call without starting an agent task", async ({ page }) => {
  const mock = await mockCamera(page);
  await page.clock.install();
  await page.goto("/chats");
  await page.getByRole("button", { name: "Conversar con GPT-Live-1" }).click();
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await activate(page, "continuous");
  await expect.poll(() => mock.analyses.length).toBe(1);
  expect((await mediaState(page)).commentary).toHaveLength(0);
  await page.clock.fastForward(6_000);
  await expect.poll(async () => (await mediaState(page)).commentary.join(" ")).toContain("Hay una taza junto al cuaderno azul.");
  expect(mock.prompts).toHaveLength(0);
  expect(mock.calls).toHaveLength(1);
});

test("saves the camera model and interval in Preferences without opening media or inference", async ({ page }, testInfo) => {
  const mock = await mockCamera(page);
  await page.goto("/settings");
  const panel = page.locator("#camera-preferences");
  await panel.scrollIntoViewIfNeeded();
  await expect(panel.getByRole("combobox", { name: "Modelo de la cámara" })).toBeEnabled();
  await panel.getByRole("combobox", { name: "Modelo de la cámara" }).selectOption("gpt-5.6-sol");
  await panel.getByRole("combobox", { name: "Intervalo de observación continua" }).selectOption("10");
  await panel.getByRole("button", { name: "Guardar preferencias de cámara" }).click();
  await expect(panel.getByText(/Preferencias de cámara guardadas/)).toBeVisible();
  expect(mock.preferences()).toEqual({ modelId: "gpt-5.6-sol", intervalSeconds: 10, configured: true });
  expect(mock.analyses).toHaveLength(0); expect(mock.calls).toHaveLength(0);
  expect((await mediaState(page)).cameras).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel.screenshot({ path: testInfo.outputPath("camera-preferences.png") });
});
