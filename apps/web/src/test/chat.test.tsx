import axe from "axe-core";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import i18n from "../i18n";
import { ChatView } from "../components/ChatView";
import { automations, gateways, initialMessages, profiles, sessions, workspaces } from "../data";
import { api } from "../lib/api";
import { textForSpeech } from "../hooks/useSpeechPlayback";
import { useAppStore } from "../store/appStore";

const chatScribeMock = vi.hoisted(() => {
  const handlers = new Map<string, (event: Record<string, unknown>) => void>();
  const connection = {
    on: vi.fn((event: string, handler: (payload: Record<string, unknown>) => void) => { handlers.set(event, handler); }),
    close: vi.fn(),
    mute: vi.fn(),
    commit: vi.fn(),
  };
  const connect = vi.fn(() => connection);
  return {
    connect,
    emit(event: string, payload: Record<string, unknown> = {}) { handlers.get(event)?.(payload); },
    reset() {
      handlers.clear();
      connect.mockClear();
      connection.on.mockClear();
      connection.close.mockClear();
      connection.mute.mockClear();
      connection.commit.mockClear();
    },
  };
});

vi.mock("../lib/elevenlabsScribeClient", () => ({
  Scribe: { connect: chatScribeMock.connect },
  CommitStrategy: { VAD: "vad" },
  RealtimeEvents: {
    SESSION_STARTED: "session_started",
    PARTIAL_TRANSCRIPT: "partial_transcript",
    COMMITTED_TRANSCRIPT: "committed_transcript",
    AUTH_ERROR: "auth_error",
    QUOTA_EXCEEDED: "quota_exceeded",
    RATE_LIMITED: "rate_limited",
    RESOURCE_EXHAUSTED: "resource_exhausted",
    ERROR: "error",
    CLOSE: "close",
  },
}));

function enableBrowserAudio() {
  Object.defineProperty(window, "isSecureContext", { configurable: true, value: true });
  Object.defineProperty(navigator, "onLine", { configurable: true, value: true });
  Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn() } });
  Object.defineProperty(window, "AudioContext", { configurable: true, value: class AudioContext {} });
  Object.defineProperty(window, "AudioWorkletNode", { configurable: true, value: class AudioWorkletNode {} });
  Object.defineProperty(window, "WebSocket", { configurable: true, value: class WebSocket {} });
}

