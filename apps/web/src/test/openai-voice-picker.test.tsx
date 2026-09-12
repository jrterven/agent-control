import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ElevenLabsIntegration } from "../components/ElevenLabsIntegration";
import { OpenAIVoicePicker } from "../components/OpenAIVoicePicker";
import i18n from "../i18n";
import { api } from "../lib/api";
import { OPENAI_LIVE_VOICES } from "../lib/openaiLiveVoices";
import { useAppStore } from "../store/appStore";

type PreviewOptions = ConstructorParameters<typeof import("../lib/openaiLiveVoicePreview").OpenAILiveVoicePreview>[0];
const previewMock = vi.hoisted(() => {
  class Preview {
    static instances: Preview[] = [];
    readonly abort = new AbortController();
    disposed = false;
    constructor(readonly options: PreviewOptions) { Preview.instances.push(this); }
    start = vi.fn(async () => {
      this.options.onPhase("connecting");
      await this.options.negotiate("sample-offer", this.abort.signal);
      if (!this.disposed) this.options.onPhase("listening");
    });
    dispose = vi.fn(() => { this.disposed = true; this.abort.abort(); });
    stop = vi.fn(() => this.options.onPhase("stopping"));
    play = vi.fn(async () => this.options.onPlaybackBlocked(false));
  }
  return { Preview, supported: vi.fn(() => true) };
});

vi.mock("../lib/openaiLiveVoicePreview", () => ({
  OpenAILiveVoicePreview: previewMock.Preview,
  voicePreviewSupported: previewMock.supported,
}));

class FakeAudio {
  static instances: FakeAudio[] = [];
  preload = "";
  ended = false;
  onplaying: (() => void) | null = null;
  onwaiting: (() => void) | null = null;
  onpause: (() => void) | null = null;
  onended: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public src: string) { FakeAudio.instances.push(this); }
  play = vi.fn(async () => this.onplaying?.());
  pause = vi.fn(() => this.onpause?.());
  removeAttribute = vi.fn((name: string) => { if (name === "src") this.src = ""; });
  load = vi.fn();
}

