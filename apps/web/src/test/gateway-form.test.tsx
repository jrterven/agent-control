import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { GatewaysScreen } from "../screens/Screens";
import { useAppStore } from "../store/appStore";
import type { BootstrapData, Gateway } from "../types";

vi.mock("@tanstack/react-router", () => ({
  Link: ({ children }: { children: ReactNode }) => <>{children}</>,
  useNavigate: () => vi.fn(),
}));

const gateway: Gateway = {
  id: "gateway-edit",
  name: "Gateway remoto",
  location: "Privado",
  status: "connected",
  envManaged: false,
  hasTrustedSourceSha: true,
  version: "0.20.6",
  sha: "9978706",
  capabilities: {
    realtime: true,
    sessions: true,
    prompts: false,
    interrupt: false,
    cron: false,
    profiles: true,
    config: false,
    memory: false,
  },
};

const bootstrap = (nextGateway = gateway): BootstrapData => ({
  gateways: [nextGateway],
  profiles: [],
  workspaces: [],
  sessions: [],
  automations: [],
});

async function openAndFillSecrets() {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Añadir gateway" }));
  await user.type(screen.getByLabelText("Token dashboard (solo escritura)"), "dashboard-secret-123");
  await user.type(screen.getByLabelText("API key fallback (solo escritura)"), "fallback-secret-456");
  return user;
}

describe("write-only gateway fields", () => {
  beforeEach(() => {
    useAppStore.setState({ gateways: [], csrfToken: "csrf-memory-only", connection: "connected" });
  });

  afterEach(() => vi.restoreAllMocks());

  it("labels an unreported Hermes SHA without exposing the configured trust anchor", () => {
    useAppStore.setState({ gateways: [{ ...gateway, sha: null }] });

    render(<GatewaysScreen />);

    expect(screen.getByText("SHA no reportado")).toBeInTheDocument();
    expect(screen.getByText("SHA confiable configurado")).toBeInTheDocument();
  });

  it("clears credentials when the form is cancelled and reopened", async () => {
    render(<GatewaysScreen />);
    const user = await openAndFillSecrets();
    await user.click(screen.getByRole("button", { name: "Cancelar" }));
    await user.click(screen.getByRole("button", { name: "Añadir gateway" }));

    expect(screen.getByLabelText("Token dashboard (solo escritura)")).toHaveValue("");
    expect(screen.getByLabelText("API key fallback (solo escritura)")).toHaveValue("");
    expect(screen.getByLabelText("SHA fuente confiable (solo escritura, opcional)")).toHaveValue("");
  });

  it("clears credentials in finally after a failed create request", async () => {
    vi.spyOn(api, "createGateway").mockRejectedValue(new TypeError("network down"));
    render(<GatewaysScreen />);
    const user = await openAndFillSecrets();
    await user.type(screen.getByRole("textbox", { name: "Nombre" }), "Gateway remoto");
    await user.type(screen.getByRole("textbox", { name: "REST dashboard" }), "http://127.0.0.1:19119");
    await user.type(screen.getByRole("textbox", { name: "WebSocket dashboard" }), "ws://127.0.0.1:19119");
    await user.click(screen.getByRole("button", { name: "Guardar cifrado" }));

    await screen.findByRole("alert");
    await waitFor(() => {
      expect(screen.getByLabelText("Token dashboard (solo escritura)")).toHaveValue("");
      expect(screen.getByLabelText("API key fallback (solo escritura)")).toHaveValue("");
    });
    expect(screen.getByRole("textbox", { name: "Nombre" })).toHaveValue("Gateway remoto");
  });

  it("requires an exact 40-hex operator SHA on create and sends it write-only", async () => {
    vi.spyOn(api, "createGateway").mockResolvedValue({ id: "gateway-edit" });
    vi.spyOn(api, "refreshProfiles").mockResolvedValue(undefined);
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap());
    render(<GatewaysScreen />);
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Añadir gateway" }));
    await user.type(screen.getByRole("textbox", { name: "Nombre" }), "Gateway remoto");
    await user.type(screen.getByRole("textbox", { name: "REST dashboard" }), "http://127.0.0.1:19119");
    await user.type(screen.getByRole("textbox", { name: "WebSocket dashboard" }), "ws://127.0.0.1:19119");
    const sha = screen.getByLabelText("SHA fuente confiable (solo escritura, opcional)");
    await user.type(sha, "abc123");
    await user.click(screen.getByRole("button", { name: "Guardar cifrado" }));
    expect(await screen.findByText("Usa el SHA Git exacto de 40 caracteres hexadecimales")).toBeInTheDocument();
    expect(api.createGateway).not.toHaveBeenCalled();

    await user.clear(sha);
    await user.type(sha, "ABCDEF0123456789ABCDEF0123456789ABCDEF01");
    await user.click(screen.getByRole("button", { name: "Guardar cifrado" }));
    await waitFor(() => expect(api.createGateway).toHaveBeenCalledWith(
      expect.objectContaining({
        trustedSourceSha: "abcdef0123456789abcdef0123456789abcdef01",
      }),
      "csrf-memory-only",
    ));
    expect(screen.queryByLabelText("SHA fuente confiable (solo escritura, opcional)")).not.toBeInTheDocument();
  });

  it("replaces and revokes gateway trust without ever hydrating the SHA", async () => {
    useAppStore.setState({ gateways: [gateway] });
    vi.spyOn(api, "updateGateway").mockResolvedValue(gateway);
    vi.spyOn(api, "refreshProfiles").mockResolvedValue(undefined);
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap());
    render(<GatewaysScreen />);
    const user = userEvent.setup();

    await user.click(screen.getByRole("button", { name: "Editar confianza" }));
    const input = screen.getByLabelText("Nuevo SHA confiable (solo escritura)");
    expect(input).toHaveValue("");
    await user.type(input, "ABCDEF0123456789ABCDEF0123456789ABCDEF01");
    await user.click(screen.getByRole("button", { name: "Guardar y comprobar" }));
    await waitFor(() => expect(api.updateGateway).toHaveBeenCalledWith(
      "gateway-edit",
      { trustedSourceSha: "abcdef0123456789abcdef0123456789abcdef01" },
      "csrf-memory-only",
    ));
    expect(api.refreshProfiles).toHaveBeenCalledWith("gateway-edit", "csrf-memory-only");

    await user.click(screen.getByRole("button", { name: "Editar confianza" }));
    expect(screen.getByLabelText("Nuevo SHA confiable (solo escritura)")).toHaveValue("");
    await user.click(screen.getByRole("button", { name: "Volver a solo lectura" }));
    await waitFor(() => expect(api.updateGateway).toHaveBeenLastCalledWith(
      "gateway-edit",
      { trustedSourceSha: null },
      "csrf-memory-only",
    ));
  });
});
