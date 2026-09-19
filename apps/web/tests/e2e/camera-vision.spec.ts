import type { Page } from "@playwright/test";
import type { VisionAnalysisInput, VisionIntentInput, VisionObservation, VisionPreferences } from "@hermes-control/shared-types";
import { bootstrapData, expect, test } from "./fixtures";

test.use({ serviceWorkers: "block" });

type SmokeState = {
  cameras: number; endedCameras: number; cameraStarts: string[]; microphones: number;
  callsClosed: number; commentary: string[]; attachments: string[]; remoteAudioTracks: number;
};
type SmokeWindow = Window & { cameraSmoke: {
  state(): SmokeState; speak(text: string): number; delegate(question?: string): void;
  delegateTurn(turn: number, id?: string): void; scene(value: "cup" | "notebook"): void;
} };
const mediaState = (page: Page) => page.evaluate(() => (window as SmokeWindow).cameraSmoke.state());
const cupSummary = "Veo una taza roja sobre una mesa.";
const notebookSummary = "Veo un cuaderno azul y ya no hay taza.";
const cameraAnswer = (summary: string) => `Según la captura: ${summary}`;

async function mockCamera(page: Page) {
  let preferences: VisionPreferences = { modelId: "gpt-5.6-luna", intervalSeconds: 5, configured: true };
  const analyses: VisionAnalysisInput[] = [];
  const intents: VisionIntentInput[] = [];
  const observations: VisionObservation[] = [];
  const prompts: { content: string; bytes: Buffer | null }[] = [];
  const calls: unknown[] = [];
  let summary = cupSummary;
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
    return route.fulfill({ json: { intent: /ves|mira|c[aá]mara|taza|cuaderno|ahora/i.test(payload.text) ? "visual" : "nonvisual", question: payload.text } });
  });
  await page.route("**/api/v1/sessions/*/vision/analyses", (route) => {
    const payload: VisionAnalysisInput = route.request().postDataJSON();
    analyses.push(payload);
    const observation: VisionObservation = {
      id: `00000000-0000-4000-8000-${String(analyses.length).padStart(12, "0")}`,
      sessionId: "session-e2e", activationId: payload.activationId, capturedAt: payload.capturedAt,
      createdAt: new Date().toISOString(), modelId: preferences.modelId, mode: payload.mode,
      summary: payload.mode === "continuous" ? "Hay una taza junto al cuaderno azul." : summary,
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
    if (!keepWorking) history.push({ id: `answer-${prompts.length}`, role: "assistant",
      content: cameraAnswer(observations.at(-1)?.summary ?? "No hay una observación visual disponible."), timestamp: Date.now() / 1000 });
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
    const cameraStarts: string[] = [];
    const microphones: { enabled: boolean; readyState: string }[] = [];
    const commentary: string[] = [];
    const attachments: string[] = [];
    const channels: Channel[] = [];
    const remoteContexts: AudioContext[] = [];
    const remoteTracks: MediaStreamTrack[] = [];
    const renderers: (() => void)[] = [];
    let callsClosed = 0;
    let utterances = 0;
    let delegationEvents = 0;
    let scene: "cup" | "notebook" = "cup";
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
      remoteContext = remoteContexts.shift()!;
      remoteStream?: MediaStream;
      remoteTone?: OscillatorNode;
      addTrack() {}
      createDataChannel() { channels.push(this.channel); return this.channel; }
      async createOffer() { return this.localDescription; }
      async setLocalDescription() {}
      async setRemoteDescription() {
        // Feed the real playback gate a live, silent remote audio track. Its
        // AudioContext measures actual silence; this fixture never fakes a
        // gate acknowledgement or an audio clock.
        const output = this.remoteContext.createMediaStreamDestination();
        const gain = this.remoteContext.createGain(); gain.gain.value = 0;
        this.remoteTone = this.remoteContext.createOscillator();
        this.remoteTone.connect(gain).connect(output); this.remoteTone.start();
        this.remoteStream = output.stream;
        const track = output.stream.getAudioTracks()[0]; remoteTracks.push(track);
        const event = new Event("track");
        Object.defineProperties(event, { track: { value: track }, streams: { value: [output.stream] } });
        this.dispatchEvent(event);
        queueMicrotask(() => this.channel.emit({ type: "session.started" }));
      }
      close() {
        this.remoteTone?.stop();
        this.remoteStream?.getTracks().forEach((track) => track.stop());
        void this.remoteContext.close().catch(() => {});
      }
    }
    Object.defineProperty(window, "RTCPeerConnection", { configurable: true, value: Peer });
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: {
      enumerateDevices: async () => [
        { kind: "videoinput", deviceId: "camera-front", label: "Cámara frontal de prueba" },
        { kind: "videoinput", deviceId: "camera-back", label: "Cámara trasera de prueba" },
      ],
      getUserMedia: async (constraints: MediaStreamConstraints) => {
        if (constraints.video) {
          const settings = typeof constraints.video === "object" ? constraints.video : {};
          const device = typeof settings.deviceId === "object" && "exact" in settings.deviceId
            ? String(settings.deviceId.exact)
            : typeof settings.facingMode === "object" && "ideal" in settings.facingMode && settings.facingMode.ideal === "environment"
              ? "camera-back" : "camera-front";
          cameraStarts.push(device);
          const canvas = document.createElement("canvas"); canvas.width = 640; canvas.height = 480;
          const context = canvas.getContext("2d")!;
          const draw = () => {
            context.fillStyle = "#dfc69c"; context.fillRect(0, 0, 640, 480);
            context.fillStyle = "#2563eb"; context.fillRect(60, 130, 170, 230);
            if (scene === "cup") {
              context.fillStyle = "#b91c1c"; context.beginPath(); context.arc(430, 260, 82, 0, Math.PI * 2); context.fill();
            }
            context.fillStyle = "#111827"; context.font = "22px sans-serif"; context.fillText("CAMARA DE PRUEBA", 180, 50);
          };
          draw();
          renderers.push(draw);
          const stream = canvas.captureStream(12); cameras.push(...stream.getTracks());
          stream.getVideoTracks().forEach((track) => {
            const originalSettings = track.getSettings.bind(track);
            track.getSettings = () => ({ ...originalSettings(), deviceId: device });
          });
          const timer = setInterval(() => { if (stream.getTracks().every((track) => track.readyState === "ended")) clearInterval(timer); else draw(); }, 80);
          return stream;
        }
        // Construct/resume on the same user gesture as microphone permission,
        // including WebKit's stricter audio activation behavior.
        const remoteContext = new AudioContext(); remoteContexts.push(remoteContext);
        void remoteContext.resume();
        const track = Object.assign(new EventTarget(), { enabled: true, muted: false, readyState: "live", stop() { this.readyState = "ended"; } });
        microphones.push(track);
        return { getTracks: () => [track], getAudioTracks: () => [track] };
      },
    } });
    const speak = (text: string) => {
      const turn = ++utterances;
      channels.at(-1)!.emit({ type: "session.input_transcript.delta", event_id: `camera-task-text-${turn}`,
        delta: text, start_ms: turn * 2000 + 100, end_ms: turn * 2000 + 1000 });
      return turn;
    };
    const delegateTurn = (turn: number, id = `camera-report-task-${turn}`) => {
      channels.at(-1)!.emit({ type: "session.delegation.created", event_id: `camera-task-${++delegationEvents}`,
        delegation: { id, target: "client" }, offset_ms: turn * 2000 + 1100 });
    };
    Object.assign(window, { cameraSmoke: {
      state: () => ({
        cameras: cameras.filter((track) => track.readyState === "live").length,
        endedCameras: cameras.filter((track) => track.readyState === "ended").length,
        cameraStarts, microphones: microphones.filter((track) => track.readyState === "live").length,
        callsClosed, commentary, attachments, remoteAudioTracks: remoteTracks.filter((track) => track.readyState === "live").length,
      }),
      speak, delegateTurn,
      delegate: (question = "Prepara un informe detallado.") => delegateTurn(speak(question)),
      scene: (value: "cup" | "notebook") => { scene = value; renderers.forEach((draw) => draw()); },
    } });
  });
  return { analyses, intents, observations, prompts, calls, preferences: () => preferences, keepWorking: () => { keepWorking = true; },
    scene: async (value: "cup" | "notebook") => {
      summary = value === "cup" ? cupSummary : notebookSummary;
      await page.evaluate((next) => (window as SmokeWindow).cameraSmoke.scene(next), value);
    } };
}

