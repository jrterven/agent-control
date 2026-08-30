import { afterEach, describe, expect, it, vi } from "vitest";
import { advanceReplayCursor, api } from "../lib/api";

describe("browser API boundary", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses only same-origin relative API paths", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "admin", name: "Admin" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await api.me();
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/auth/me", expect.objectContaining({ credentials: "same-origin" }));
  });

  it("adds idempotency to prompt mutations without persisting credentials", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ operationId: "op-1", status: "accepted" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await api.submitPrompt("session-a", "Hola", "prompt-idempotency-key", "csrf-memory-only");
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(init.headers).toEqual(expect.objectContaining({ "Idempotency-Key": "prompt-idempotency-key", "X-CSRF-Token": "csrf-memory-only" }));
    expect(init.credentials).toBe("same-origin");
  });

  it("keeps the owner ElevenLabs key write-only and issues non-replayable Scribe tokens", async () => {
    const presence = { configured: true, provider: "elevenlabs", modelId: "scribe_v2_realtime" };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify(presence), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(presence), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ ok: true, provider: "elevenlabs", modelId: "scribe_v2_realtime" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ token: "single-use", expiresAt: "2026-08-29T12:15:00Z", modelId: "scribe_v2_realtime" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.elevenLabsIntegration();
    await api.saveElevenLabsKey("sk_backend_only", "csrf-memory-only");
    await api.testElevenLabsIntegration("csrf-memory-only");
    await api.createTranscriptionToken({ sessionId: "session-a" }, "csrf-memory-only");
    await api.deleteElevenLabsKey("csrf-memory-only");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/v1/integrations/elevenlabs");
    const [keyPath, keyInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(keyPath).toBe("/api/v1/integrations/elevenlabs/key");
    expect(JSON.parse(String(keyInit.body))).toEqual({ apiKey: "sk_backend_only" });
    expect(keyInit.headers).toEqual(expect.objectContaining({ "Idempotency-Key": expect.any(String), "X-CSRF-Token": "csrf-memory-only" }));

    const [tokenPath, tokenInit] = fetchMock.mock.calls[3] as [string, RequestInit];
    expect(tokenPath).toBe("/api/v1/realtime/transcription-token");
    expect(JSON.parse(String(tokenInit.body))).toEqual({ sessionId: "session-a" });
    expect(tokenInit.headers).toEqual(expect.objectContaining({ "X-CSRF-Token": "csrf-memory-only" }));
    expect(tokenInit.headers).not.toEqual(expect.objectContaining({ "Idempotency-Key": expect.anything() }));
    expect(fetchMock.mock.calls[4][0]).toBe("/api/v1/integrations/elevenlabs/key");
    expect((fetchMock.mock.calls[4][1] as RequestInit).method).toBe("DELETE");
  });

  it("sends only an allowlisted TTS model with the selected voice", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      configured: true,
      provider: "elevenlabs",
      modelId: "scribe_v2_realtime",
      ttsModelId: "eleven_multilingual_v2",
      voiceId: "voice-aria",
      voiceName: "Aria",
      speechAvailable: true,
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await api.saveElevenLabsVoice(
      "voice-aria",
      "eleven_multilingual_v2",
      "csrf-memory-only",
    );

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v1/integrations/elevenlabs/voice");
    expect(JSON.parse(String(init.body))).toEqual({
      voiceId: "voice-aria",
      ttsModelId: "eleven_multilingual_v2",
    });
  });

  it("responds to official approval and clarification gates through same-origin Control routes", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ requestId: "approval/1", resolved: 1, status: "resolved" }), { status: 200, headers: { "Content-Type": "application/json" } }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ requestId: "clarify/1", status: "ok", remaining: ["q1"] }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await api.respondApproval("session/a", "approval/1", "once", "csrf-memory-only");
    await api.respondClarification("session/a", "clarify/1", ["staging", "canary"], "q0", "csrf-memory-only");

    const [approvalPath, approvalInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(approvalPath).toBe("/api/v1/sessions/session%2Fa/approvals/approval%2F1/respond");
    expect(approvalInit.method).toBe("POST");
    expect(JSON.parse(String(approvalInit.body))).toEqual({ choice: "once" });
    expect(approvalInit.headers).toEqual(expect.objectContaining({
      "Idempotency-Key": expect.any(String),
      "X-CSRF-Token": "csrf-memory-only",
    }));

    const [clarifyPath, clarifyInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(clarifyPath).toBe("/api/v1/sessions/session%2Fa/clarifications/clarify%2F1/respond");
    expect(clarifyInit.method).toBe("POST");
    expect(JSON.parse(String(clarifyInit.body))).toEqual({ answer: ["staging", "canary"], questionId: "q0" });
    expect(clarifyInit.headers).toEqual(expect.objectContaining({
      "Idempotency-Key": expect.any(String),
      "X-CSRF-Token": "csrf-memory-only",
    }));
  });

  it("uses the official session sync contract", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify([]), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await api.syncSessions("gateway-a", "default", "csrf-memory-only");
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v1/sessions/sync");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ gatewayId: "gateway-a", profileName: "default" });
    expect(init.headers).toEqual(expect.objectContaining({ "Idempotency-Key": expect.any(String), "X-CSRF-Token": "csrf-memory-only" }));
  });

  it("creates a fresh agent through the same-origin profile boundary", async () => {
    const response = {
      id: "profile-researcher",
      gatewayId: "gateway-a",
      technicalName: "researcher-ai",
      displayName: "Researcher",
      model: "unconfigured",
      status: "ready",
      mutable: true,
    };
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(response), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await api.createProfile({
      gatewayId: "gateway-a",
      technicalName: "researcher-ai",
      displayName: "Researcher",
      description: "Researches technical sources without inherited credentials.",
    }, "csrf-memory-only");

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v1/profiles");
    expect(init.method).toBe("POST");
    expect(init.credentials).toBe("same-origin");
    expect(init.headers).toEqual(expect.objectContaining({
      "Idempotency-Key": expect.any(String),
      "X-CSRF-Token": "csrf-memory-only",
    }));
    expect(JSON.parse(String(init.body))).toEqual({
      gatewayId: "gateway-a",
      technicalName: "researcher-ai",
      displayName: "Researcher",
      description: "Researches technical sources without inherited credentials.",
    });
  });

  it("sends operator trust only to the gateway PATCH and supports explicit revocation", async () => {
    const fetchMock = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ id: "gateway-a" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    await api.updateGateway("gateway/a", { trustedSourceSha: "a".repeat(40) }, "csrf-memory-only");
    await api.updateGateway("gateway/a", { trustedSourceSha: null }, "csrf-memory-only");

    const [updatePath, updateInit] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(updatePath).toBe("/api/v1/gateways/gateway%2Fa");
    expect(updateInit.method).toBe("PATCH");
    expect(JSON.parse(String(updateInit.body))).toEqual({ trustedSourceSha: "a".repeat(40) });
    expect(updateInit.headers).toEqual(expect.objectContaining({
      "Idempotency-Key": expect.any(String),
      "X-CSRF-Token": "csrf-memory-only",
    }));

    const [, revokeInit] = fetchMock.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(String(revokeInit.body))).toEqual({ trustedSourceSha: null });
  });

  it("searches through the same-origin Control API instead of browser-only state", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [], partial: false }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await api.search("tema con espacios", "message", 25);
    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v1/search?q=tema%20con%20espacios&kind=message&limit=25");
    expect(init.credentials).toBe("same-origin");
  });

  it("reads active readiness from Control", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "ready", database: "ready", upstream: "degraded", time: "2026-08-28T00:00:00Z" }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await api.readiness()).toEqual(expect.objectContaining({ database: "ready", upstream: "degraded" }));
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/ready", expect.objectContaining({ credentials: "same-origin" }));
  });

  it("archives only the Control session reference through the local PATCH route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: "session-a",
      gatewayId: "gateway-a",
      profileName: "control-dev",
      profileId: "profile-a",
      storedSessionId: "stored-a",
      status: "ready",
      archivedAt: "2026-08-28T12:00:00Z",
      updatedAt: "2026-08-28T12:00:00Z",
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const archived = await api.archiveSession("session/a", "csrf-memory-only");

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v1/sessions/session%2Fa");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ archived: true });
    expect(init.headers).toEqual(expect.objectContaining({
      "Idempotency-Key": expect.any(String),
      "X-CSRF-Token": "csrf-memory-only",
    }));
    expect(archived.archived).toBe(true);
  });

  it("sends the exact persistent Hermes session id in the reinforced delete header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(null, { status: 204 }));
    vi.stubGlobal("fetch", fetchMock);

    await api.deleteSessionFromHermes("session/a", "stored-exact-42", "csrf-memory-only");

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v1/sessions/session%2Fa");
    expect(init.method).toBe("DELETE");
    expect(init.headers).toEqual(expect.objectContaining({
      "Idempotency-Key": expect.any(String),
      "X-CSRF-Token": "csrf-memory-only",
      "X-Confirm-Delete": "stored-exact-42",
    }));
  });

  it("moves a session to a workspace through Control metadata", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: "session-a",
      gatewayId: "gateway-a",
      workspaceId: "workspace-papers",
      profileName: "jarvis",
      profileId: "profile-jarvis",
      storedSessionId: "stored-a",
      runtimeSessionId: null,
      title: "Conversation",
      status: "idle",
      archivedAt: null,
      updatedAt: "2026-08-30T02:00:00Z",
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);

    const moved = await api.moveSession("session/a", "workspace-papers", "csrf-memory-only");

    const [path, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v1/sessions/session%2Fa");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(String(init.body))).toEqual({ workspaceId: "workspace-papers" });
    expect(init.headers).toEqual(expect.objectContaining({
      "Idempotency-Key": expect.any(String),
      "X-CSRF-Token": "csrf-memory-only",
    }));
    expect(moved.workspaceId).toBe("workspace-papers");
  });

  it("downloads sanitized session exports through Control", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response("{}", {
      status: 200,
      headers: {
        "Content-Type": "application/json",
        "Content-Disposition": 'attachment; filename="hermes-session-a.json"',
      },
    }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await api.exportSession("session/a");

    expect(result.filename).toBe("hermes-session-a.json");
    expect(result.blob.size).toBe(2);
    expect(fetchMock).toHaveBeenCalledWith("/api/v1/sessions/session%2Fa/export", expect.objectContaining({ credentials: "same-origin" }));
  });

  it("resets sequence watermarks when the replay epoch changes", () => {
    expect(advanceReplayCursor({ seq: 12, epoch: "epoch-a" }, 8, "epoch-a")).toEqual({ seq: 12, epoch: "epoch-a" });
    expect(advanceReplayCursor({ seq: 12, epoch: "epoch-a" }, 2, "epoch-b")).toEqual({ seq: 2, epoch: "epoch-b" });
  });
});
