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
    vi.spyOn(api, "openaiVoice").mockResolvedValue({ voiceId: "marin" });
    vi.spyOn(api, "bootstrap").mockImplementation(async () => ({
      gateways: [], profiles: [], workspaces: [], sessions: [], automations: [], features: useAppStore.getState().features,
    }));
  });

  afterEach(() => vi.restoreAllMocks());

  it("explains independent voice buttons without an exclusive provider selector", async () => {
    render(<VoiceSettings />);
    await waitFor(() => expect(screen.getByLabelText("API key de OpenAI")).toBeEnabled());
    expect(screen.queryByRole("combobox", { name: "Proveedor de voz" })).not.toBeInTheDocument();
    expect(api.voiceSettings).not.toHaveBeenCalled();
    expect(screen.getByText(/Solo aparecen los botones configurados/)).toBeVisible();
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

    expect(setProvider).not.toHaveBeenCalled();
    expect(useAppStore.getState().features?.live?.available).toBe(true);
    expect(useAppStore.getState().features?.dictation.available).toBe(true);
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
    expect(useAppStore.getState().features?.live?.available).not.toBe(true);
  });

  it("reports failed credential loading without requiring the legacy mode endpoint", async () => {
    vi.mocked(api.openaiIntegration).mockRejectedValue(new Error("unavailable"));
    render(<VoiceSettings />);
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo cargar la configuración de voz.");
    expect(screen.getByLabelText("API key de OpenAI")).toBeDisabled();
    expect(api.voiceSettings).not.toHaveBeenCalled();
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
    expect(await screen.findByText("Clave de OpenAI eliminada. El botón de GPT Live ya no aparece.")).toBeVisible();
    expect(screen.getByLabelText("API key de OpenAI")).toHaveValue("");
    expect(useAppStore.getState().features?.live?.available).toBe(false);
    expect(useAppStore.getState().features?.dictation.available).toBe(true);
  });

  it("does not fetch credentials or permit provider changes offline", () => {
    useAppStore.setState({ authState: "offline" });
    render(<VoiceSettings />);
    expect(api.openaiIntegration).not.toHaveBeenCalled();
    expect(api.voiceSettings).not.toHaveBeenCalled();
    expect(screen.queryByRole("combobox", { name: "Proveedor de voz" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("API key de OpenAI")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Guardar cifrada" })).toBeDisabled();
  });

  it("keeps API key settings usable when the separate saved voice request fails", async () => {
    vi.mocked(api.openaiVoice).mockRejectedValue(new Error("voice request unavailable"));
    render(<VoiceSettings />);
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo cargar la voz guardada.");
    await waitFor(() => expect(screen.getByLabelText("API key de OpenAI")).toBeEnabled());
    expect(screen.getByRole("combobox", { name: "Voz de GPT-Live-1" })).toBeDisabled();
  });
});
