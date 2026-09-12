import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatView } from "../components/ChatView";
import { automations, gateways, initialMessages, profiles, sessions, workspaces } from "../data";
import i18n from "../i18n";
import { api } from "../lib/api";
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
    dispose = vi.fn();
    play = vi.fn(async () => { this.options.onPlaybackBlocked(false); });
  }
  return { Client, scribeStart: vi.fn(), scribeStop: vi.fn(), liveSupported: vi.fn(() => true) };
});

vi.mock("../lib/openaiLiveClient", async (importOriginal) => ({
  ...await importOriginal<typeof import("../lib/openaiLiveClient")>(),
  OpenAILiveClient: transport.Client,
  liveSupported: transport.liveSupported,
}));

vi.mock("../hooks/useScribeDictation", () => ({
  useScribeDictation: ({ enabled }: { enabled: boolean }) => ({
    available: enabled, supported: true, phase: "idle", active: false, issue: null, partial: "",
    start: transport.scribeStart, stop: transport.scribeStop,
  }),
}));

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
    await i18n.changeLanguage("es");
    await db.drafts.clear();
    transport.Client.instances = [];
    transport.scribeStart.mockClear();
    transport.scribeStop.mockClear();
    transport.liveSupported.mockReturnValue(true);
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
    await db.drafts.clear();
  });

  it("switches the composer voice icon by provider without an idle GPT-Live panel or automatic start", async () => {
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
    expect(screen.queryByRole("button", { name: "Dictar por voz" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Escuchar respuestas en vivo" })).not.toBeInTheDocument();
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
    expect(screen.getByText("Conversación en vivo").closest(".dictation-state")).not.toBeNull();
    expect(transport.scribeStart).not.toHaveBeenCalled();
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

  it.each(["session", "profile", "logout", "provider", "unmount"] as const)("releases an active microphone transport on %s without restarting it", async (transition) => {
    chooseLive();
    const user = userEvent.setup();
    const view = render(<ChatView />);
    await user.click(screen.getByRole("button", { name: "Conversar con GPT-Live-1" }));
    const client = transport.Client.instances[0];
    expect(client.dispose).not.toHaveBeenCalled();
    act(() => {
      if (transition === "session") useAppStore.setState({ selectedSessionId: "session-architecture" });
      if (transition === "profile") useAppStore.setState({ selectedProfileId: "profile-jarvis", selectedSessionId: "session-evals" });
      if (transition === "logout") useAppStore.setState({ authState: "unauthenticated" });
      if (transition === "provider") useAppStore.setState({ features: { ...features, voice: { provider: "elevenlabs" } } });
      if (transition === "unmount") view.unmount();
    });
    await waitFor(() => expect(client.dispose).toHaveBeenCalledTimes(1));
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
    expect(screen.queryByText("Revisa el resultado")).not.toBeInTheDocument();
    expect(screen.queryByText("Estoy revisando")).not.toBeInTheDocument();
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
    if (reason === "offline") {
      expect(screen.queryByRole("button", { name: "Conversar con GPT-Live-1" })).not.toBeInTheDocument();
      expect(screen.getByText("Borrador offline")).toBeVisible();
    } else {
      const start = screen.getByRole("button", { name: "Conversar con GPT-Live-1" });
      expect(start).toBeDisabled();
      await user.click(start);
    }
    expect(transport.Client.instances).toHaveLength(0);
    expect(transport.scribeStart).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Dictar por voz" })).not.toBeInTheDocument();
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

  it("routes the ElevenLabs icon through its existing consent flow", async () => {
    const user = userEvent.setup();
    render(<ChatView />);
    await user.click(screen.getByRole("button", { name: "Dictar por voz" }));
    expect(transport.scribeStart).not.toHaveBeenCalled();
    const dialog = screen.getByRole("dialog", { name: "Activar dictado por voz" });
    await user.click(within(dialog).getByRole("button", { name: "Aceptar y activar micrófono" }));
    expect(transport.scribeStart).toHaveBeenCalledTimes(1);
    expect(transport.Client.instances).toHaveLength(0);
  });
});
