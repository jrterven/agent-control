import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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
      sessionStatus: "ready",
      activeOperation: null,
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
      sessionStatus: "ready",
      activeOperation: null,
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

  it("keeps partial tool evidence collapsed when an interrupted turn has no assistant text", async () => {
    vi.spyOn(api, "sessionHistory").mockResolvedValue({
      sessionStatus: "interrupted",
      activeOperation: null,
      pendingInteractions: [],
      items: [
        { id: "user-partial", role: "user", content: "Agenda la reunión" },
        {
          id: "assistant-partial",
          role: "assistant",
          content: "",
          finish_reason: "tool_calls",
          tool_calls: [{ id: "calendar-call", name: "calendar.create" }],
        },
        {
          id: "calendar-result",
          role: "tool",
          tool_call_id: "calendar-call",
          tool_name: "calendar.create",
          content: "Se alcanzó a crear un evento",
        },
      ],
    });

    const user = userEvent.setup();
    render(<ReopenedChat />);

    const toggle = await screen.findByRole("button", { name: "Mostrar historial de herramientas de Newton" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("Se alcanzó a crear un evento")).not.toBeInTheDocument();
    expect(screen.getByText(/se interrumpió antes/i)).toBeVisible();

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle).toHaveAccessibleName("Ocultar historial de herramientas de Newton");
    const history = screen.getByRole("region", { name: "Historial de herramientas de Newton" });
    expect(history).toHaveAttribute("data-max-items", "10");
    expect(history).toHaveClass("message-evidence__viewport");
    expect(screen.getByText("Se alcanzó a crear un evento")).toBeVisible();
  });
});
