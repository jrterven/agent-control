import { bootstrapData, expect, test } from "./fixtures";
import type { Page } from "@playwright/test";

test.use({ serviceWorkers: "block" });

async function mockLive(page: Page) {
  const calls: { purpose?: string; focusMessageId?: string; sessionId: string }[] = [];
  await page.route("**/api/v1/bootstrap", (route) => route.fulfill({ json: {
    ...bootstrapData,
    features: { dictation: { available: false }, speech: { available: true, provider: "elevenlabs", modelId: "eleven_flash_v2_5", voiceId: "test-voice" }, live: { available: true, provider: "openai", modelId: "gpt-live-1" } },
  } }));
  await page.route("**/api/v1/sessions/*/live-transcripts**", (route) => route.fulfill(route.request().method() === "PUT" ? { status: 204 } : { json: { items: [], nextCursor: null } }));
  await page.route("**/api/v1/realtime/live-session", (route) => {
    calls.push(route.request().postDataJSON());
    return route.fulfill({ json: { session: { id: `live-${calls.length}` }, transport: { type: "webrtc", sdp: "fake-answer" } } });
  });
  await page.addInitScript(() => {
    const channels: Channel[] = [];
    let closed = 0;
    let liveTracks = 0;
    const tracks: { enabled: boolean; readyState: string }[] = [];
    class Channel extends EventTarget {
      readyState = "open";
      send(data: string) {
        if (JSON.parse(data).type === "session.close") {
          closed += 1;
          queueMicrotask(() => this.emit({ type: "session.closed", reason: "close_requested", usage: { seconds: 20 } }));
        }
      }
      emit(event: Record<string, unknown>) { this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(event) })); }
      close() { this.readyState = "closed"; }
    }
    class Peer extends EventTarget {
      channel = new Channel();
      connectionState = "connected";
      iceGatheringState = "complete";
      localDescription = { type: "offer", sdp: "fake-offer" };
      addTrack() {}
      createDataChannel() { channels.push(this.channel); return this.channel; }
      async createOffer() { return this.localDescription; }
      async setLocalDescription() {}
      async setRemoteDescription() { queueMicrotask(() => this.channel.emit({ type: "session.started" })); }
      close() {}
    }
    Object.defineProperty(window, "RTCPeerConnection", { value: Peer });
    Object.defineProperty(navigator, "mediaDevices", { value: { getUserMedia: async () => {
      liveTracks += 1;
      const track = Object.assign(new EventTarget(), { enabled: true, muted: false, readyState: "live", stop() { if (this.readyState === "live") liveTracks -= 1; this.readyState = "ended"; } });
      tracks.push(track);
      return { getTracks: () => [track], getAudioTracks: () => [track] };
    } } });
    Object.assign(window, { voiceWorkflow: {
      state: () => ({ closed, liveTracks, enabledTracks: tracks.filter((track) => track.readyState === "live" && track.enabled).length }),
      delegate: () => {
        const channel = channels.at(-1)!;
        channel.emit({ type: "session.input_transcript.delta", event_id: "request-text", delta: "Prepara el informe detallado.", start_ms: 100, end_ms: 1000 });
        channel.emit({ type: "session.delegation.created", event_id: "request", delegation: { id: "delegate-report", target: "client" }, offset_ms: 1100 });
      },
    } });
  });
  return calls;
}

for (const mode of ["resume", "explain", "paused"]) test(`closes during a long task and ${mode === "explain" ? "explains its result after returning" : mode === "paused" ? "keeps the microphone paused when resuming" : "resumes only when its result arrives"}`, async ({ page }, testInfo) => {
  const stopManually = mode === "explain";
  const calls = await mockLive(page);
  await page.clock.install();
  const history: { id: string; role: string; content: string; timestamp: number }[] = [];
  let operationId = "";
  let working = false;
  let prompts = 0;
  let interrupts = 0;
  await page.route("**/api/v1/sessions/session-e2e/messages", (route) => route.fulfill({ json: {
    items: history, sessionStatus: working ? "streaming" : "ready", activeOperation: working ? { operationId, status: "streaming", acceptedAt: new Date().toISOString() } : null,
  } }));
  await page.route("**/api/v1/sessions/session-e2e/interrupt", (route) => { interrupts++; return route.fulfill({ status: 204 }); });
  await page.route("**/api/v1/sessions/session-e2e/prompts", (route) => {
    prompts++;
    working = true;
    operationId = route.request().headers()["idempotency-key"];
    history.push({ id: "report-request", role: "user", content: route.request().postDataJSON().content, timestamp: Date.now() / 1000 });
    return route.fulfill({ json: { operationId, status: "accepted" } });
  });
  await page.goto("/chats");
  await page.getByRole("button", { name: "Conversar con GPT-Live-1" }).click();
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await page.evaluate(() => (window as unknown as { voiceWorkflow: { delegate(): void } }).voiceWorkflow.delegate());
  await page.clock.runFor(800);
  await expect.poll(() => prompts).toBe(1);
  if (mode === "paused") await page.getByRole("button", { name: "Pausar micrófono" }).click();
  await page.clock.fastForward(16_000);
  await expect(page.getByText("Esperando respuesta…", { exact: true })).toBeVisible();
  const capture = () => page.evaluate(() => (window as unknown as { voiceWorkflow: { state(): { closed: number; liveTracks: number; enabledTracks: number } } }).voiceWorkflow.state());
  await expect.poll(capture).toEqual({ closed: 1, liveTracks: 0, enabledTracks: 0 });
  expect(calls).toHaveLength(1);
  await page.screenshot({ path: testInfo.outputPath("waiting-without-voice-session.png"), fullPage: true });
  if (stopManually) {
    await page.getByRole("button", { name: "Terminar conversación de voz" }).click();
    await page.evaluate(() => {
      Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
      document.dispatchEvent(new Event("visibilitychange"));
    });
  }
  await page.clock.fastForward(20 * 60_000);
  expect(calls).toHaveLength(1);
  history.push({ id: "report-result", role: "assistant", content: "El informe está listo. Encontré tres oportunidades de mejora y guardé los resultados.", timestamp: Date.now() / 1000 + 1 });
  working = false;
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "visible" });
    window.dispatchEvent(new Event("focus"));
  });
  await expect(page.getByText("El informe está listo. Encontré tres oportunidades de mejora y guardé los resultados.", { exact: true })).toBeVisible();
  if (stopManually) {
    expect(calls).toHaveLength(1);
    await expect(page.getByRole("button", { name: "Escuchar esta respuesta" })).toBeVisible();
    await page.getByRole("button", { name: "Explicar con GPT Live" }).click();
  }
  await expect.poll(() => calls.length).toBe(2);
  expect(calls[1]).toMatchObject({ sessionId: "session-e2e", purpose: stopManually ? "explain" : "resume", focusMessageId: expect.stringMatching(/^sha256:[a-f0-9]{64}$/) });
  if (mode === "paused") {
    await expect(page.getByRole("button", { name: "Reanudar micrófono" })).toBeVisible();
    await expect.poll(capture).toEqual({ closed: 1, liveTracks: 1, enabledTracks: 0 });
    await page.getByRole("button", { name: "Reanudar micrófono" }).click();
    await expect.poll(capture).toEqual({ closed: 1, liveTracks: 1, enabledTracks: 1 });
  }
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  expect(prompts).toBe(1);
  expect(interrupts).toBe(0);
  await page.screenshot({ path: testInfo.outputPath("live-explains-completed-result.png"), fullPage: true });
});