describe("GPT Live voice selection and samples", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("es");
    useAppStore.setState({
      authState: "authenticated", demoMode: false, csrfToken: "csrf-memory",
      features: undefined, profiles: [], gateways: [], selectedProfileId: "",
    });
    previewMock.Preview.instances = [];
    previewMock.supported.mockReturnValue(true);
    FakeAudio.instances = [];
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
    vi.spyOn(api, "openaiVoice").mockResolvedValue({ voiceId: "marin" });
    vi.spyOn(api, "createLiveVoicePreview").mockResolvedValue({ session: { id: "sample" }, transport: { type: "webrtc", sdp: "answer" } });
  });

  afterEach(() => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("saves any catalog voice without a key, preserves mode, and reloads the saved choice", async () => {
    const save = vi.spyOn(api, "saveOpenAIVoice").mockImplementation(async (voiceId) => {
      vi.mocked(api.openaiVoice).mockResolvedValue({ voiceId });
      return { voiceId };
    });
    const saveProvider = vi.spyOn(api, "saveVoiceProvider");
    const saveKey = vi.spyOn(api, "saveOpenAIKey");
    const bootstrap = vi.spyOn(api, "bootstrap");
    const user = userEvent.setup();
    const first = render(<OpenAIVoicePicker configured={false} />);
    const voice = screen.getByRole("combobox", { name: "Voz de GPT-Live-1" });
    await waitFor(() => expect(voice).toBeEnabled());
    expect(voice).toHaveValue("marin");
    expect(within(voice).getAllByRole("option")).toHaveLength(22);
    expect(within(voice).getAllByRole("option").map((option) => option.getAttribute("value"))).toEqual(OPENAI_LIVE_VOICES.map((item) => item.id));
    expect(screen.getByRole("button", { name: "Probar voz" })).toBeDisabled();
    await user.selectOptions(voice, "coral");
    expect(previewMock.Preview.instances).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Guardar voz" }));
    expect(save).toHaveBeenCalledWith("coral", "csrf-memory");
    expect(await screen.findByText("Voz guardada para la próxima conversación.")).toBeVisible();
    expect(screen.getByText("Voz guardada: Coral")).toBeVisible();
    expect(saveProvider).not.toHaveBeenCalled();
    expect(saveKey).not.toHaveBeenCalled();
    expect(bootstrap).not.toHaveBeenCalled();
    expect(api.createLiveVoicePreview).not.toHaveBeenCalled();
    first.unmount();
    render(<OpenAIVoicePicker configured={false} />);
    await waitFor(() => expect(screen.getByRole("combobox")).toHaveValue("coral"));
  });

  it("retains the last saved voice when saving fails and keeps the attempted choice for retry", async () => {
    vi.spyOn(api, "saveOpenAIVoice").mockRejectedValue(new Error("temporary error"));
    const user = userEvent.setup();
    render(<OpenAIVoicePicker configured={false} />);
    const voice = screen.getByRole("combobox");
    await waitFor(() => expect(voice).toBeEnabled());
    await user.selectOptions(voice, "ash");
    await user.click(screen.getByRole("button", { name: "Guardar voz" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudo guardar la voz. Tu voz guardada no cambió.");
    expect(screen.getByText("Voz guardada: Marin")).toBeVisible();
    expect(voice).toHaveValue("ash");
    expect(screen.getByRole("button", { name: "Guardar voz" })).toBeEnabled();
    expect(previewMock.Preview.instances).toHaveLength(0);
  });

  it("starts a sample only after a click, discloses quota, and sends the selected voice and interface language", async () => {
    const user = userEvent.setup();
    render(<OpenAIVoicePicker configured />);
    const voice = screen.getByRole("combobox");
    await waitFor(() => expect(voice).toBeEnabled());
    await user.selectOptions(voice, "willow");
    expect(previewMock.Preview.instances).toHaveLength(0);
    expect(screen.getByText(/factura al menos 15 segundos; no activa el micrófono/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Probar voz" }));
    expect(previewMock.Preview.instances).toHaveLength(1);
    expect(api.createLiveVoicePreview).toHaveBeenCalledWith({ sdp: "sample-offer", voiceId: "willow", language: "es" }, "csrf-memory", expect.any(AbortSignal));
    expect(screen.getByRole("button", { name: "Detener muestra" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Detener muestra" }));
    expect(previewMock.Preview.instances[0].stop).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Detener muestra" })).toBeDisabled();
    expect(previewMock.Preview.instances[0].dispose).not.toHaveBeenCalled();
    act(() => previewMock.Preview.instances[0].options.onPhase("idle"));
    expect(previewMock.Preview.instances[0].dispose).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Probar voz" })).toBeEnabled();
  });

  it("stops a pending sample on selection and ignores its late failure", async () => {
    let rejectSample!: (error: Error) => void;
    vi.mocked(api.createLiveVoicePreview).mockImplementationOnce(() => new Promise((_resolve, reject) => { rejectSample = reject; }));
    const user = userEvent.setup();
    render(<OpenAIVoicePicker configured />);
    const voice = screen.getByRole("combobox");
    await waitFor(() => expect(voice).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Probar voz" }));
    expect(screen.getByRole("button", { name: "Detener muestra" })).toHaveTextContent("Generando muestra…");
    const previous = previewMock.Preview.instances[0];
    await user.selectOptions(voice, "cedar");
    expect(previous.dispose).toHaveBeenCalledTimes(1);
    expect(previous.abort.signal.aborted).toBe(true);
    await user.click(screen.getByRole("button", { name: "Probar voz" }));
    await act(async () => rejectSample(new Error("late failure")));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Detener muestra" })).toBeVisible();
    expect(previewMock.Preview.instances[1].dispose).not.toHaveBeenCalled();
  });

  it.each(["offline", "hidden", "pagehide", "unmount", "keyRemoval"] as const)("disposes a sample on %s and never automatically restarts", async (transition) => {
    const user = userEvent.setup();
    const view = render(<OpenAIVoicePicker configured />);
    await user.click(screen.getByRole("button", { name: "Probar voz" }));
    const preview = previewMock.Preview.instances[0];
    act(() => {
      if (transition === "offline") fireEvent(window, new Event("offline"));
      if (transition === "hidden") {
        vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
        fireEvent(document, new Event("visibilitychange"));
      }
      if (transition === "pagehide") fireEvent(window, new Event("pagehide"));
      if (transition === "unmount") view.unmount();
      if (transition === "keyRemoval") view.rerender(<OpenAIVoicePicker configured={false} />);
    });
    expect(preview.dispose).toHaveBeenCalledTimes(1);
    expect(previewMock.Preview.instances).toHaveLength(1);
    expect(screen.queryByRole("button", { name: "Detener muestra" })).not.toBeInTheDocument();
  });

  it("reports quota failures and recovers blocked playback without creating another billable sample", async () => {
    const user = userEvent.setup();
    render(<OpenAIVoicePicker configured />);
    await user.click(screen.getByRole("button", { name: "Probar voz" }));
    const preview = previewMock.Preview.instances[0];
    act(() => preview.options.onPlaybackBlocked(true));
    await user.click(screen.getByRole("button", { name: "Reproducir muestra" }));
    expect(preview.play).toHaveBeenCalledTimes(1);
    expect(api.createLiveVoicePreview).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Reproducir muestra" })).not.toBeInTheDocument();
    act(() => { preview.options.onIssue("quota"); preview.options.onPhase("error"); });
    expect(screen.getByRole("alert")).toHaveTextContent("OpenAI no tiene cuota disponible");
    expect(preview.dispose).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Probar voz" })).toBeEnabled();
  });

  it("allows saving when this browser cannot preview", async () => {
    previewMock.supported.mockReturnValue(false);
    const user = userEvent.setup();
    render(<OpenAIVoicePicker configured />);
    const voice = screen.getByRole("combobox");
    await waitFor(() => expect(voice).toBeEnabled());
    await user.selectOptions(voice, "verse");
    expect(screen.getByRole("button", { name: "Guardar voz" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Probar voz" })).toBeDisabled();
    expect(screen.getByText("Este navegador no permite generar muestras de voz.")).toBeVisible();
  });

  it("prevents overlapping OpenAI and ElevenLabs samples in either direction", async () => {
    vi.stubGlobal("Audio", FakeAudio);
    vi.spyOn(api, "elevenLabsIntegration").mockResolvedValue({ configured: true, provider: "elevenlabs", modelId: "scribe_v2_realtime", voiceId: "voice-aria" });
    vi.spyOn(api, "elevenLabsVoices").mockResolvedValue({ items: [{ id: "voice-aria", name: "Aria", previewAvailable: true, labels: {} }] });
    const user = userEvent.setup();
    render(<>
      <section aria-label="OpenAI settings"><OpenAIVoicePicker configured /></section>
      <section aria-label="ElevenLabs settings"><ElevenLabsIntegration /></section>
    </>);
    const openai = within(screen.getByRole("region", { name: "OpenAI settings" }));
    const elevenlabs = within(screen.getByRole("region", { name: "ElevenLabs settings" }));
    await user.click(openai.getByRole("button", { name: "Probar voz" }));
    await waitFor(() => expect(elevenlabs.getByRole("button", { name: "Probar voz" })).toBeEnabled());
    await user.click(elevenlabs.getByRole("button", { name: "Probar voz" }));
    expect(previewMock.Preview.instances[0].dispose).toHaveBeenCalledTimes(1);
    expect(FakeAudio.instances).toHaveLength(1);
    expect(FakeAudio.instances[0].play).toHaveBeenCalledTimes(1);
    await user.click(openai.getByRole("button", { name: "Probar voz" }));
    expect(FakeAudio.instances[0].pause).toHaveBeenCalledTimes(1);
    expect(FakeAudio.instances[0].src).toBe("");
    expect(previewMock.Preview.instances).toHaveLength(2);
  });
});