async function activate(page: Page) {
  expect((await mediaState(page)).cameras).toBe(0);
  const trigger = page.getByRole("button", { name: "Activar cámara", exact: true });
  await expect(trigger).toBeEnabled();
  await trigger.click();
  await expect(page.getByText("Cámara activa", { exact: true })).toBeVisible();
  await expect.poll(async () => (await mediaState(page)).cameras).toBe(1);
  await expect(page.locator(".camera-vision__trigger")).toHaveAttribute("aria-pressed", "true");
  await expect(page.locator(".composer-wrap .camera-vision__inline")).toBeVisible();
  await expect(page.getByRole("region", { name: "Vista previa de la cámara" }).locator("video")).toBeVisible();
  await expect(page.locator(".camera-vision__panel, .camera-vision__menu")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Mirar ahora|Cambiar modo/ })).toHaveCount(0);
}

async function stopCamera(page: Page) {
  await page.locator(".camera-vision__trigger").click();
  await expect.poll(async () => (await mediaState(page)).cameras).toBe(0);
  await expect(page.getByRole("button", { name: "Activar cámara", exact: true })).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator(".camera-vision__inline")).toHaveCount(0);
}

test("the eye turns on an inline thumbnail and waits silently while normal chat remains available", async ({ page }, testInfo) => {
  const mock = await mockCamera(page);
  await page.clock.install();
  await page.goto("/chats");
  const composer = page.getByRole("textbox", { name: "Mensaje a Newton…" });
  const question = "Escribe un poema corto sobre el invierno.";
  await composer.fill(question);
  await activate(page);
  await expect(composer).toHaveValue(question);
  await expect(composer).toBeEditable();
  await page.clock.fastForward(30_000);
  expect(mock.analyses).toHaveLength(0);
  expect(mock.intents).toHaveLength(0);
  expect(mock.prompts).toHaveLength(0);
  expect((await mediaState(page)).commentary).toHaveLength(0);
  await expect(page.getByRole("article", { name: "Contexto visual" })).toHaveCount(0);
  const thumbnail = await page.locator(".camera-vision__preview").boundingBox();
  expect(thumbnail?.width).toBeLessThan(150);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath("camera-inline-idle.png"), fullPage: true });
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect.poll(() => mock.prompts.length).toBe(1);
  expect(mock.prompts[0].content).toBe(question);
  expect(mock.intents).toHaveLength(1);
  expect(mock.intents[0]).toMatchObject({ text: question });
  expect(mock.intents[0]).not.toHaveProperty("image");
  expect(mock.analyses).toHaveLength(0);
  expect((await mediaState(page)).cameras).toBe(1);
  await stopCamera(page);
  expect((await mediaState(page)).endedCameras).toBe(1);
});

