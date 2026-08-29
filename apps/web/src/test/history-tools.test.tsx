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

  it("folds role=tool rows from REST and RPC history into the assistant tool cards", async () => {
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
    const user = userEvent.setup();
    render(<ReopenedChat />);

    const toolsButton = await screen.findByRole("button", { name: /Herramientas · 2/i });
    const answer = await screen.findByText("La evidencia quedó contrastada.");
    expect(answer).toBeVisible();
    await waitFor(() => {
      const assistant = useAppStore.getState().messages.find((message) => message.id === "assistant-1");
      expect(assistant?.tools).toEqual(expect.arrayContaining([
        expect.objectContaining({ id: "call-search", name: "web_search", status: "completed" }),
        expect.objectContaining({ name: "paper_reader", status: "completed" }),
      ]));
    });

    await user.click(toolsButton);
    expect(screen.getByText("web search")).toBeVisible();
    expect(screen.getByText("18 fuentes revisadas")).toBeVisible();
    expect(screen.getByText("paper reader")).toBeVisible();
    expect(screen.getByText("6 papers comparados")).toBeVisible();
  });
});
