import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { webcrypto } from "node:crypto";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatView } from "../components/ChatView";
import { rehydrateSession } from "../hooks";
import { automations, gateways, initialMessages, profiles, sessions, workspaces } from "../data";
import i18n from "../i18n";
import { api } from "../lib/api";
import { liveConversationSeparator, liveDelegationPrefix } from "../lib/liveDelegation";
import { db, loadDraft } from "../lib/db";
import { usePwaUpdateStore } from "../lib/pwaUpdate";
import { useAppStore } from "../store/appStore";

type LiveOptions = ConstructorParameters<typeof import("../lib/openaiLiveClient").OpenAILiveClient>[0];

// Keep the real useOpenAILive lifecycle, replacing only the browser transport.
const transport = vi.hoisted(() => {
  class Client {
    static instances: Client[] = [];
    constructor(readonly options: LiveOptions) { Client.instances.push(this); }
    start = vi.fn(async () => { this.options.onPhase("listening"); });
    stop = vi.fn(() => { this.options.onPhase("idle"); });
    setPaused = vi.fn((paused: boolean) => { this.options.onPhase(paused ? "paused" : "listening"); });
    dispose = vi.fn();
    play = vi.fn(async () => { this.options.onPlaybackBlocked(false); });
  }
  return { Client, scribeCommit: (_text: string) => {}, scribeStart: vi.fn(), scribeStop: vi.fn(), liveSupported: vi.fn(() => true) };
});

vi.mock("../lib/openaiLiveClient", async (importOriginal) => ({
  ...await importOriginal<typeof import("../lib/openaiLiveClient")>(),
  OpenAILiveClient: transport.Client,
  liveSupported: transport.liveSupported,
}));

vi.mock("../hooks/useScribeDictation", async () => {
  const { useState, useEffect } = await import("react");
  return { useScribeDictation: ({ enabled, sessionId, onCommitted }: { enabled: boolean; sessionId: string; onCommitted: (text: string) => void }) => {
    const [phase, setPhase] = useState("idle");
    useEffect(() => setPhase("idle"), [enabled, sessionId]);
    transport.scribeCommit = onCommitted;
    return {
      available: enabled, supported: true, phase, active: ["listening", "paused"].includes(phase), issue: null, partial: "",
      start: async () => { transport.scribeStart(); setPhase("listening"); },
      stop: () => { transport.scribeStop(); setPhase("idle"); },
      pause: () => setPhase("paused"), resume: () => setPhase("listening"),
    };
  } };
});

const features = {
  dictation: { available: true, provider: "elevenlabs", modelId: "scribe_v2_realtime" },
  speech: { available: true, provider: "elevenlabs", modelId: "eleven_flash_v2_5", voiceId: "voice-aria", voiceName: "Aria" },
  live: { available: true, provider: "openai", modelId: "gpt-live-1" },
} as const;

function chooseLive() {
  useAppStore.setState({ features: { ...features, voice: { provider: "openai_live" } } });
}

