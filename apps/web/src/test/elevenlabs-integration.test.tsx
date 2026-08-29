import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ElevenLabsIntegration } from "../components/ElevenLabsIntegration";
import i18n from "../i18n";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";

const integration = { configured: false, provider: "elevenlabs" as const, modelId: "scribe_v2_realtime" as const };
const configuredIntegration = { ...integration, configured: true };
const bootstrap = {
  gateways: [], profiles: [], workspaces: [], sessions: [], automations: [],
  features: { dictation: { available: true, provider: "elevenlabs" as const, modelId: "scribe_v2_realtime" as const } },
};

describe("owner-scoped ElevenLabs integration", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("es");
    useAppStore.setState({ authState: "authenticated", demoMode: false, csrfToken: "csrf-memory", bootstrapLoaded: true, features: undefined });
    vi.spyOn(api, "elevenLabsIntegration").mockResolvedValue(integration);
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap);
  });

  afterEach(() => vi.restoreAllMocks());

  it("saves a write-only key, refreshes presence, tests it, and deletes it", async () => {
    const save = vi.spyOn(api, "saveElevenLabsKey").mockResolvedValue(configuredIntegration);
    const test = vi.spyOn(api, "testElevenLabsIntegration").mockResolvedValue({ ok: true, provider: "elevenlabs", modelId: "scribe_v2_realtime" });
    const remove = vi.spyOn(api, "deleteElevenLabsKey").mockResolvedValue(undefined);
    const localStorageWrite = vi.spyOn(Storage.prototype, "setItem");
    const user = userEvent.setup();
    render(<ElevenLabsIntegration />);

    const key = await screen.findByPlaceholderText("Pega una API key");
    expect(key).toHaveAttribute("autocomplete", "off");
    await user.type(key, "sk_private_user_value");
    await user.click(screen.getByRole("button", { name: "Guardar cifrada" }));
    await waitFor(() => expect(save).toHaveBeenCalledWith("sk_private_user_value", "csrf-memory"));
    expect(key).toHaveValue("");
    expect(JSON.stringify(useAppStore.getState())).not.toContain("sk_private_user_value");
    expect(localStorageWrite).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Probar conexión" }));
    await waitFor(() => expect(test).toHaveBeenCalledWith("csrf-memory"));
    expect(await screen.findByText("Conexión verificada correctamente.")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Eliminar clave" }));
    const confirmation = screen.getByRole("group", { name: /Eliminar tu clave de ElevenLabs/ });
    await user.click(within(confirmation).getByRole("button", { name: "Eliminar" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("csrf-memory"));
    expect(await screen.findByText("Clave eliminada. El dictado quedó desactivado.")).toBeVisible();
  });

  it("clears a rejected key from the field and never exposes it in an error", async () => {
    vi.spyOn(api, "saveElevenLabsKey").mockRejectedValue(new Error("upstream rejected"));
    const user = userEvent.setup();
    render(<ElevenLabsIntegration />);
    const key = await screen.findByPlaceholderText("Pega una API key");
    await user.type(key, "sk_will_be_cleared");
    await user.click(screen.getByRole("button", { name: "Guardar cifrada" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo guardar la clave");
    expect(key).toHaveValue("");
    expect(document.body).not.toHaveTextContent("sk_will_be_cleared");
  });
});