test("answers a normal written camera question with one fresh capture and one agent answer", async ({ page }, testInfo) => {
  const mock = await mockCamera(page);
  await page.clock.install();
  await page.goto("/chats");
  await activate(page);
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
  await expect(page.getByText(cameraAnswer(cupSummary), { exact: true })).toBeVisible();
  await expect(page.getByRole("article", { name: "Contexto visual" })).toHaveCount(0);
  await expect(page.getByText("Veo una taza roja sobre una mesa.", { exact: true })).toHaveCount(0);
  await page.clock.fastForward(30_000);
  expect(mock.analyses).toHaveLength(1);
  expect(mock.prompts).toHaveLength(1);
  expect((await mediaState(page)).commentary).toHaveLength(0);
  expect((await mediaState(page)).cameras).toBe(1);
  await page.screenshot({ path: testInfo.outputPath("camera-written-question.png"), fullPage: true });
  // Persisted camera evidence must not become a second unsolicited answer on reload.
  await page.reload();
  await expect(page.getByRole("button", { name: "Activar cámara", exact: true })).toBeEnabled();
  await expect(page.getByText(cameraAnswer(cupSummary), { exact: true })).toBeVisible();
  await expect(page.getByRole("article", { name: "Contexto visual" })).toHaveCount(0);
  await expect(page.getByText("Veo una taza roja sobre una mesa.", { exact: true })).toHaveCount(0);
  expect(mock.analyses).toHaveLength(1);
  expect(mock.prompts).toHaveLength(1);
  expect((await mediaState(page)).cameras).toBe(0);
});

