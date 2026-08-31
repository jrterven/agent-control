import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ElevenLabsIntegration } from "../components/ElevenLabsIntegration";
import { gateways as demoGateways, profiles as demoProfiles } from "../data";
import i18n from "../i18n";
import { api, type ElevenLabsProfileVoiceView } from "../lib/api";
import { useAppStore } from "../store/appStore";

const integration = { configured: false, provider: "elevenlabs" as const, modelId: "scribe_v2_realtime" as const };
const configuredIntegration = { ...integration, configured: true, ttsModelId: "eleven_flash_v2_5" as const, speechAvailable: false, voiceId: null, voiceName: null };
const bootstrap = {
  gateways: [], profiles: [], workspaces: [], sessions: [], automations: [],
  features: { dictation: { available: true, provider: "elevenlabs" as const, modelId: "scribe_v2_realtime" as const } },
};
const profileAwareProfiles = demoProfiles.map((profile) => ({
  ...profile,
  speech: {
    available: true,
    modelId: "eleven_flash_v2_5" as const,
    voiceId: "voice-aria",
    voiceName: "Aria",
    inherited: true,
  },
}));

class FakeAudio {
  static instances: FakeAudio[] = [];
  src: string;
  preload = "";
  ended = false;
  paused = true;
  onplaying: (() => void) | null = null;
  onwaiting: (() => void) | null = null;
  onpause: (() => void) | null = null;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(src: string) {
    this.src = src;
    FakeAudio.instances.push(this);
  }

  play = vi.fn(async () => {
    this.paused = false;
    this.onplaying?.();
  });

  pause = vi.fn(() => {
    this.paused = true;
    this.onpause?.();
  });

  removeAttribute = vi.fn((name: string) => {
    if (name === "src") this.src = "";
  });

  load = vi.fn();
}

