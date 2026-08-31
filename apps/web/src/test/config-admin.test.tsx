import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../lib/api";
import { db } from "../lib/db";
import { ConfigScreen } from "../screens/Screens";
import { useAppStore } from "../store/appStore";
import type { BootstrapData } from "../types";

function response(body: unknown, status = 200) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

function bootstrap(methods: string[]): BootstrapData {
  return {
    gateways: [{
      id: "gateway-1",
      name: "Hermes existente",
      location: "Túnel privado",
      status: "connected",
      version: "0.20.5",
      sha: "791e2ae",
      capabilities: { realtime: true, sessions: true, prompts: true, interrupt: true, cron: true, profiles: true, config: true, memory: false },
      capabilitySet: { protocol: "dashboard-rest", version: "0.20.5", sourceSha: "791e2ae", methods, features: ["administration"] },
    }],
    profiles: [{
      id: "profile-1",
      gatewayId: "gateway-1",
      technicalName: "control-dev",
      displayName: "Control Dev",
      model: "mock-model",
      status: "ready",
      mutable: true,
      capabilities: { realtime: true, sessions: true, prompts: true, interrupt: true, cron: true, profiles: true, config: true, memory: false },
      capabilitySet: { protocol: "dashboard-rest", version: "0.20.5", sourceSha: "791e2ae", methods, features: ["administration"] },
    }],
    workspaces: [],
    sessions: [],
    automations: [],
  };
}

function lifecycleBootstrap(): BootstrapData {
  const sourceMethods = ["profiles.delete", "profiles.export", "profiles.transfer"];
  const data = bootstrap(sourceMethods);
  const destination = {
    ...data.gateways[0],
    id: "gateway-2",
    name: "Hermes destino",
  };
  return {
    ...data,
    gateways: [...data.gateways, destination],
    profiles: [{
      ...data.profiles[0],
      capabilities: {
        ...data.profiles[0].capabilities!,
        profileDelete: true,
        profileExport: true,
        profileTransfer: true,
      },
      capabilitySet: { ...data.profiles[0].capabilitySet!, methods: sourceMethods },
    }, {
      ...data.profiles[0],
      id: "profile-destination-admin",
      gatewayId: destination.id,
      technicalName: "default",
      displayName: "Destino",
      capabilities: { ...data.profiles[0].capabilities!, profileDelete: true, profileImport: true, profileTransfer: true },
      capabilitySet: { ...data.profiles[0].capabilitySet!, methods: ["profiles.delete", "profiles.import", "profiles.transfer"] },
    }],
  };
}