test("captures again for a visual follow-up and supplies the preceding analyzed image", async ({ page }) => {
  const mock = await mockCamera(page);
  await page.clock.install();
  await page.goto("/chats");
  await activate(page);
  const composer = page.getByRole("textbox", { name: "Mensaje a Newton…" });
  await composer.fill("¿Qué ves en la cámara?");
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect.poll(() => mock.prompts.length).toBe(1);
  await expect(page.getByText(cameraAnswer(cupSummary), { exact: true })).toBeVisible();
  await mock.scene("notebook");
  await page.clock.fastForward(3_000);
  await composer.fill("¿Y ahora?");
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect.poll(() => mock.prompts.length).toBe(2);
  expect(mock.intents).toHaveLength(2);
  expect(mock.intents[1]).toMatchObject({ text: "¿Y ahora?", recentContext: "Veo una taza roja sobre una mesa." });
  expect(mock.analyses).toHaveLength(2);
  expect(mock.analyses[1]).toMatchObject({ mode: "on_demand", question: "¿Y ahora?", previousImage: mock.analyses[0].image });
  expect(mock.analyses[1].requestId).not.toBe(mock.analyses[0].requestId);
  expect(mock.analyses[1].capturedAt).not.toBe(mock.analyses[0].capturedAt);
  expect(mock.analyses[1].image).not.toBe(mock.analyses[0].image);
  expect(mock.prompts[1].content).toBe("¿Y ahora?");
  await expect(page.getByText(cameraAnswer(notebookSummary), { exact: true })).toBeVisible();
  await expect(page.getByRole("article", { name: "Contexto visual" })).toHaveCount(0);
  await stopCamera(page);
});

test("switches cameras from the thumbnail, preserves the draft, and releases the previous tracks", async ({ page }, testInfo) => {
  const mock = await mockCamera(page);
  await page.goto("/chats");
  const composer = page.getByRole("textbox", { name: "Mensaje a Newton…" });
  const draft = "Conserva este borrador mientras cambio de cámara.";
  await composer.fill(draft);
  await activate(page);
  const initial = await mediaState(page);
  const nextCamera = initial.cameraStarts[0] === "camera-front" ? "camera-back" : "camera-front";
  const selector = page.getByRole("combobox", { name: "Cámara del dispositivo" });
  await expect(selector.locator(`option[value="${nextCamera}"]`)).toHaveCount(1);
  await selector.selectOption(nextCamera);
  await expect(page.getByText("Cámara activa", { exact: true })).toBeVisible();
  await expect.poll(async () => {
    const state = await mediaState(page);
    return { cameras: state.cameras, ended: state.endedCameras, selected: state.cameraStarts.at(-1) };
  }).toEqual({ cameras: 1, ended: 1, selected: nextCamera });
  await expect(selector).toHaveValue(nextCamera);
  await expect(composer).toHaveValue(draft);
  expect(mock.analyses).toHaveLength(0);
  expect(mock.intents).toHaveLength(0);
  expect(mock.prompts).toHaveLength(0);
  await page.screenshot({ path: testInfo.outputPath("camera-inline-switch.png"), fullPage: true });
  await stopCamera(page);
  expect((await mediaState(page)).endedCameras).toBe(2);
  await expect(composer).toHaveValue(draft);
});

