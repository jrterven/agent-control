import { bootstrapData, expect, test } from "./fixtures";
import type { LiveFragment } from "../../src/lib/openaiLiveClient";

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
