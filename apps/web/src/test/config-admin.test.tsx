import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
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

describe("administración de perfil guiada por capacidades", () => {
  beforeEach(() => {
    useAppStore.setState({ authState: "authenticated", csrfToken: "csrf-test", bootstrapLoaded: false });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    useAppStore.getState().resetPrivateState();
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
});