test("a Live transcript alone triggers a fresh capture and one verified agent answer", async ({ page }, testInfo) => {
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
  await page.evaluate((text) => (window as SmokeWindow).cameraSmoke.speak(text), question);
  await page.clock.runFor(500);
  expect(mock.intents).toHaveLength(0);
  await page.clock.runFor(400);
  await expect.poll(() => mock.prompts.length).toBe(1);
  expect(mock.intents).toHaveLength(1);
  expect(mock.intents[0]).toMatchObject({ text: question });
  expect(mock.analyses).toHaveLength(1);
  expect(mock.analyses[0]).toMatchObject({ mode: "on_demand", question });
  expect(mock.prompts[0].content).toContain(question);
  expect(mock.prompts[0].content).toContain(cupSummary);
  await expect.poll(async () => (await mediaState(page)).commentary.join(" ")).toContain(cameraAnswer(cupSummary));
  await expect(page.locator(".live-transcript").filter({ hasText: question }).first()).toBeVisible();
  expect((await page.locator(".live-transcript").allTextContents()).join("\n"))
    .not.toMatch(/Current camera evidence|"capturedAt"|"summary"|"uncertainties"/);
  expect((await mediaState(page)).remoteAudioTracks).toBe(1);
  await page.clock.fastForward(30_000);
  expect(mock.analyses).toHaveLength(1);
  expect(mock.prompts).toHaveLength(1);
  expect(mock.calls).toHaveLength(1);
  expect((await mediaState(page)).commentary.some((content) => content.startsWith("Camera evidence"))).toBe(false);
  expect((await mediaState(page)).cameras).toBe(1);
  await expect(page.getByRole("article", { name: "Contexto visual" })).toHaveCount(0);
  await expect(page.getByText("Veo una taza roja sobre una mesa.", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: testInfo.outputPath("camera-live-question.png"), fullPage: true });
});

test("a spoken visual follow-up analyzes the changed scene instead of repeating the prior observation", async ({ page }) => {
  const mock = await mockCamera(page);
  await page.clock.install();
  await page.goto("/chats");
  await page.getByRole("button", { name: "Conversar con GPT-Live-1" }).click();
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await activate(page);
  await page.evaluate(() => (window as SmokeWindow).cameraSmoke.speak("¿Qué ves en la cámara?"));
  await page.clock.runFor(900);
  await expect.poll(async () => (await mediaState(page)).commentary.join(" ")).toContain(cameraAnswer(cupSummary));
  expect(mock.prompts).toHaveLength(1);
  const beforeFollowup = (await mediaState(page)).commentary.length;
  await mock.scene("notebook");
  await page.clock.runFor(3_000);
  await page.evaluate(() => (window as SmokeWindow).cameraSmoke.speak("¿Y ahora?"));
  await page.clock.runFor(900);
  await expect.poll(() => mock.prompts.length).toBe(2);
  expect(mock.intents).toHaveLength(2);
  expect(mock.intents[1]).toMatchObject({ text: "¿Y ahora?", recentContext: cupSummary });
  expect(mock.analyses).toHaveLength(2);
  expect(mock.analyses[1]).toMatchObject({ mode: "on_demand", question: "¿Y ahora?", previousImage: mock.analyses[0].image });
  expect(mock.analyses[1].image).not.toBe(mock.analyses[0].image);
  expect(mock.analyses[1].requestId).not.toBe(mock.analyses[0].requestId);
  expect(mock.analyses[1].capturedAt).not.toBe(mock.analyses[0].capturedAt);
  await expect.poll(async () => (await mediaState(page)).commentary.slice(beforeFollowup).join(" ")).toContain(cameraAnswer(notebookSummary));
  expect((await mediaState(page)).commentary.slice(beforeFollowup).join(" ")).not.toContain(cameraAnswer(cupSummary));
  await expect(page.getByText(cameraAnswer(notebookSummary), { exact: true })).toBeVisible();
  expect(mock.calls).toHaveLength(1);
});

test("early and late provider delegations share the already processed spoken visual turn", async ({ page }) => {
  const mock = await mockCamera(page);
  await page.clock.install();
  await page.goto("/chats");
  await page.getByRole("button", { name: "Conversar con GPT-Live-1" }).click();
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await activate(page);
  const question = "Mira la taza de la cámara.";
  const turn = await page.evaluate((text) => (window as SmokeWindow).cameraSmoke.speak(text), question);
  // Provider delegation can precede the transcript debounce and can repeat
  // with a new event or delegation id after the local request has completed.
  await page.evaluate((id) => (window as SmokeWindow).cameraSmoke.delegateTurn(id), turn);
  await page.clock.runFor(900);
  await expect.poll(async () => (await mediaState(page)).commentary.join(" ")).toContain(cameraAnswer(cupSummary));
  await page.evaluate((id) => {
    (window as SmokeWindow).cameraSmoke.delegateTurn(id);
    (window as SmokeWindow).cameraSmoke.delegateTurn(id, "duplicate-late-provider-id");
  }, turn);
  await page.clock.runFor(3_000);
  expect(mock.intents).toHaveLength(1);
  expect(mock.analyses).toHaveLength(1);
  expect(mock.prompts).toHaveLength(1);
  expect(mock.prompts[0].content).toContain(question);
  expect((await mediaState(page)).commentary.filter((content) => content.includes("Backend agent result"))).toHaveLength(1);
  expect(mock.calls).toHaveLength(1);
});

