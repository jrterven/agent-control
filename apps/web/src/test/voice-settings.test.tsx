import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { VoiceSettings } from "../components/VoiceSettings";
import i18n from "../i18n";
import { api, type OpenAIIntegrationView } from "../lib/api";
import { useAppStore } from "../store/appStore";

const unconfigured = { configured: false, provider: "openai", modelId: "gpt-live-1" } as const;
const configured = { ...unconfigured, configured: true };

describe("voice provider settings", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("es");
    useAppStore.setState({
      authState: "authenticated", demoMode: false, csrfToken: "csrf-memory", bootstrapLoaded: true,
      features: { dictation: { available: true, provider: "elevenlabs", modelId: "scribe_v2_realtime" } },
    });
    vi.spyOn(api, "voiceSettings").mockResolvedValue({ provider: "elevenlabs" });
    vi.spyOn(api, "openaiIntegration").mockResolvedValue(unconfigured);
    vi.spyOn(api, "bootstrap").mockImplementation(async () => ({
      gateways: [], profiles: [], workspaces: [], sessions: [], automations: [], features: useAppStore.getState().features,
    }));
  });

  afterEach(() => vi.restoreAllMocks());

  it("defaults to ElevenLabs and requires a saved OpenAI key before selecting live mode", async () => {
    render(<VoiceSettings />);
    const provider = screen.getByRole("combobox", { name: "Proveedor de voz" });
    expect(provider).toHaveValue("elevenlabs");
    await waitFor(() => expect(provider).toBeEnabled());
    expect(within(provider).getByRole("option", { name: "GPT-Live-1 · OpenAI" })).toBeDisabled();
    expect(screen.getByText("Guarda una API key de OpenAI para habilitar GPT-Live-1.")).toBeVisible();
    expect(screen.getByText(/el micrófono y el contexto compartido se envían a OpenAI/)).toBeVisible();
  });

  it("clears the write-only key before the request finishes and enables either provider", async () => {
    let resolveSave!: (value: OpenAIIntegrationView) => void;
    const save = vi.spyOn(api, "saveOpenAIKey").mockImplementation(() => new Promise((resolve) => { resolveSave = resolve; }));
    const setProvider = vi.spyOn(api, "saveVoiceProvider").mockImplementation(async (provider) => ({ provider }));
    const storageWrite = vi.spyOn(Storage.prototype, "setItem");
    const user = userEvent.setup();
    render(<VoiceSettings />);
    const key = screen.getByLabelText("API key de OpenAI");
    await waitFor(() => expect(key).toBeEnabled());
    expect(key).toHaveAttribute("type", "password");
    expect(key).toHaveAttribute("autocomplete", "off");
    await user.type(key, "sk_openai_private");
    await user.click(screen.getByRole("button", { name: "Guardar cifrada" }));
    expect(save).toHaveBeenCalledWith("sk_openai_private", "csrf-memory");
    expect(key).toHaveValue("");
    expect(JSON.stringify(useAppStore.getState())).not.toContain("sk_openai_private");
    expect(storageWrite).not.toHaveBeenCalled();
    await act(async () => resolveSave(configured));

    const provider = screen.getByRole("combobox", { name: "Proveedor de voz" });
    await waitFor(() => expect(provider).toBeEnabled());
    await user.selectOptions(provider, "openai_live");
    await waitFor(() => expect(setProvider).toHaveBeenCalledWith("openai_live", "csrf-memory"));
    expect(provider).toHaveValue("openai_live");
    expect(useAppStore.getState().features?.voice?.provider).toBe("openai_live");
    expect(useAppStore.getState().features?.dictation.available).toBe(true);
    expect(screen.getByText(/GPT-Live-1 puede responder y enviar solicitudes al agente seleccionado/)).toBeVisible();

    await user.selectOptions(provider, "elevenlabs");
    await waitFor(() => expect(setProvider).toHaveBeenCalledWith("elevenlabs", "csrf-memory"));
    expect(useAppStore.getState().features?.voice?.provider).toBe("elevenlabs");
    expect(screen.getByLabelText("Reemplazar API key de OpenAI")).toHaveValue("");
  });

  it("does not expose a rejected key or raw provider error", async () => {
    vi.spyOn(api, "saveOpenAIKey").mockRejectedValue(new Error("sk_rejected_private"));
    const user = userEvent.setup();
    render(<VoiceSettings />);
    const key = screen.getByLabelText("API key de OpenAI");
    await waitFor(() => expect(key).toBeEnabled());
    await user.type(key, "sk_rejected_private");
    await user.click(screen.getByRole("button", { name: "Guardar cifrada" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo guardar la clave de OpenAI");
    expect(key).toHaveValue("");
    expect(document.body).not.toHaveTextContent("sk_rejected_private");
    expect(within(screen.getByRole("combobox")).getByRole("option", { name: "GPT-Live-1 · OpenAI" })).toBeDisabled();
  });

  it("keeps the saved provider selected when changing it fails", async () => {
    vi.mocked(api.openaiIntegration).mockResolvedValue(configured);
    vi.spyOn(api, "saveVoiceProvider").mockRejectedValue(new Error("provider rejected"));
    const user = userEvent.setup();
    render(<VoiceSettings />);
    const provider = screen.getByRole("combobox");
    await waitFor(() => expect(provider).toBeEnabled());
    await user.selectOptions(provider, "openai_live");
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo cambiar el modo de voz.");
    expect(provider).toHaveValue("elevenlabs");
    expect(useAppStore.getState().features?.voice?.provider).toBeUndefined();
  });

  it("removes the key and immediately resets live features even when bootstrap refresh fails", async () => {
    vi.mocked(api.openaiIntegration).mockResolvedValue(configured);
    vi.mocked(api.voiceSettings).mockResolvedValue({ provider: "openai_live" });
    vi.mocked(api.bootstrap).mockRejectedValue(new Error("temporary offline"));
    const remove = vi.spyOn(api, "deleteOpenAIKey").mockResolvedValue(undefined);
    useAppStore.setState((state) => ({ features: { ...state.features!, voice: { provider: "openai_live" }, live: { available: true, provider: "openai", modelId: "gpt-live-1" } } }));
    const user = userEvent.setup();
    render(<VoiceSettings />);
    const key = await screen.findByLabelText("Reemplazar API key de OpenAI");
    await user.type(key, "sk_unsaved_replacement");
    await user.click(screen.getByRole("button", { name: "Eliminar clave" }));
    const confirmation = screen.getByRole("group", { name: /Eliminar tu clave de OpenAI/ });
    await user.click(within(confirmation).getByRole("button", { name: "Eliminar" }));
    expect(remove).toHaveBeenCalledWith("csrf-memory");
    expect(await screen.findByText("Clave de OpenAI eliminada. Se seleccionó el modo ElevenLabs.")).toBeVisible();
    expect(screen.getByRole("combobox")).toHaveValue("elevenlabs");
    expect(screen.getByLabelText("API key de OpenAI")).toHaveValue("");
    expect(useAppStore.getState().features?.live?.available).toBe(false);
    expect(useAppStore.getState().features?.voice?.provider).toBe("elevenlabs");
    expect(useAppStore.getState().features?.dictation.available).toBe(true);
  });

  it("does not fetch credentials or permit provider changes offline", () => {
    useAppStore.setState({ authState: "offline" });
    render(<VoiceSettings />);
    expect(api.openaiIntegration).not.toHaveBeenCalled();
    expect(api.voiceSettings).not.toHaveBeenCalled();
    expect(screen.getByRole("combobox")).toBeDisabled();
    expect(screen.getByLabelText("API key de OpenAI")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Guardar cifrada" })).toBeDisabled();
  });
});