describe("live voice in the chat", () => {
  beforeEach(async () => {
    vi.stubGlobal("crypto", webcrypto);
    await i18n.changeLanguage("es");
    await db.drafts.clear();
    transport.Client.instances = [];
    transport.scribeStart.mockClear();
    transport.scribeStop.mockClear();
    transport.liveSupported.mockReturnValue(true);
    vi.spyOn(api, "liveTranscripts").mockResolvedValue({ items: [], nextCursor: null });
    vi.spyOn(api, "saveLiveTranscript").mockResolvedValue(undefined);
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
    useAppStore.setState({
      authState: "authenticated", csrfToken: "csrf-memory", demoMode: false,
      selectedProfileId: "profile-newton", selectedSessionId: "session-papers",
      selectedGatewayId: "gateway-home", selectedWorkspaceId: "workspace-papers",
      gateways, profiles: profiles.map((profile) => ({ ...profile, mutable: true })), sessions,
      workspaces, automations, messages: initialMessages, streamingBySession: {},
      approvalsBySession: {}, clarificationsBySession: {}, features,
    });
  });

  afterEach(async () => {
    cleanup();
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    await db.drafts.clear();
  });

  it("offers a separate Live explanation of a saved response without starting speech playback or submitting work", async () => {
    const create = vi.spyOn(api, "createLiveSession").mockResolvedValue({ session: { id: "explanation" }, transport: { type: "webrtc", sdp: "answer" } });
    const speech = vi.spyOn(api, "streamSpeech");
    const user = userEvent.setup();
    render(<ChatView />);
    const button = screen.getByRole("button", { name: "Explicar con GPT Live" });
    expect(button).toBeEnabled();
    expect(screen.getByRole("button", { name: "Escuchar esta respuesta" })).toBeEnabled();
    await user.click(button);
    await waitFor(() => expect(transport.Client.instances).toHaveLength(1));
    const client = transport.Client.instances[0];
    await act(() => client.options.negotiate("offer", new AbortController().signal));
    expect(create).toHaveBeenCalledWith(expect.objectContaining({
      sessionId: "session-papers", profileId: "profile-newton", purpose: "explain", focusMessageId: expect.stringMatching(/^sha256:[a-f0-9]{64}$/),
    }), "csrf-memory", expect.any(AbortSignal));
    expect(speech).not.toHaveBeenCalled();
    expect(button).toHaveClass("is-selected");
    expect(screen.getByRole("button", { name: "Terminar conversación de voz" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Terminar conversación de voz" }));
    expect(screen.getByRole("button", { name: "Escuchar esta respuesta" })).toBeEnabled();
  });

  it("hides Live explanation when unconfigured and blocks it while dictation is capturing", async () => {
    const user = userEvent.setup();
    render(<ChatView />);
    await user.click(screen.getByRole("button", { name: "Dictar por voz" }));
    expect(screen.getByRole("button", { name: "Explicar con GPT Live" })).toBeDisabled();
    expect(transport.Client.instances).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Detener dictado" }));
    act(() => useAppStore.setState({ features: { ...features, live: { ...features.live, available: false } } }));
    expect(screen.queryByRole("button", { name: "Explicar con GPT Live" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Escuchar esta respuesta" })).toBeVisible();
  });

  it("shows only dialogue from an internal voice prompt, including after durable history replaces it", async () => {
    const content = `${liveDelegationPrefix}Keep your own identity, personality, configured instructions, memory, tools and permissions.${liveConversationSeparator}User: Hola, ¿quién eres?\nVoice assistant: Soy Newton, tu agente.\nUser: ¿Qué puedes hacer por mí?\nMe interesa automatizar tareas.`;
    useAppStore.setState({ messages: [{ id: "optimistic-voice", sessionId: "session-papers", role: "user", content, createdAt: "12:20", delivery: "ambiguous" }] });
    render(<ChatView />);
    expect(screen.queryByText(/This is a live voice request|Keep your own identity|Live conversation:/)).not.toBeInTheDocument();
    expect(screen.getByText("Hola, ¿quién eres?")).toBeVisible();
    expect(screen.getByText("Soy Newton, tu agente.")).toBeVisible();
    expect(screen.getByText(/¿Qué puedes hacer por mí\?/)).toHaveTextContent("Me interesa automatizar tareas.");
    expect(screen.getByLabelText(i18n.t("chat.delivery.unconfirmed"))).toBeVisible();
    expect(useAppStore.getState().messages[0].content).toBe(content);
    act(() => useAppStore.getState().updateMessage("optimistic-voice", { delivery: "sent" }));

    vi.spyOn(api, "sessionHistory").mockResolvedValue({ items: [
      { id: "durable-voice", role: "user", content, timestamp: Date.now() / 1000 },
      { id: "durable-answer", role: "assistant", content: "Puedo ayudarte a automatizar ese proceso.", timestamp: Date.now() / 1000 + 1 },
    ], sessionStatus: "ready", activeOperation: null });
    await act(() => rehydrateSession("session-papers"));
    expect(screen.getByText("Hola, ¿quién eres?")).toBeVisible();
    expect(screen.getByText("Puedo ayudarte a automatizar ese proceso.")).toBeVisible();
    expect(screen.queryByText(/This is a live voice request|Keep your own identity|Live conversation:/)).not.toBeInTheDocument();
    expect(useAppStore.getState().messages.find((message) => message.id === "durable-voice")?.content).toBe(content);
  });

  it("releases backgrounded voice without cancelling the agent and shows its recovered answer after the captions", async () => {
    const user = userEvent.setup();
    const interrupt = vi.spyOn(api, "interrupt");
    render(<ChatView />);
    await user.click(screen.getByRole("button", { name: "Conversar con GPT-Live-1" }));
    const client = transport.Client.instances[0];
    act(() => {
      client.options.onTranscript!([{ role: "assistant", text: "Claro, lo reviso.", start: 0, end: 100, order: 0 }]);
      useAppStore.getState().appendMessage({ id: "pending-voice-result", sessionId: "session-papers", role: "assistant", content: "", streaming: true, createdAt: "", timestamp: new Date().toISOString() });
      useAppStore.getState().setStreamingMessageId("session-papers", "pending-voice-result");
    });
    vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    fireEvent(document, new Event("visibilitychange"));
    expect(client.stop).toHaveBeenCalledOnce();
    expect(interrupt).not.toHaveBeenCalled();
    expect(useAppStore.getState().streamingBySession["session-papers"]).toBe("pending-voice-result");
    vi.spyOn(api, "sessionHistory").mockResolvedValue({ items: [{ id: "durable-result", role: "assistant", content: "Necesito tu confirmación para continuar.", timestamp: Date.now() / 1000 + 1 }], sessionStatus: "ready", activeOperation: null });
    await act(() => rehydrateSession("session-papers"));
    const caption = screen.getByText("Claro, lo reviso.");
    const answer = screen.getByText("Necesito tu confirmación para continuar.");
    expect(caption.compareDocumentPosition(answer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(useAppStore.getState().streamingBySession["session-papers"]).toBeUndefined();
    expect(screen.getByRole("button", { name: "Enviar mensaje" })).toBeInTheDocument();
  });

  it("shows both speakers immediately, keeps captions after stop, and restores them from history", async () => {
    const user = userEvent.setup();
    const view = render(<ChatView />);
    await user.click(screen.getByRole("button", { name: "Conversar con GPT-Live-1" }));
    const client = transport.Client.instances[0];
    const fragments = [
      { role: "user" as const, text: "¿Qué estás entendiendo?", start: 100, end: 600, order: 0 },
      { role: "assistant" as const, text: "Que quieres ver nuestra conversación.", start: 400, end: 1000, order: 1 },
    ];
    act(() => client.options.onTranscript?.(fragments));
    expect(screen.getByText(fragments[0].text)).toBeVisible();
    expect(screen.getByText(fragments[1].text)).toBeVisible();
    expect(screen.getByRole("region", { name: "Transcripción de voz" })).toHaveTextContent("Newton");
    expect(screen.getByRole("textbox", { name: "Mensaje a Newton…" })).toHaveValue("");
    expect(api.saveLiveTranscript).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Terminar conversación de voz" }));
    await waitFor(() => expect(api.saveLiveTranscript).toHaveBeenCalledTimes(1));
    expect(screen.getByText(fragments[0].text)).toBeVisible();
    const savedId = vi.mocked(api.saveLiveTranscript).mock.calls[0][1];
    view.unmount();
    vi.mocked(api.liveTranscripts).mockResolvedValue({ items: [{ id: savedId, createdAt: new Date().toISOString(), fragments }], nextCursor: null });
    render(<ChatView />);
    expect(await screen.findByText(fragments[0].text)).toBeVisible();
    expect(transport.Client.instances).toHaveLength(1);
    act(() => useAppStore.setState({ selectedSessionId: "session-other" }));
    expect(screen.queryByText(fragments[0].text)).not.toBeInTheDocument();
  });

  it("shows both configured voice icons without an idle panel or automatic start", async () => {
    const user = userEvent.setup();
    const { container } = render(<ChatView />);
    const elevenLabsButton = screen.getByRole("button", { name: "Dictar por voz" });
    const composerActions = elevenLabsButton.closest(".composer__actions");
    expect(composerActions).not.toBeNull();
    expect(elevenLabsButton).toBeVisible();
    expect(elevenLabsButton).toHaveAttribute("data-voice-provider", "elevenlabs");
    expect(elevenLabsButton.textContent).toBe("");
    expect(screen.getByRole("checkbox", { name: "Escuchar respuestas en vivo" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "GPT-Live-1" })).not.toBeInTheDocument();
    expect(transport.Client.instances).toHaveLength(0);
    expect(transport.scribeStart).not.toHaveBeenCalled();

    act(chooseLive);
    expect(screen.getByRole("button", { name: "Dictar por voz" })).toBeVisible();
    expect(screen.getByRole("checkbox", { name: "Escuchar respuestas en vivo" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "GPT-Live-1" })).not.toBeInTheDocument();
    expect(screen.queryByText("GPT-Live-1", { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByText(/El audio y el contexto se envían a OpenAI/)).not.toBeInTheDocument();
    expect(container.querySelector(".live-voice")).toBeNull();
    expect(transport.Client.instances).toHaveLength(0);
    const liveButton = screen.getByRole("button", { name: "Conversar con GPT-Live-1" });
    expect(liveButton.closest(".composer__actions")).toBe(composerActions);
    expect(liveButton).toHaveAttribute("data-voice-provider", "openai_live");
    expect(liveButton.textContent).toBe("");

    await user.click(liveButton);
    expect(transport.Client.instances).toHaveLength(1);
    expect(transport.Client.instances[0].start).toHaveBeenCalledTimes(1);
    const stop = screen.getByRole("button", { name: "Terminar conversación de voz" });
    expect(stop).toBeEnabled();
    expect(stop).toHaveClass("is-selected");
    expect(stop.closest(".composer__actions")).toBe(composerActions);
    expect(screen.getByText("Escuchando · ya puedes hablar").closest(".dictation-state")).not.toBeNull();
    expect(transport.scribeStart).not.toHaveBeenCalled();
  });

  it("separates connecting, listening and microphone pause without opening another call", async () => {
    chooseLive();
    const user = userEvent.setup();
    render(<ChatView />);
    await user.click(screen.getByRole("button", { name: "Conversar con GPT-Live-1" }));
    const client = transport.Client.instances[0];
    act(() => client.options.onPhase("connecting"));
    expect(screen.getByText("Conectando… espera para hablar")).toBeVisible();
    expect(screen.queryByText("Escuchando · ya puedes hablar")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pausar micrófono" })).not.toBeInTheDocument();
    act(() => client.options.onPhase("listening"));
    await user.click(screen.getByRole("button", { name: "Pausar micrófono" }));
    expect(client.setPaused).toHaveBeenLastCalledWith(true);
    expect(screen.getByText("Micrófono en pausa")).toBeVisible();
    expect(screen.queryByText("Escuchando · ya puedes hablar")).not.toBeInTheDocument();
    expect(usePwaUpdateStore.getState().blockers.dictation).toBe(true);
    expect(screen.getByRole("button", { name: "Terminar conversación de voz" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Enviar mensaje" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Reanudar micrófono" }));
    expect(client.setPaused).toHaveBeenLastCalledWith(false);
    expect(screen.getByText("Escuchando · ya puedes hablar")).toBeVisible();
    expect(transport.Client.instances).toHaveLength(1);
    expect(client.start).toHaveBeenCalledOnce();
    expect(client.stop).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Pausar micrófono" }));
    await user.click(screen.getByRole("button", { name: "Terminar conversación de voz" }));
    expect(client.stop).toHaveBeenCalledOnce();
    expect(usePwaUpdateStore.getState().blockers.dictation).toBe(false);
  });

  it("keeps the voice stop control accessible while the agent is streaming", async () => {
    chooseLive();
    const user = userEvent.setup();
    render(<ChatView />);
    await user.click(screen.getByRole("button", { name: "Conversar con GPT-Live-1" }));
    const client = transport.Client.instances[0];
    act(() => useAppStore.setState({ streamingBySession: { "session-papers": "agent-response" } }));
    const stop = screen.getByRole("button", { name: "Terminar conversación de voz" });
    expect(stop).toBeEnabled();
    expect(stop.closest(".composer__actions")).not.toBeNull();
    expect(client.dispose).not.toHaveBeenCalled();
    expect(screen.getByRole("textbox", { name: "Mensaje a Newton…" })).toHaveAttribute("readonly");
    expect(usePwaUpdateStore.getState().blockers.dictation).toBe(true);
    await user.click(stop);
    expect(client.stop).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Conversar con GPT-Live-1" })).toBeDisabled();
    expect(usePwaUpdateStore.getState().blockers.dictation).toBe(false);
  });

  it.each([[false, false], [true, false], [false, true], [true, true]])("shows only configured actions (ElevenLabs=%s, Live=%s)", (scribe, live) => {
    useAppStore.setState({ features: { ...features, dictation: { ...features.dictation, available: scribe }, live: { ...features.live, available: live }, voice: { provider: "elevenlabs" } } });
    render(<ChatView />);
    expect(screen.queryByRole("button", { name: "Dictar por voz" }) !== null).toBe(scribe);
    expect(screen.queryByRole("button", { name: "Conversar con GPT-Live-1" }) !== null).toBe(live);
    expect(screen.getByRole("textbox", { name: "Mensaje a Newton…" })).not.toHaveAttribute("readonly");
    expect(transport.scribeStart).not.toHaveBeenCalled();
    expect(transport.Client.instances).toHaveLength(0);
  });

  it("keeps both actions visible but prevents overlapping capture, including while paused", async () => {
    const user = userEvent.setup();
    render(<ChatView />);
    await user.click(screen.getByRole("button", { name: "Dictar por voz" }));
    expect(screen.getByRole("button", { name: "Conversar con GPT-Live-1" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Pausar dictado" }));
    expect(screen.getByText("Dictado en pausa")).toBeVisible();
    expect(screen.getByRole("button", { name: "Conversar con GPT-Live-1" })).toBeDisabled();
    expect(usePwaUpdateStore.getState().blockers.dictation).toBe(true);
    await user.click(screen.getByRole("button", { name: "Reanudar dictado" }));
    act(() => transport.scribeCommit("Instrucción confirmada"));
    await user.click(screen.getByRole("button", { name: "Detener dictado" }));
    const input = screen.getByRole("textbox", { name: "Mensaje a Newton…" });
    expect(input).toHaveValue("Instrucción confirmada");
    expect(screen.getByRole("button", { name: "Conversar con GPT-Live-1" })).toBeDisabled();
    await user.clear(input);
    await user.click(screen.getByRole("button", { name: "Conversar con GPT-Live-1" }));
    expect(screen.getByRole("button", { name: "Dictar por voz" })).toBeDisabled();
    expect(screen.queryByRole("checkbox", { name: "Escuchar respuestas en vivo" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Pausar micrófono" }));
    expect(screen.getByRole("button", { name: "Dictar por voz" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Terminar conversación de voz" }));
    expect(screen.getByRole("button", { name: "Dictar por voz" })).toBeEnabled();
    expect(transport.scribeStart).toHaveBeenCalledOnce();
    expect(transport.Client.instances).toHaveLength(1);
  });

  it("cancels pending ElevenLabs playback when a Live conversation takes the microphone", async () => {
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
    vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
    const stream = vi.spyOn(api, "streamSpeech").mockImplementation(() => new Promise(() => {}));
    const user = userEvent.setup();
    render(<ChatView />);
    await user.click(screen.getAllByRole("button", { name: "Escuchar esta respuesta" })[0]);
    const signal = stream.mock.calls[0][3]!;
    expect(signal.aborted).toBe(false);
    await user.click(screen.getByRole("button", { name: "Conversar con GPT-Live-1" }));
    expect(signal.aborted).toBe(true);
    expect(screen.queryByRole("button", { name: "Escuchar esta respuesta" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Pausar micrófono" }));
    expect(screen.queryByRole("button", { name: "Escuchar esta respuesta" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Terminar conversación de voz" }));
    expect(screen.getAllByRole("button", { name: "Escuchar esta respuesta" }).length).toBeGreaterThan(0);
    expect(stream).toHaveBeenCalledOnce();
  });

  it("preserves a typed draft when switching providers and requires handling it before live start", async () => {
    const user = userEvent.setup();
    render(<ChatView />);
    const composer = screen.getByRole("textbox", { name: "Mensaje a Newton…" });
    await user.type(composer, "Borrador que quiero revisar");
    await waitFor(async () => expect(await loadDraft("session-papers")).toBe("Borrador que quiero revisar"));
    act(chooseLive);
    expect(composer).toHaveValue("Borrador que quiero revisar");
    expect(screen.getByRole("button", { name: "Conversar con GPT-Live-1" })).toBeDisabled();
    expect(transport.Client.instances).toHaveLength(0);
    act(() => useAppStore.setState({ features: { ...features, voice: { provider: "elevenlabs" } } }));
    expect(composer).toHaveValue("Borrador que quiero revisar");
    expect(await loadDraft("session-papers")).toBe("Borrador que quiero revisar");
    expect(screen.getByRole("button", { name: "Enviar mensaje" })).toBeEnabled();
  });

  it.each(["session", "profile", "logout", "key", "unmount"] as const)("releases a paused microphone transport on %s without restarting it", async (transition) => {
    chooseLive();
    const user = userEvent.setup();
    const view = render(<ChatView />);
    await user.click(screen.getByRole("button", { name: "Conversar con GPT-Live-1" }));
    const client = transport.Client.instances[0];
    await user.click(screen.getByRole("button", { name: "Pausar micrófono" }));
    expect(screen.getByText("Micrófono en pausa")).toBeVisible();
    expect(client.dispose).not.toHaveBeenCalled();
    act(() => {
      if (transition === "session") useAppStore.setState({ selectedSessionId: "session-architecture" });
      if (transition === "profile") useAppStore.setState({ selectedProfileId: "profile-jarvis", selectedSessionId: "session-evals" });
      if (transition === "logout") useAppStore.setState({ authState: "unauthenticated" });
      if (transition === "key") useAppStore.setState({ features: { ...features, live: { ...features.live, available: false } } });
      if (transition === "unmount") view.unmount();
    });
    await waitFor(() => expect(client.stop).toHaveBeenCalledTimes(1));
    expect(usePwaUpdateStore.getState().blockers.dictation).toBe(false);
    expect(transport.Client.instances).toHaveLength(1);
    expect(client.start).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Terminar conversación de voz" })).not.toBeInTheDocument();
  });

  it("keeps captions out of the compact composer and lets the user unlock audio with an icon", async () => {
    chooseLive();
    const user = userEvent.setup();
    render(<ChatView />);
    await user.click(screen.getByRole("button", { name: "Conversar con GPT-Live-1" }));
    const client = transport.Client.instances[0];
    // A conversational client provides captions; silent previews omit them.
    expect(client.options.onTranscript).toBeTypeOf("function");
    act(() => {
      client.options.onTranscript!([
        { role: "user", text: "Revisa el resultado", start: 1, end: 2, order: 1 },
        { role: "assistant", text: "Estoy revisando", start: 2, end: 3, order: 2 },
      ]);
      client.options.onPlaybackBlocked(true);
    });
    expect(screen.getByText("Revisa el resultado").closest(".message-scroll")).not.toBeNull();
    expect(screen.getByText("Estoy revisando").closest(".composer")).toBeNull();
    expect(screen.getByRole("textbox", { name: "Mensaje a Newton…" })).toHaveValue("");
    expect(await loadDraft("session-papers")).toBe("");
    const playback = screen.getByRole("button", { name: "Activar audio" });
    expect(playback.textContent).toBe("");
    expect(playback.closest(".composer__actions")).not.toBeNull();
    await user.click(playback);
    expect(client.play).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Activar audio" })).not.toBeInTheDocument();
  });

  it.each(["offline", "unconfigured", "unsupported", "streaming"] as const)("does not start live voice when %s", async (reason) => {
    chooseLive();
    if (reason === "offline") useAppStore.setState({ authState: "offline" });
    if (reason === "unconfigured") useAppStore.setState({ features: { ...features, live: { ...features.live, available: false }, voice: { provider: "openai_live" } } });
    if (reason === "unsupported") transport.liveSupported.mockReturnValue(false);
    if (reason === "streaming") useAppStore.setState({ streamingBySession: { "session-papers": "agent-response" } });
    const user = userEvent.setup();
    render(<ChatView />);
    if (reason === "offline" || reason === "unconfigured") {
      expect(screen.queryByRole("button", { name: "Conversar con GPT-Live-1" })).not.toBeInTheDocument();
      if (reason === "offline") expect(screen.getByText("Borrador offline")).toBeVisible();
    } else {
      const start = screen.getByRole("button", { name: "Conversar con GPT-Live-1" });
      expect(start).toBeDisabled();
      await user.click(start);
    }
    expect(transport.Client.instances).toHaveLength(0);
    expect(transport.scribeStart).not.toHaveBeenCalled();
    if (reason !== "offline") expect(screen.getByRole("button", { name: "Dictar por voz" })).toBeVisible();
  });

  it("blocks voice start while attachments are staged and enables it again after removal", async () => {
    chooseLive();
    const user = userEvent.setup();
    render(<ChatView />);
    await user.upload(screen.getByLabelText("Archivo"), new File(["Draft"], "notas.txt", { type: "text/plain" }));
    const start = screen.getByRole("button", { name: "Conversar con GPT-Live-1" });
    expect(start).toBeDisabled();
    await user.click(start);
    expect(transport.Client.instances).toHaveLength(0);
    await user.click(screen.getByRole("button", { name: "Quitar notas.txt" }));
    expect(start).toBeEnabled();
  });

  it("keeps Enter from sending a typed draft after the agent starts streaming", async () => {
    chooseLive();
    const submit = vi.spyOn(api, "submitPrompt");
    const user = userEvent.setup();
    render(<ChatView />);
    const composer = screen.getByRole("textbox", { name: "Mensaje a Newton…" });
    await user.type(composer, "Borrador pendiente");
    act(() => useAppStore.setState({ streamingBySession: { "session-papers": "agent-response" } }));
    fireEvent.keyDown(composer, { key: "Enter", code: "Enter" });
    expect(submit).not.toHaveBeenCalled();
    expect(composer).toHaveValue("Borrador pendiente");
    expect(transport.Client.instances).toHaveLength(0);
  });

  it("starts ElevenLabs directly from its icon without opening a disclosure modal", async () => {
    const user = userEvent.setup();
    render(<ChatView />);
    expect(transport.scribeStart).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Dictar por voz" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(transport.scribeStart).toHaveBeenCalledTimes(1);
    expect(transport.Client.instances).toHaveLength(0);
  });
});