test("a nonvisual Live transcript is classified without capturing or submitting an agent task", async ({ page }) => {
  const mock = await mockCamera(page);
  await page.clock.install();
  await page.goto("/chats");
  await page.getByRole("button", { name: "Conversar con GPT-Live-1" }).click();
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await activate(page);
  const question = "Cuéntame un chiste corto.";
  await page.evaluate((text) => (window as SmokeWindow).cameraSmoke.speak(text), question);
  await page.clock.runFor(900);
  await expect.poll(() => mock.intents.length).toBe(1);
  expect(mock.intents[0]).toMatchObject({ text: question });
  expect(mock.intents[0]).not.toHaveProperty("image");
  await page.clock.runFor(3_000);
  expect(mock.analyses).toHaveLength(0);
  expect(mock.prompts).toHaveLength(0);
  expect((await mediaState(page)).commentary.some((content) => content.includes("Backend agent result"))).toBe(false);
  expect((await mediaState(page)).cameras).toBe(1);
  expect(mock.calls).toHaveLength(1);
});

test("turning the camera off cancels a pending spoken visual classification without ending Live", async ({ page }) => {
  const mock = await mockCamera(page);
  let releaseIntent!: () => void;
  const classifier = new Promise<void>((resolve) => { releaseIntent = resolve; });
  await page.route("**/api/v1/sessions/*/vision/intent", async (route) => {
    const payload: VisionIntentInput = route.request().postDataJSON();
    mock.intents.push(payload);
    await classifier;
    await route.fulfill({ json: { intent: "visual", question: payload.text } }).catch(() => {});
  });
  await page.clock.install();
  await page.goto("/chats");
  await page.getByRole("button", { name: "Conversar con GPT-Live-1" }).click();
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await activate(page);
  await page.evaluate(() => (window as SmokeWindow).cameraSmoke.speak("¿Qué ves en la cámara?"));
  await page.clock.runFor(900);
  await expect.poll(() => mock.intents.length).toBe(1);
  await stopCamera(page);
  releaseIntent();
  await page.clock.runFor(3_000);
  expect(mock.analyses).toHaveLength(0);
  expect(mock.prompts).toHaveLength(0);
  expect((await mediaState(page)).commentary.some((content) => content.includes("Backend agent result"))).toBe(false);
  expect((await mediaState(page)).microphones).toBe(1);
  expect((await mediaState(page)).callsClosed).toBe(0);
  expect(mock.calls).toHaveLength(1);
  await expect(page.getByRole("button", { name: "Terminar conversación de voz" })).toBeVisible();
});