describe("owner-scoped ElevenLabs integration", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("es");
    useAppStore.setState({
      authState: "authenticated",
      demoMode: false,
      csrfToken: "csrf-memory",
      bootstrapLoaded: true,
      features: undefined,
      gateways: [],
      profiles: [],
      selectedGatewayId: "",
      selectedProfileId: "",
    });
    vi.spyOn(api, "elevenLabsIntegration").mockResolvedValue(integration);
    vi.spyOn(api, "bootstrap").mockResolvedValue(bootstrap);
    FakeAudio.instances = [];
    vi.stubGlobal("Audio", FakeAudio);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("saves a write-only key, refreshes presence, tests it, and deletes it", async () => {
    const save = vi.spyOn(api, "saveElevenLabsKey").mockResolvedValue(configuredIntegration);
    const test = vi.spyOn(api, "testElevenLabsIntegration").mockResolvedValue({ ok: true, provider: "elevenlabs", modelId: "scribe_v2_realtime" });
    const remove = vi.spyOn(api, "deleteElevenLabsKey").mockResolvedValue(undefined);
    const voices = vi.spyOn(api, "elevenLabsVoices").mockResolvedValue({ items: [
      { id: "voice-aria", name: "Aria", category: "premade", labels: {}, previewAvailable: true },
    ] });
    const saveVoice = vi.spyOn(api, "saveElevenLabsVoice").mockResolvedValue({
      ...configuredIntegration,
      ttsModelId: "eleven_multilingual_v2",
      speechAvailable: true,
      voiceId: "voice-aria",
      voiceName: "Aria",
    });
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

    const voice = await screen.findByRole("combobox", { name: "Voz predeterminada" });
    const model = screen.getByRole("combobox", { name: "Modelo de voz" });
    await waitFor(() => expect(voices).toHaveBeenCalled());
    await user.selectOptions(voice, "voice-aria");
    await user.selectOptions(model, "eleven_multilingual_v2");
    await user.click(screen.getByRole("button", { name: "Probar voz" }));
    await waitFor(() => expect(FakeAudio.instances).toHaveLength(1));
    expect(FakeAudio.instances[0]?.src).toBe("/api/v1/integrations/elevenlabs/voice-preview/voice-aria");
    expect(await screen.findByRole("button", { name: "Pausar prueba" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Pausar prueba" }));
    expect(await screen.findByRole("button", { name: "Continuar prueba" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Continuar prueba" }));
    expect(await screen.findByRole("button", { name: "Pausar prueba" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Guardar voz y modelo" }));
    await waitFor(() => expect(saveVoice).toHaveBeenCalledWith("voice-aria", "eleven_multilingual_v2", "csrf-memory"));
    expect(await screen.findByText("Voz predeterminada: Aria · Multilingual v2")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Probar conexión" }));
    await waitFor(() => expect(test).toHaveBeenCalledWith("csrf-memory"));
    expect(await screen.findByText("Conexión verificada correctamente.")).toBeVisible();

    await user.click(screen.getByRole("button", { name: "Eliminar clave" }));
    const confirmation = screen.getByRole("group", { name: /Eliminar tu clave de ElevenLabs/ });
    await user.click(within(confirmation).getByRole("button", { name: "Eliminar" }));
    await waitFor(() => expect(remove).toHaveBeenCalledWith("csrf-memory"));
    expect(await screen.findByText("Clave eliminada. El dictado y la lectura quedaron desactivados.")).toBeVisible();
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

  it("sets and resets a profile voice while identifying the agent by gateway", async () => {
    const configuredWithDefault = {
      ...configuredIntegration,
      speechAvailable: true,
      voiceId: "voice-aria",
      voiceName: "Aria",
    };
    vi.mocked(api.elevenLabsIntegration).mockResolvedValue(configuredWithDefault);
    vi.mocked(api.bootstrap).mockResolvedValue({
      ...bootstrap,
      gateways: demoGateways,
      profiles: profileAwareProfiles,
      features: {
        ...bootstrap.features,
        speech: { available: true, provider: "elevenlabs", modelId: "eleven_flash_v2_5", voiceId: "voice-aria", voiceName: "Aria" },
      },
    });
    useAppStore.setState({
      gateways: demoGateways,
      profiles: profileAwareProfiles,
      selectedGatewayId: "gateway-home",
      selectedProfileId: "profile-newton",
    });
    vi.spyOn(api, "elevenLabsVoices").mockResolvedValue({ items: [
      { id: "voice-aria", name: "Aria", category: "premade", labels: {}, previewAvailable: true },
      { id: "voice-roger", name: "Roger", category: "premade", labels: {}, previewAvailable: true },
    ] });
    const inherited = {
      profileId: "profile-newton",
      gatewayId: "gateway-home",
      profileName: "default",
      ttsModelId: "eleven_flash_v2_5" as const,
      voiceId: "voice-aria",
      voiceName: "Aria",
      speechAvailable: true,
      inherited: true,
    };
    const custom = {
      ...inherited,
      ttsModelId: "eleven_multilingual_v2",
      voiceId: "voice-roger",
      voiceName: "Roger",
      inherited: false,
    } as const;
    let newtonConfiguration: ElevenLabsProfileVoiceView = inherited;
    vi.spyOn(api, "elevenLabsProfileVoice").mockImplementation((requestedProfileId) => requestedProfileId === "profile-newton"
      ? Promise.resolve(newtonConfiguration)
      : Promise.reject(new Error("profile voice unavailable")));
    let resolveSave!: (configuration: typeof custom) => void;
    const saveProfileVoice = vi.spyOn(api, "saveElevenLabsProfileVoice").mockImplementation(() => new Promise((resolve) => {
      resolveSave = resolve;
    }));
    const deleteProfileVoice = vi.spyOn(api, "deleteElevenLabsProfileVoice").mockResolvedValue(inherited);
    const user = userEvent.setup();

    render(<ElevenLabsIntegration />);

    const profile = await screen.findByRole("combobox", { name: "Agente" });
    await waitFor(() => expect(api.elevenLabsProfileVoice).toHaveBeenCalledWith("profile-newton"));
    expect(profile).toHaveValue("profile-newton");
    expect(within(profile).getByRole("option", { name: "Newton · default · gx10-58f9" })).toBeVisible();
    const agentVoice = await screen.findByRole("combobox", { name: "Voz del agente" });
    const agentModel = screen.getByRole("combobox", { name: "Modelo del agente" });
    expect(agentVoice).toHaveValue("voice-aria");
    expect(await screen.findByText("Usa la predeterminada: Aria · Flash v2.5")).toBeVisible();

    await user.selectOptions(agentVoice, "voice-roger");
    await user.selectOptions(agentModel, "eleven_multilingual_v2");
    const profileVoiceSection = screen.getByText("Voces por agente").closest("section");
    expect(profileVoiceSection).not.toBeNull();
    await user.click(within(profileVoiceSection as HTMLElement).getByRole("button", { name: "Probar voz" }));
    await waitFor(() => expect(FakeAudio.instances.at(-1)?.src).toBe("/api/v1/integrations/elevenlabs/voice-preview/voice-roger"));
    await user.click(screen.getByRole("button", { name: "Guardar voz del agente" }));

    await waitFor(() => expect(saveProfileVoice).toHaveBeenCalledWith(
      "profile-newton",
      "voice-roger",
      "eleven_multilingual_v2",
      "csrf-memory",
    ));
    expect(screen.getByRole("combobox", { name: "Voz predeterminada" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Probar conexión" })).toBeDisabled();
    await act(async () => {
      newtonConfiguration = custom;
      resolveSave(custom);
    });
    expect(await screen.findByText("Voz propia: Roger · Multilingual v2")).toBeVisible();
    expect(screen.getByRole("combobox", { name: "Voz predeterminada" })).toBeEnabled();

    await user.selectOptions(profile, "profile-jarvis");
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo cargar la voz de este agente.");
    expect(screen.getByRole("combobox", { name: "Voz del agente" })).toHaveValue("");
    expect(screen.getByRole("combobox", { name: "Modelo del agente" })).toHaveValue("");
    expect(screen.queryByText("Voz propia: Roger · Multilingual v2")).not.toBeInTheDocument();

    await user.selectOptions(profile, "profile-newton");
    expect(await screen.findByText("Voz propia: Roger · Multilingual v2")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Volver a la predeterminada" }));
    await waitFor(() => expect(deleteProfileVoice).toHaveBeenCalledWith("profile-newton", "csrf-memory"));
    expect(await screen.findByText("Usa la predeterminada: Aria · Flash v2.5")).toBeVisible();
    expect(api.bootstrap).toHaveBeenCalledTimes(2);
  });

  it("hides profile voices and never requests them for a legacy bootstrap", async () => {
    vi.mocked(api.elevenLabsIntegration).mockResolvedValue({
      ...configuredIntegration,
      speechAvailable: true,
      voiceId: "voice-aria",
      voiceName: "Aria",
    });
    vi.spyOn(api, "elevenLabsVoices").mockResolvedValue({ items: [
      { id: "voice-aria", name: "Aria", category: "premade", labels: {}, previewAvailable: true },
    ] });
    const loadProfileVoice = vi.spyOn(api, "elevenLabsProfileVoice");
    useAppStore.setState({
      gateways: demoGateways,
      profiles: demoProfiles,
      selectedGatewayId: "gateway-home",
      selectedProfileId: "profile-newton",
    });

    render(<ElevenLabsIntegration />);

    expect(await screen.findByRole("combobox", { name: "Voz predeterminada" })).toBeVisible();
    expect(screen.queryByText("Voces por agente")).not.toBeInTheDocument();
    expect(loadProfileVoice).not.toHaveBeenCalled();
  });
});
