import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../i18n";
import { ChatView } from "../components/ChatView";
import { gateways, profiles, sessions, workspaces } from "../data";
import { useSessionHistory } from "../hooks";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";

function ReopenedChat() {
  useSessionHistory();
  return <ChatView />;
}

describe("rehydrated Hermes tool history", () => {
  beforeEach(() => {
    useAppStore.setState({
      authState: "authenticated",
      demoMode: false,
      bootstrapLoaded: true,
      selectedGatewayId: "gateway-home",
      selectedProfileId: "profile-newton",
      selectedWorkspaceId: "workspace-papers",
      selectedSessionId: "session-papers",
      gateways,
      profiles,
      sessions,
      workspaces,
      messages: [],
      streamingBySession: {},
      pendingOperations: {},
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("preserves role=tool history for the activity panel without inline chat cards", async () => {
    vi.spyOn(api, "sessionHistory").mockResolvedValue({
      items: [
        { id: "user-1", role: "user", content: "Busca evidencia", timestamp: 1_777_000_000 },
        {
          id: "assistant-call",
          role: "assistant",
          content: "",
          tool_calls: [{ id: "call-search", function: { name: "web_search", arguments: "{}" } }],
        },
        {
          id: "tool-row-1",
          role: "tool",
          tool_call_id: "call-search",
          tool_name: "web_search",
          content: "18 fuentes revisadas",
          timestamp: 1_777_000_002,
        },
        {
          role: "tool",
          name: "paper_reader",
          context: "6 papers comparados",
        },
        { id: "assistant-1", role: "assistant", text: "La evidencia quedó contrastada.", timestamp: 1_777_000_003 },
      ],
    });
    render(<ReopenedChat />);

    const answer = await screen.findByText("La evidencia quedó contrastada.");
    expect(answer).toBeVisible();
    await waitFor(() => {
      const assistant = useAppStore.getState().messages.find((message) => message.id === "assistant-1");
      expect(assistant?.tools).toEqual(expect.arrayContaining([
        expect.objectContaining({ id: "call-search", name: "web_search", status: "completed" }),
        expect.objectContaining({ name: "paper_reader", status: "completed" }),
      ]));
    });

    expect(screen.queryByRole("button", { name: /Herramientas · 2/i })).not.toBeInTheDocument();
  });

  it("renders an authenticated voice-note player instead of a private MEDIA path", async () => {
    vi.spyOn(api, "sessionHistory").mockResolvedValue({
      items: [
        {
          id: "assistant-voice",
          role: "assistant",
          content: "Nota de voz — resumen ejecutivo",
          controlMedia: [{
            id: "0123456789abcdef0123456789abcdef",
            kind: "audio",
            mediaType: "audio/mpeg",
          }],
        },
      ],
    });
    const { container } = render(<ReopenedChat />);

    expect(await screen.findByLabelText("Nota de voz")).toBeVisible();
    expect(screen.getByText("Nota de voz — resumen ejecutivo")).toBeVisible();
    expect(screen.queryByText(/MEDIA:\//)).not.toBeInTheDocument();
    const audio = container.querySelector("audio");
    const source = container.querySelector("audio source");
    expect(audio).toHaveAttribute("preload", "metadata");
    expect(audio).toHaveAccessibleName("Reproducir nota de voz");
    expect(source).toHaveAttribute(
      "src",
      "/api/v1/sessions/session-papers/media/0123456789abcdef0123456789abcdef",
    );
    expect(source).toHaveAttribute("type", "audio/mpeg");
    expect(useAppStore.getState().messages[0]?.media).toEqual([
      {
        id: "0123456789abcdef0123456789abcdef",
        kind: "audio",
        mediaType: "audio/mpeg",
      },
    ]);
  });
});