test("attaches exactly the last analyzed frame without changing the current draft or analyzing again", async ({ page }, testInfo) => {
  const mock = await mockCamera(page);
  await page.goto("/chats");
  await activate(page);
  const composer = page.getByRole("textbox", { name: "Mensaje a Newton…" });
  await composer.fill("¿Qué ves?");
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect.poll(() => mock.prompts.length).toBe(1);
  await expect(page.getByText(cameraAnswer(cupSummary), { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Adjuntar captura analizada" })).toBeEnabled();
  await composer.fill("Conserva este borrador hasta enviarlo.");
  await page.getByRole("button", { name: "Adjuntar captura analizada" }).click();
  await expect(page.locator(".composer-attachment")).toHaveCount(1);
  await expect(composer).toHaveValue("Conserva este borrador hasta enviarlo.");
  expect(mock.prompts).toHaveLength(1);
  expect(mock.analyses).toHaveLength(1);
  await stopCamera(page);
  await expect(page.locator(".composer-attachment")).toHaveCount(1);
  await page.screenshot({ path: testInfo.outputPath("camera-selected-attachment.png"), fullPage: true });
  await page.getByRole("button", { name: "Enviar mensaje" }).click();
  await expect.poll(() => mock.prompts.length).toBe(2);
  // WebKit's network interception omits binary multipart bodies; inspect the
  // actual File handed to fetch so byte equality is checked in every browser.
  expect((await mediaState(page)).attachments).toEqual([mock.analyses[0].image]);
  expect(mock.analyses).toHaveLength(1);
});

test("takes an explicitly attached photo without analysis or an unsolicited agent task", async ({ page }) => {
  const mock = await mockCamera(page);
  await page.goto("/chats");
  await activate(page);
  const composer = page.getByRole("textbox", { name: "Mensaje a Newton…" });
  await composer.fill("Esta foto se enviará cuando yo decida.");
  await page.getByRole("button", { name: "Tomar y adjuntar nueva captura" }).click();
  await expect(page.locator(".composer-attachment")).toHaveCount(1);
  await expect(composer).toHaveValue("Esta foto se enviará cuando yo decida.");
  expect(mock.analyses).toHaveLength(0);
  expect(mock.prompts).toHaveLength(0);
  await stopCamera(page);
  await expect(page.locator(".composer-attachment")).toHaveCount(1);
});

test("keeps the silent camera through internal Live suspension, then releases it on explicit hangup", async ({ page }, testInfo) => {
  const mock = await mockCamera(page);
  mock.keepWorking();
  await page.clock.install();
  await page.goto("/chats");
  await page.getByRole("button", { name: "Conversar con GPT-Live-1" }).click();
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await activate(page);
  await page.evaluate(() => (window as SmokeWindow).cameraSmoke.delegate());
  await page.clock.runFor(800);
  await expect.poll(() => mock.prompts.length).toBe(1);
  await page.clock.fastForward(16_000);
  await expect(page.getByText("Esperando respuesta…", { exact: true })).toBeVisible();
  await expect.poll(async () => { const state = await mediaState(page); return { cameras: state.cameras, microphones: state.microphones, callsClosed: state.callsClosed }; }).toEqual({ cameras: 1, microphones: 0, callsClosed: 1 });
  await expect(page.getByText("Cámara activa", { exact: true })).toBeVisible();
  const commentaryAfterClose = (await mediaState(page)).commentary.length;
  await page.clock.fastForward(6_000);
  expect(mock.analyses).toHaveLength(0);
  expect((await mediaState(page)).commentary).toHaveLength(commentaryAfterClose);
  expect(mock.calls).toHaveLength(1);
  expect(mock.prompts).toHaveLength(1);
  await page.screenshot({ path: testInfo.outputPath("camera-live-suspended.png"), fullPage: true });
  await page.getByRole("button", { name: "Terminar conversación de voz" }).click();
  await expect.poll(async () => (await mediaState(page)).cameras).toBe(0);
  await expect(page.locator(".camera-vision__inline")).toHaveCount(0);
});

test("saves the camera model in Preferences without opening media or inference", async ({ page }, testInfo) => {
  const mock = await mockCamera(page);
  await page.goto("/settings");
  const panel = page.locator("#camera-preferences");
  await panel.scrollIntoViewIfNeeded();
  await expect(panel.getByRole("combobox", { name: "Modelo de la cámara" })).toBeEnabled();
  await panel.getByRole("combobox", { name: "Modelo de la cámara" }).selectOption("gpt-5.6-sol");
  await expect(panel.getByRole("combobox", { name: "Intervalo de observación continua" })).toHaveCount(0);
  await panel.getByRole("button", { name: "Guardar preferencias de cámara" }).click();
  await expect(panel.getByText(/Preferencias de cámara guardadas/)).toBeVisible();
  expect(mock.preferences()).toEqual({ modelId: "gpt-5.6-sol", intervalSeconds: 5, configured: true });
  expect(mock.analyses).toHaveLength(0); expect(mock.calls).toHaveLength(0);
  expect((await mediaState(page)).cameras).toBe(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await panel.screenshot({ path: testInfo.outputPath("camera-preferences.png") });
});
