import { afterEach, describe, expect, it, vi } from "vitest";
import { visionApi } from "../lib/vision";

const jsonResponse = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("vision API boundary", () => {
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("loads and saves camera preferences without receiving or sending provider keys", async () => {
    const preferences = { modelId: "gpt-5.6-luna", intervalSeconds: 5, configured: true } as const;
    const fetchMock = vi.fn().mockImplementation(async () => jsonResponse(preferences));
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;
    await visionApi.preferences(signal);
    await visionApi.savePreferences({ modelId: "gpt-5.6-terra", intervalSeconds: 2 }, "csrf-memory", signal);
    expect(fetchMock.mock.calls[0]).toEqual(["/api/v1/vision/preferences", expect.objectContaining({ credentials: "same-origin", cache: "no-store", signal })]);
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(url).toBe("/api/v1/vision/preferences");
    expect(init.method).toBe("PUT");
    expect(init.headers).toEqual(expect.objectContaining({ "X-CSRF-Token": "csrf-memory" }));
    expect(new Headers(init.headers).has("Authorization")).toBe(false);
    expect(JSON.parse(String(init.body))).toEqual({ modelId: "gpt-5.6-terra", intervalSeconds: 2 });
  });

  it("scopes intent, images and observation pagination to encoded session paths", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => jsonResponse({}));
    vi.stubGlobal("fetch", fetchMock);
    const signal = new AbortController().signal;
    const intent = { requestId: "intent-request", text: "¿Qué ves?", recentContext: "Se activó la cámara." };
    const analysis = { requestId: "frame-request", activationId: "camera-activation", mode: "on_demand" as const, capturedAt: "2026-09-18T17:00:00Z", image: "data:image/jpeg;base64,AA==", question: "¿Qué ves?" };
    await visionApi.intent("private/session", intent, "csrf", signal);
    await visionApi.analyze("private/session", analysis, "csrf", signal);
    await visionApi.observations("private/session", "cursor/value", signal);
    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/v1/sessions/private%2Fsession/vision/intent",
      "/api/v1/sessions/private%2Fsession/vision/analyses",
      "/api/v1/sessions/private%2Fsession/vision/observations?before=cursor%2Fvalue",
    ]);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual(intent);
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual(analysis);
    for (const [, init] of fetchMock.mock.calls as [string, RequestInit][]) expect(init).toEqual(expect.objectContaining({ signal, cache: "no-store", credentials: "same-origin" }));
  });

  it("does not retry inference after provider/server or network errors", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(jsonResponse({ message: "Vision unavailable" }, 502)).mockRejectedValueOnce(new TypeError("Network lost"));
    vi.stubGlobal("fetch", fetchMock);
    await expect(visionApi.intent("session", { requestId: "one", text: "Look" }, "csrf")).rejects.toThrow("Vision unavailable");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await expect(visionApi.analyze("session", { requestId: "two", activationId: "camera", mode: "continuous", capturedAt: "2026-09-18T17:00:00Z", image: "data:image/jpeg;base64,AA==" }, "csrf")).rejects.toThrow("Network lost");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