describe("administración de perfil guiada por capacidades", () => {
  beforeEach(async () => {
    vi.mocked(window.matchMedia).mockImplementation((query: string) => ({
      matches: query.includes("min-width") || query.includes("dark"),
      media: query,
      onchange: null,
      addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
    }));
    await db.delete();
    await db.open();
    useAppStore.setState({ authState: "authenticated", csrfToken: "csrf-test", bootstrapLoaded: false });
  });

  afterEach(async () => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    useAppStore.getState().resetPrivateState();
    await db.delete();
  });

  it("oculta tabs y mutaciones que el método exacto no anunció", async () => {
    const fetchMock = vi.fn(() => response({
      gatewayId: "gateway-1",
      profileName: "control-dev",
      resource: "models",
      data: { current: { provider: "mock", model: "mock-model" }, providers: [{ id: "mock", label: "Mock", models: ["mock-model"] }] },
    }));
    vi.stubGlobal("fetch", fetchMock);
    useAppStore.getState().hydrateBootstrap(bootstrap(["models.list"]));

    render(<ConfigScreen />);

    expect(await screen.findByRole("heading", { name: "Modelo principal" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "General" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Herramientas" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Integraciones" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Secretos" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Guardar modelo" })).not.toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not offer a destination without verified transfer rollback support", async () => {
    const current = lifecycleBootstrap();
    current.profiles[1] = {
      ...current.profiles[1],
      capabilities: { ...current.profiles[1].capabilities!, profileTransfer: false },
      capabilitySet: { ...current.profiles[1].capabilitySet!, methods: ["profiles.delete", "profiles.import"] },
    };
    useAppStore.getState().hydrateBootstrap(current);
    const user = userEvent.setup();

    render(<ConfigScreen />);
    await user.click(screen.getByRole("button", { name: "Administrar agente" }));

    expect(screen.getByText(/rollback\/eliminación de perfiles verificados/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mover agente" })).toBeDisabled();
  });

  it("envía CSRF e idempotencia y limpia el valor write-only después de guardarlo", async () => {
    const privateValue = "PRIVATE-NEVER-RENDER";
    const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (init?.method === "PATCH") {
        return response({ gatewayId: "gateway-1", profileName: "control-dev", resource: "secrets", data: { name: "OPENAI_API_KEY", configured: true, status: "applied" } });
      }
      if (url.endsWith("/secrets")) {
        return response({ gatewayId: "gateway-1", profileName: "control-dev", resource: "secrets", data: { items: [{ name: "OPENAI_API_KEY", configured: false, description: "Clave del proveedor" }] } });
      }
      return response({}, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    useAppStore.getState().hydrateBootstrap(bootstrap(["secrets.list", "secrets.set"]));
    const user = userEvent.setup();

    render(<ConfigScreen />);
    await user.click(await screen.findByRole("button", { name: "Secretos" }));
    const input = await screen.findByLabelText("Nuevo valor para OPENAI_API_KEY");
    await user.type(input, privateValue);
    await user.click(screen.getByRole("button", { name: "Guardar valor" }));

    await waitFor(() => expect(input).toHaveValue(""));
    const patchCall = fetchMock.mock.calls.find(([, init]) => init?.method === "PATCH");
    expect(patchCall).toBeDefined();
    const headers = patchCall?.[1]?.headers as Record<string, string>;
    expect(headers["X-CSRF-Token"]).toBe("csrf-test");
    expect(headers["Idempotency-Key"]).toBeTruthy();
    expect(JSON.parse(String(patchCall?.[1]?.body))).toEqual({ value: privateValue });
    expect(document.body).not.toHaveTextContent(privateValue);
    expect(await screen.findByText("OPENAI_API_KEY guardado sin exponer su valor.")).toBeInTheDocument();
  });

  it("moves a profile only after exact confirmation and selects it on the destination gateway", async () => {
    const current = lifecycleBootstrap();
    useAppStore.getState().hydrateBootstrap(current);
    const movedProfile = {
      ...current.profiles[0],
      id: "profile-1-moved",
      gatewayId: "gateway-2",
    };
    const next = {
      ...current,
      profiles: [current.profiles[1], movedProfile],
    };
    const move = vi.spyOn(api, "moveProfile").mockResolvedValue({ warnings: ["Revisa las rutas locales"] });
    vi.spyOn(api, "bootstrap").mockResolvedValue(next);
    const user = userEvent.setup();

    render(<ConfigScreen />);
    await user.click(screen.getByRole("button", { name: "Administrar agente" }));
    expect(screen.getByText(/Las credenciales no se copian/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Mover agente" }));
    const dialog = screen.getByRole("dialog", { name: "Mover a Control Dev" });
    const destination = within(dialog).getByLabelText("Gateway destino");
    expect(destination).toHaveFocus();
    expect(dialog.querySelector(".modal-scrim")).toHaveAttribute("tabindex", "-1");
    expect(dialog.querySelector(".modal-scrim")).toHaveAttribute("aria-hidden", "true");
    const confirm = within(dialog).getByRole("button", { name: "Confirmar transferencia" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Escribe control-dev para confirmar"), "control-dev");
    await user.click(confirm);

    await waitFor(() => expect(move).toHaveBeenCalledWith("profile-1", "gateway-2", "control-dev", "csrf-test"));
    await waitFor(() => expect(useAppStore.getState().selectedProfileId).toBe(movedProfile.id));
    expect(useAppStore.getState().selectedGatewayId).toBe("gateway-2");
    expect(await screen.findByText("Revisa las rutas locales")).toBeInTheDocument();
  });

  it("deletes a non-default profile after exact confirmation and selects a safe fallback", async () => {
    const current = lifecycleBootstrap();
    const fallback = {
      ...current.profiles[0],
      id: "profile-fallback",
      technicalName: "default",
      displayName: "Default",
      capabilities: { ...current.profiles[0].capabilities!, profileDelete: false, profileExport: false, profileTransfer: false },
      capabilitySet: { ...current.profiles[0].capabilitySet!, methods: [] },
    };
    current.profiles = [current.profiles[0], fallback, current.profiles[1]];
    useAppStore.getState().hydrateBootstrap(current);
    const remove = vi.spyOn(api, "deleteProfile").mockResolvedValue({});
    vi.spyOn(api, "bootstrap").mockResolvedValue({ ...current, profiles: [fallback, current.profiles[2]] });
    const user = userEvent.setup();

    render(<ConfigScreen />);
    await user.click(screen.getByRole("button", { name: "Administrar agente" }));
    await user.click(screen.getByRole("button", { name: "Eliminar agente" }));
    const dialog = screen.getByRole("dialog", { name: "Eliminar a Control Dev" });
    const confirm = within(dialog).getByRole("button", { name: "Eliminar definitivamente" });
    expect(confirm).toBeDisabled();
    await user.type(within(dialog).getByLabelText("Escribe control-dev para confirmar"), "control-dev");
    await user.click(confirm);

    await waitFor(() => expect(remove).toHaveBeenCalledWith("profile-1", "control-dev", "csrf-test"));
    await waitFor(() => expect(useAppStore.getState().selectedProfileId).toBe(fallback.id));
    expect(useAppStore.getState().profiles.some((profile) => profile.id === "profile-1")).toBe(false);
  });

  it("shows a rejected move inside the open dialog", async () => {
    useAppStore.getState().hydrateBootstrap(lifecycleBootstrap());
    vi.spyOn(api, "moveProfile").mockRejectedValue(new ApiError(409, "Las revisiones de Hermes no son compatibles", "CONFLICT"));
    const user = userEvent.setup();

    render(<ConfigScreen />);
    await user.click(screen.getByRole("button", { name: "Administrar agente" }));
    await user.click(screen.getByRole("button", { name: "Mover agente" }));
    const dialog = screen.getByRole("dialog", { name: "Mover a Control Dev" });
    await user.type(within(dialog).getByLabelText("Escribe control-dev para confirmar"), "control-dev");
    await user.click(within(dialog).getByRole("button", { name: "Confirmar transferencia" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Las revisiones de Hermes no son compatibles");
    expect(screen.getByRole("dialog", { name: "Mover a Control Dev" })).toBeInTheDocument();
  });

  it("blocks both lifecycle actions after an ambiguous move, including after switching profiles", async () => {
    const current = lifecycleBootstrap();
    useAppStore.getState().hydrateBootstrap(current);
    const move = vi.spyOn(api, "moveProfile").mockRejectedValue(new ApiError(
      409,
      "Hermes no pudo confirmar la entrega",
      "MUTATION_DELIVERY_UNKNOWN",
    ));
    vi.spyOn(api, "bootstrap").mockResolvedValue(current);
    const user = userEvent.setup();

    render(<ConfigScreen />);
    await user.click(screen.getByRole("button", { name: "Administrar agente" }));
    await user.click(screen.getByRole("button", { name: "Mover agente" }));
    const dialog = screen.getByRole("dialog", { name: "Mover a Control Dev" });
    await user.type(within(dialog).getByLabelText("Escribe control-dev para confirmar"), "control-dev");
    await user.click(within(dialog).getByRole("button", { name: "Confirmar transferencia" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Mover a Control Dev" })).not.toBeInTheDocument());
    expect(await screen.findByText(/bloqueó el reenvío/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Mover agente" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Eliminar agente" })).toBeDisabled();

    const gatewaySelect = screen.getByLabelText("Gateway");
    await user.selectOptions(gatewaySelect, "gateway-2");
    expect(useAppStore.getState().selectedProfileId).toBe("profile-destination-admin");
    await user.selectOptions(gatewaySelect, "gateway-1");
    expect(useAppStore.getState().selectedProfileId).toBe("profile-1");
    expect(screen.getByText(/bloqueadas en este dispositivo/)).toBeInTheDocument();
    const blockedMove = screen.getByRole("button", { name: "Mover agente" });
    expect(blockedMove).toBeDisabled();
    await user.click(blockedMove);
    expect(move).toHaveBeenCalledTimes(1);
  });

  it("treats a transport failure as an unknown outcome and never resubmits it", async () => {
    const current = lifecycleBootstrap();
    useAppStore.getState().hydrateBootstrap(current);
    const move = vi.spyOn(api, "moveProfile").mockRejectedValue(new TypeError("Failed to fetch"));
    vi.spyOn(api, "bootstrap").mockResolvedValue(current);
    const user = userEvent.setup();

    render(<ConfigScreen />);
    await user.click(screen.getByRole("button", { name: "Administrar agente" }));
    await user.click(screen.getByRole("button", { name: "Mover agente" }));
    const dialog = screen.getByRole("dialog", { name: "Mover a Control Dev" });
    await user.type(within(dialog).getByLabelText("Escribe control-dev para confirmar"), "control-dev");
    await user.click(within(dialog).getByRole("button", { name: "Confirmar transferencia" }));

    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Mover a Control Dev" })).not.toBeInTheDocument());
    expect(screen.getByRole("button", { name: "Mover agente" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Mover agente" }));
    expect(move).toHaveBeenCalledTimes(1);
  });

  it("invalidates route snapshots before a committed move refresh fails", async () => {
    const current = lifecycleBootstrap();
    useAppStore.getState().hydrateBootstrap(current);
    const now = Date.now();
    await db.drafts.put({ sessionId: "draft-preserved", content: "keep", updatedAt: now });
    await db.offlineSnapshots.put({ id: "latest", cipherText: "stale", expiresAt: now + 60_000, updatedAt: now });
    await db.shellSnapshots.put({ id: "latest", cipherText: "stale", expiresAt: now + 60_000, updatedAt: now });
    vi.spyOn(api, "moveProfile").mockResolvedValue({});
    vi.spyOn(api, "bootstrap").mockRejectedValue(new Error("refresh unavailable"));
    const user = userEvent.setup();

    render(<ConfigScreen />);
    await user.click(screen.getByRole("button", { name: "Administrar agente" }));
    await user.click(screen.getByRole("button", { name: "Mover agente" }));
    const dialog = screen.getByRole("dialog", { name: "Mover a Control Dev" });
    await user.type(within(dialog).getByLabelText("Escribe control-dev para confirmar"), "control-dev");
    await user.click(within(dialog).getByRole("button", { name: "Confirmar transferencia" }));

    expect(await screen.findByText(/ya fue movido/)).toBeInTheDocument();
    expect(await db.offlineSnapshots.count()).toBe(0);
    expect(await db.shellSnapshots.count()).toBe(0);
    expect((await db.drafts.get("draft-preserved"))?.content).toBe("keep");
  });

  it("purges a deleted profile's chats before waiting for bootstrap", async () => {
    const current = lifecycleBootstrap();
    const fallback = {
      ...current.profiles[0],
      id: "profile-fallback",
      technicalName: "default",
      displayName: "Default",
      capabilities: { ...current.profiles[0].capabilities!, profileDelete: false, profileExport: false, profileTransfer: false },
      capabilitySet: { ...current.profiles[0].capabilitySet!, methods: [] },
    };
    current.profiles = [current.profiles[0], fallback, current.profiles[1]];
    current.sessions = [{
      id: "session-deleted",
      gatewayId: "gateway-1",
      profileName: "control-dev",
      profileId: "profile-1",
      storedSessionId: "stored-deleted",
      title: "Privado",
      preview: "",
      updatedAt: "ahora",
    }];
    useAppStore.getState().hydrateBootstrap(current);
    useAppStore.setState({ messages: [{ id: "message-deleted", sessionId: "session-deleted", role: "assistant", content: "private", createdAt: "ahora" }] });
    const now = Date.now();
    await db.drafts.put({ sessionId: "session-deleted", content: "private", updatedAt: now });
    await db.offlineSnapshots.put({ id: "latest", cipherText: "stale", expiresAt: now + 60_000, updatedAt: now });
    let finishBootstrap!: (value: BootstrapData) => void;
    const bootstrapRequest = vi.spyOn(api, "bootstrap").mockImplementation(() => new Promise((resolve) => { finishBootstrap = resolve; }));
    vi.spyOn(api, "deleteProfile").mockResolvedValue({});
    const user = userEvent.setup();

    render(<ConfigScreen />);
    await user.click(screen.getByRole("button", { name: "Administrar agente" }));
    await user.click(screen.getByRole("button", { name: "Eliminar agente" }));
    const dialog = screen.getByRole("dialog", { name: "Eliminar a Control Dev" });
    await user.type(within(dialog).getByLabelText("Escribe control-dev para confirmar"), "control-dev");
    await user.click(within(dialog).getByRole("button", { name: "Eliminar definitivamente" }));

    await waitFor(() => expect(bootstrapRequest).toHaveBeenCalledTimes(1));
    expect(useAppStore.getState().sessions).toEqual([]);
    expect(useAppStore.getState().messages).toEqual([]);
    expect(await db.drafts.get("session-deleted")).toBeUndefined();
    expect(await db.offlineSnapshots.count()).toBe(0);

    finishBootstrap({ ...current, profiles: [fallback, current.profiles[2]], sessions: [] });
    await waitFor(() => expect(useAppStore.getState().selectedProfileId).toBe(fallback.id));
  });
});
