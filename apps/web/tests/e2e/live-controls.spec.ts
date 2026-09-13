import { bootstrapData, expect, test } from "./fixtures";

test("waits for readiness and pauses the microphone without a new call", async ({ page }, testInfo) => {
  let calls = 0;
  await page.route("**/api/v1/bootstrap", (route) => route.fulfill({ json: {
    ...bootstrapData,
    features: { dictation: { available: false, provider: "elevenlabs", modelId: "scribe_v2_realtime" }, voice: { provider: "openai_live" }, live: { available: true, provider: "openai", modelId: "gpt-live-1" } },
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
        if (JSON.parse(data).type === "session.close") queueMicrotask(() => this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "session.closed", reason: "close_requested" }) })));
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
    Object.defineProperty(window, "AudioContext", { value: undefined });
    Object.assign(window, { liveTest: {
      ready: () => channel.dispatchEvent(new MessageEvent("message", { data: JSON.stringify({ type: "session.started" }) })),
      enabled: () => track.enabled,
    } });
  });
  const micEnabled = () => page.evaluate(() => (window as unknown as { liveTest: { enabled(): boolean } }).liveTest.enabled());
  await page.goto("/chats");
  await page.getByRole("button", { name: "Conversar con GPT-Live-1" }).click();
  await expect(page.getByText("Conectando… espera para hablar")).toBeVisible();
  await expect.poll(micEnabled).toBe(false);
  await expect.poll(() => calls).toBe(1);
  await page.evaluate(() => (window as unknown as { liveTest: { ready(): void } }).liveTest.ready());
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await expect.poll(micEnabled).toBe(true);
  await page.locator(".composer").screenshot({ path: testInfo.outputPath("live-listening.png") });
  await page.getByRole("button", { name: "Pausar micrófono" }).click();
  await expect(page.getByText("Micrófono en pausa")).toBeVisible();
  await expect.poll(micEnabled).toBe(false);
  await page.locator(".composer").screenshot({ path: testInfo.outputPath("live-paused.png") });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.getByRole("button", { name: "Reanudar micrófono" }).click();
  await expect(page.getByText("Escuchando · ya puedes hablar")).toBeVisible();
  await expect.poll(micEnabled).toBe(true);
  expect(calls).toBe(1);
  await page.getByRole("button", { name: "Pausar micrófono" }).click();
  await page.getByRole("button", { name: "Terminar conversación de voz" }).click();
  await expect(page.getByRole("button", { name: "Conversar con GPT-Live-1" })).toBeEnabled();
  await expect(page.locator(".live-voice-state")).toHaveCount(0);
});
