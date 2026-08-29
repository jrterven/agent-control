import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { createAndProvisionGateway } from "../lib/gatewayProvisioning";
import type { BootstrapData, Gateway, Profile, SessionSummary } from "../types";

const capabilities = {
  realtime: true, sessions: true, prompts: true, interrupt: true,
  cron: true, profiles: true, config: true, memory: true,
};

const gateway: Gateway = {
  id: "gateway-new", name: "Nuevo", location: "Privado", status: "connected",
  version: "0.20.6", sha: "abcdef0", capabilities,
};

const profiles: Profile[] = [
  { id: "profile-new-default", gatewayId: gateway.id, technicalName: "default", displayName: "Newton", model: "gpt", status: "ready", mutable: false },
  { id: "profile-new-dev", gatewayId: gateway.id, technicalName: "control-dev", displayName: "Control Dev", model: "gpt", status: "ready", mutable: true },
];

const session: SessionSummary = {
  id: "session-new", storedSessionId: "stored-new", profileId: profiles[0].id,
  title: "Importada", preview: "", updatedAt: "Ahora",
};

const payload = {
  name: "Nuevo", restUrl: "http://127.0.0.1:19119", wsUrl: "ws://127.0.0.1:19119",
  connectionMode: "private" as const, dashboardToken: "write-only-token",
};

function bootstrap(status: Gateway["status"] = "connected", sessions: SessionSummary[] = []): BootstrapData {
  return { gateways: [{ ...gateway, status }], profiles, workspaces: [], sessions, automations: [] };
}

describe("new gateway provisioning", () => {
  afterEach(() => vi.restoreAllMocks());

  it("creates, refreshes profiles, syncs each profile and then rehydrates bootstrap", async () => {
    const create = vi.spyOn(api, "createGateway").mockResolvedValue(gateway);
    const refresh = vi.spyOn(api, "refreshProfiles").mockResolvedValue([]);
    const load = vi.spyOn(api, "bootstrap")
      .mockResolvedValueOnce(bootstrap())
      .mockResolvedValueOnce(bootstrap("connected", [session]));
    const sync = vi.spyOn(api, "syncSessions").mockResolvedValue([session]);

    const result = await createAndProvisionGateway(payload, "csrf-memory-only");

    expect(result).toMatchObject({ gatewayId: gateway.id, degraded: false });
    expect(result.bootstrap.sessions).toEqual([session]);
    expect(create).toHaveBeenCalledWith(payload, "csrf-memory-only");
    expect(refresh).toHaveBeenCalledWith(gateway.id, "csrf-memory-only");
    expect(sync).toHaveBeenCalledTimes(2);
    expect(sync).toHaveBeenCalledWith(gateway.id, "default", "csrf-memory-only");
    expect(sync).toHaveBeenCalledWith(gateway.id, "control-dev", "csrf-memory-only");
    expect(create.mock.invocationCallOrder[0]).toBeLessThan(refresh.mock.invocationCallOrder[0]);
    expect(refresh.mock.invocationCallOrder[0]).toBeLessThan(load.mock.invocationCallOrder[0]);
    expect(load.mock.invocationCallOrder[0]).toBeLessThan(sync.mock.invocationCallOrder[0]);
    expect(sync.mock.invocationCallOrder[1]).toBeLessThan(load.mock.invocationCallOrder[1]);
  });

  it("keeps a created gateway visible as degraded when discovery fails", async () => {
    vi.spyOn(api, "createGateway").mockResolvedValue(gateway);
    vi.spyOn(api, "refreshProfiles").mockRejectedValue(new TypeError("tunnel unavailable"));
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap("offline"));
    const sync = vi.spyOn(api, "syncSessions");

    const result = await createAndProvisionGateway(payload);

    expect(sync).not.toHaveBeenCalled();
    expect(result.degraded).toBe(true);
    expect(result.bootstrap.gateways.find((item) => item.id === gateway.id)?.status).toBe("degraded");
  });
});