describe("mobile-first chat", () => {
  beforeEach(() => {
    chatScribeMock.reset();
    useAppStore.setState({
      authState: "authenticated",
      csrfToken: "csrf-memory-only",
      demoMode: true,
      selectedProfileId: "profile-newton",
      selectedSessionId: "session-papers",
      selectedGatewayId: "gateway-home",
      selectedWorkspaceId: "workspace-papers",
      gateways,
      profiles,
      sessions,
      workspaces,
      automations,
      messages: initialMessages,
      streamingBySession: {},
      features: undefined,
    });
  });

  afterEach(async () => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    await i18n.changeLanguage("es");
  });

  it("renders the approved conversation hierarchy without duplicating tool activity", async () => {
    const { container } = render(<ChatView />);
    expect(screen.getByRole("heading", { name: "Memoria de agentes · agosto" })).toBeInTheDocument();
    expect(screen.getByText("Comparativa de memoria de agentes — Agosto 2026")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Herramientas · 2/i })).not.toBeInTheDocument();
    expect((await axe.run(container)).violations).toHaveLength(0);
  });

  it("turns the typing label into a six-line, scrollable public activity disclosure", async () => {
    const streamingMessage = {
      id: "assistant-progress",
      sessionId: "session-papers",
      role: "assistant" as const,
      content: "Borrador parcial",
      createdAt: "10:42",
      streaming: true,
      activity: [
        { id: "search-1", kind: "tool" as const, label: "Búsqueda", summary: "Revisando fuentes", status: "running" as const },
        { id: "delegate-1", kind: "delegation" as const, label: "Investigador", summary: "Comparando resultados", status: "completed" as const },
      ],
    };
    useAppStore.setState({
      messages: [...initialMessages, streamingMessage],
      streamingBySession: { "session-papers": streamingMessage.id },
    });
    const user = userEvent.setup();
    const { container } = render(<ChatView />);

    const toggle = screen.getByRole("button", { name: "Mostrar actividad de Newton" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("log", { name: "Actividad de Newton" })).not.toBeInTheDocument();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAccessibleName("Ocultar actividad de Newton");
    const log = screen.getByRole("log", { name: "Actividad de Newton" });
    expect(log).toHaveClass("agent-activity__log");
    expect(screen.getByText("Analizando la solicitud")).toBeVisible();
    expect(screen.getByText("Búsqueda")).toBeVisible();
    expect(screen.getByText("Revisando fuentes")).toBeVisible();
    expect(screen.getByText("Redactando la respuesta")).toBeVisible();

    let scrollHeight = 320;
    Object.defineProperty(log, "scrollHeight", { configurable: true, get: () => scrollHeight });
    Object.defineProperty(log, "clientHeight", { configurable: true, get: () => 100 });
    act(() => {
      useAppStore.getState().updateMessage(streamingMessage.id, {
        activity: [...streamingMessage.activity, {
          id: "search-2", kind: "tool", label: "Búsqueda", summary: "Fuentes verificadas", status: "completed",
        }],
      });
    });
    await waitFor(() => expect(log.scrollTop).toBe(320));

    scrollHeight = 500;
    log.scrollTop = 120;
    fireEvent.scroll(log);
    act(() => {
      const current = useAppStore.getState().messages.find((message) => message.id === streamingMessage.id);
      useAppStore.getState().updateMessage(streamingMessage.id, {
        activity: [...(current?.activity ?? []), {
          id: "search-3", kind: "tool", label: "Búsqueda", summary: "Última comprobación", status: "completed",
        }],
      });
    });
    await waitFor(() => expect(screen.getByText("Última comprobación")).toBeVisible());
    expect(log.scrollTop).toBe(120);
    expect((await axe.run(container)).violations).toHaveLength(0);
  });

  it("changes chat controls to English without translating conversation content", async () => {
    await i18n.changeLanguage("en");
    render(<ChatView />);

    expect(screen.getByText("August 28, 2026")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Message Newton…" })).toBeVisible();
    expect(screen.queryByRole("button", { name: "Tools · 2" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Memoria de agentes · agosto" })).toBeVisible();
    expect(screen.getByText("Comparativa de memoria de agentes — Agosto 2026")).toBeVisible();
  });

  it("starts at one line and scrolls only after the six-line composer limit", async () => {
    render(<ChatView />);
    const composer = screen.getByRole("textbox", { name: "Mensaje a Newton…" }) as HTMLTextAreaElement;
    let measuredScrollHeight = 44;
    Object.defineProperty(composer, "scrollHeight", {
      configurable: true,
      get: () => measuredScrollHeight,
    });

    fireEvent.change(composer, { target: { value: "Una línea" } });
    await waitFor(() => expect(composer.style.height).toBe("44px"));
    expect(composer.rows).toBe(1);
    expect(composer.style.overflowY).toBe("hidden");

    measuredScrollHeight = 1_000;
    fireEvent.change(composer, { target: { value: "1\n2\n3\n4\n5\n6\n7" } });
    await waitFor(() => expect(composer.style.overflowY).toBe("auto"));
    expect(Number.parseFloat(composer.style.height)).toBeGreaterThanOrEqual(130);
    expect(Number.parseFloat(composer.style.height)).toBeLessThanOrEqual(160);
  });

  it("shows the microphone only when the current user configured dictation and the browser supports it", () => {
    enableBrowserAudio();

    const first = render(<ChatView />);
    expect(screen.queryByRole("button", { name: "Dictar por voz" })).not.toBeInTheDocument();
    first.unmount();

    useAppStore.setState({
      features: {
        dictation: { available: true, provider: "elevenlabs", modelId: "scribe_v2_realtime" },
      },
    });
    render(<ChatView />);
    expect(screen.getByRole("button", { name: "Dictar por voz" })).toBeVisible();
  });

  it("shows live listening and a speaker on completed responses only after a voice is selected", () => {
    const first = render(<ChatView />);
    expect(screen.queryByRole("checkbox", { name: "Escuchar respuestas en vivo" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Escuchar esta respuesta" })).not.toBeInTheDocument();
    first.unmount();

    useAppStore.setState({
      features: {
        dictation: { available: false, provider: "elevenlabs", modelId: "scribe_v2_realtime" },
        speech: { available: true, provider: "elevenlabs", modelId: "eleven_flash_v2_5", voiceId: "voice-aria", voiceName: "Aria" },
      },
    });
    render(<ChatView />);
    expect(screen.getByRole("checkbox", { name: "Escuchar respuestas en vivo" })).toBeVisible();
    expect(screen.getAllByRole("button", { name: "Escuchar esta respuesta" }).length).toBeGreaterThan(0);
  });

  it("turns Markdown into bounded, speakable response text", () => {
    expect(textForSpeech("## Informe\n**Listo** [detalle](https://example.com)\n```sh\nsecret\n```\nMEDIA:[private](/tmp/a.mp3)"))
      .toBe("Informe Listo detalle");
  });

  it("claims Android media playback from the speaker tap before downloading speech", async () => {
    class FakeSourceBuffer extends EventTarget {
      mode = "segments";
      updating = false;

      appendBuffer() {
        queueMicrotask(() => this.dispatchEvent(new Event("updateend")));
      }
    }

    class FakeMediaSource extends EventTarget {
      static isTypeSupported = vi.fn(() => true);
      static instances: FakeMediaSource[] = [];
      readyState = "open";
      sourceBuffer = new FakeSourceBuffer();
      addSourceBuffer = vi.fn(() => this.sourceBuffer);
      endOfStream = vi.fn(() => { this.readyState = "ended"; });

      constructor() {
        super();
        FakeMediaSource.instances.push(this);
      }
    }

    class FakeAudio extends EventTarget {
      static instances: FakeAudio[] = [];
      src = "";
      preload = "";
      playbackRate = 1;
      paused = true;
      ended = false;
      currentTime = 0;
      play = vi.fn(async () => {
        this.paused = false;
        this.dispatchEvent(new Event("playing"));
      });
      pause = vi.fn(() => { this.paused = true; });
      load = vi.fn();
      removeAttribute = vi.fn((name: string) => { if (name === "src") this.src = ""; });

      constructor() {
        super();
        FakeAudio.instances.push(this);
      }
    }

    const NativeURL = URL;
    vi.stubGlobal("Audio", FakeAudio);
    vi.stubGlobal("MediaSource", FakeMediaSource);
    vi.stubGlobal("URL", class extends NativeURL {
      static createObjectURL = vi.fn(() => "blob:history-audio");
      static revokeObjectURL = vi.fn();
    });
    useAppStore.setState({
      features: {
        dictation: { available: false, provider: "elevenlabs", modelId: "scribe_v2_realtime" },
        speech: { available: true, provider: "elevenlabs", modelId: "eleven_flash_v2_5", voiceId: "voice-aria", voiceName: "Aria" },
      },
    });
    let releaseSpeech!: (response: Response) => void;
    const streamSpeech = vi.spyOn(api, "streamSpeech").mockImplementation(() => new Promise((resolve) => {
      releaseSpeech = resolve;
    }));
    const user = userEvent.setup();
    render(<ChatView />);

    await user.click(screen.getAllByRole("button", { name: "Escuchar esta respuesta" })[0]);
    expect(FakeAudio.instances).toHaveLength(1);
    expect(FakeAudio.instances[0].play).toHaveBeenCalledTimes(1);
    expect(streamSpeech).toHaveBeenCalledTimes(1);
    expect(FakeAudio.instances[0].play.mock.invocationCallOrder[0])
      .toBeLessThan(streamSpeech.mock.invocationCallOrder[0]);

    releaseSpeech(new Response(new Uint8Array([0x49, 0x44, 0x33, 0x04]), {
      headers: { "Content-Type": "audio/mpeg" },
    }));
    await waitFor(() => expect(FakeMediaSource.instances[0].endOfStream).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText("Reproduciendo")).toBeVisible());
  });

  it("requires explicit first-use consent before requesting a token or microphone", async () => {
    enableBrowserAudio();
    useAppStore.setState({
      features: {
        dictation: { available: true, provider: "elevenlabs", modelId: "scribe_v2_realtime" },
      },
    });
    const token = vi.spyOn(api, "createTranscriptionToken").mockResolvedValue({
      token: "single-use-token",
      expiresAt: "2026-08-29T12:15:00Z",
      modelId: "scribe_v2_realtime",
    });
    const user = userEvent.setup();
    render(<ChatView />);

    await user.click(screen.getByRole("button", { name: "Dictar por voz" }));
    expect(screen.getByRole("dialog", { name: "Activar dictado por voz" })).toBeVisible();
    expect(screen.getByText(/nunca se envía automáticamente al agente/i)).toBeVisible();
    expect(token).not.toHaveBeenCalled();
    expect(chatScribeMock.connect).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Aceptar y activar micrófono" }));
    await waitFor(() => expect(token).toHaveBeenCalledWith({ sessionId: "session-papers" }, "csrf-memory-only"));
    await waitFor(() => expect(chatScribeMock.connect).toHaveBeenCalledTimes(1));
  });

  it("previews provisional dictation inside the growing composer and only keeps confirmed text", async () => {
    enableBrowserAudio();
    const dictationSession = { ...sessions[0], id: "session-dictation-preview" };
    useAppStore.setState({
      selectedSessionId: dictationSession.id,
      sessions: [...sessions, dictationSession],
      messages: [],
      features: {
        dictation: { available: true, provider: "elevenlabs", modelId: "scribe_v2_realtime" },
      },
    });
    vi.spyOn(api, "createTranscriptionToken").mockResolvedValue({
      token: "single-use-token",
      expiresAt: "2026-08-29T12:15:00Z",
      modelId: "scribe_v2_realtime",
    });
    const user = userEvent.setup();
    render(<ChatView />);
    const composer = screen.getByRole("textbox", { name: "Mensaje a Newton…" }) as HTMLTextAreaElement;

    await user.type(composer, "Antes después");
    composer.setSelectionRange(5, 5);
    await user.click(screen.getByRole("button", { name: "Dictar por voz" }));
    await user.click(screen.getByRole("button", { name: "Aceptar y activar micrófono" }));
    await waitFor(() => expect(chatScribeMock.connect).toHaveBeenCalledTimes(1));

    act(() => chatScribeMock.emit("partial_transcript", { text: "texto provisional de varias palabras" }));
    expect(composer).toHaveValue("Antes texto provisional de varias palabras después");
    expect(composer).toHaveAttribute("readonly");
    expect(screen.getByText(/^Transcripción provisional:/)).toHaveClass("dictation-state__announcement");

    act(() => chatScribeMock.emit("partial_transcript", { text: "hipótesis corregida" }));
    expect(composer).toHaveValue("Antes hipótesis corregida después");

    act(() => chatScribeMock.emit("committed_transcript", { text: "texto confirmado" }));
    await waitFor(() => expect(composer).toHaveValue("Antes texto confirmado después"));
  });

  it("streams a demo response and offers an explicit stop action", async () => {
    const user = userEvent.setup();
    render(<ChatView />);
    const composer = screen.getByRole("textbox", { name: "Mensaje a Newton…" });
    await user.type(composer, "Resume los riesgos{Enter}");
    expect(await screen.findByRole("button", { name: /Detener/i })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Detener/i }));
    await waitFor(() => expect(useAppStore.getState().streamingBySession["session-papers"]).toBeUndefined());
  });

  it("does not borrow another session when the selected profile has no chat in the workspace", () => {
    useAppStore.setState({
      demoMode: false,
      selectedProfileId: "profile-newton",
      selectedSessionId: "",
    });

    render(<ChatView />);

    expect(screen.getByRole("heading", { name: "Newton está en modo solo lectura" })).toBeVisible();
    expect(screen.getByText("Para escribir, selecciona el entorno de pruebas.")).toBeVisible();
    expect(screen.queryByRole("heading", { name: "Memoria de agentes · agosto" })).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("offers a primary new-chat action in the empty conversation and uses the active workspace", async () => {
    const user = userEvent.setup();
    const writableProfile = {
      ...profiles[0],
      mutable: true,
      capabilities: { ...gateways[0].capabilities, sessions: true },
    };
    const createdSession = {
      ...sessions[0],
      id: "session-created-from-empty-state",
      profileId: writableProfile.id,
      workspaceId: "workspace-papers",
      title: "Nuevo chat",
    };
    useAppStore.setState({
      demoMode: false,
      selectedProfileId: writableProfile.id,
      selectedSessionId: "",
      selectedWorkspaceId: "workspace-papers",
      profiles: [writableProfile],
      sessions: [],
      messages: [],
    });
    const createSession = vi.spyOn(api, "createSession").mockResolvedValue(createdSession);

    render(<ChatView />);
    const action = screen.getByRole("button", { name: "Nuevo chat" });
    expect(action).toBeVisible();
    await user.click(action);

    await waitFor(() => expect(createSession).toHaveBeenCalledWith(
      writableProfile.id,
      "workspace-papers",
      "csrf-memory-only",
    ));
    expect(useAppStore.getState().selectedSessionId).toBe(createdSession.id);
  });
});
