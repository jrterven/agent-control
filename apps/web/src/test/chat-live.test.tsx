import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ChatView } from "../components/ChatView";
import { automations, gateways, initialMessages, profiles, sessions, workspaces } from "../data";
import i18n from "../i18n";
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
  return { Client, scribeStart: vi.fn(), scribeStop: vi.fn() };
});

vi.mock("../lib/openaiLiveClient", async (importOriginal) => ({
  ...await importOriginal<typeof import("../lib/openaiLiveClient")>(),
  OpenAILiveClient: transport.Client,
  liveSupported: () => true,
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

  it("uses ElevenLabs for legacy settings and reveals OpenAI disclosure before explicit live start", async () => {
    const user = userEvent.setup();
    render(<ChatView />);
    expect(screen.getByRole("button", { name: "Dictar por voz" })).toBeVisible();
    expect(screen.getByRole("checkbox", { name: "Escuchar respuestas en vivo" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "GPT-Live-1" })).not.toBeInTheDocument();
    expect(transport.Client.instances).toHaveLength(0);
    expect(transport.scribeStart).not.toHaveBeenCalled();

    act(chooseLive);
    expect(screen.queryByRole("button", { name: "Dictar por voz" })).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Escuchar respuestas en vivo" })).not.toBeInTheDocument();
    expect(screen.getByText(/El audio y el contexto se envían a OpenAI/)).toBeVisible();
    expect(screen.getByText(/puede enviar solicitudes automáticamente al agente seleccionado/)).toBeVisible();
    expect(transport.Client.instances).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Conversar con GPT-Live-1" }));
    expect(transport.Client.instances).toHaveLength(1);
    expect(transport.Client.instances[0].start).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Terminar conversación de voz" })).toBeEnabled();
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

  it("shows live captions outside the draft and lets the user unlock blocked audio", async () => {
    chooseLive();
    const user = userEvent.setup();
    render(<ChatView />);
    await user.click(screen.getByRole("button", { name: "Conversar con GPT-Live-1" }));
    const client = transport.Client.instances[0];
    act(() => {
      client.options.onTranscript([
        { role: "user", text: "Revisa el resultado", start: 1, end: 2, order: 1 },
        { role: "assistant", text: "Estoy revisando", start: 2, end: 3, order: 2 },
      ]);
      client.options.onPlaybackBlocked(true);
    });
    expect(screen.getByText("Revisa el resultado")).toBeVisible();
    expect(screen.getByText("Estoy revisando")).toBeVisible();
    expect(screen.getByRole("textbox", { name: "Mensaje a Newton…" })).toHaveValue("");
    expect(await loadDraft("session-papers")).toBe("");
    await user.click(screen.getByRole("button", { name: "Activar audio" }));
    expect(client.play).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: "Activar audio" })).not.toBeInTheDocument();
  });
});
