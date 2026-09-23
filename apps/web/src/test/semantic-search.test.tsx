import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, type SemanticSearchStatus } from "../lib/api";
import { SearchScreen } from "../screens/SearchScreen";
import { useAppStore } from "../store/appStore";
import i18n from "../i18n";

vi.mock("@tanstack/react-router", () => ({ useNavigate: () => vi.fn() }));
vi.mock("@tanstack/react-virtual", () => ({ useVirtualizer: ({ count }: { count: number }) => ({
  getTotalSize: () => count * 100,
  getVirtualItems: () => Array.from({ length: count }, (_, index) => ({ index, start: index * 100, size: 100 })),
}) }));

const ready: SemanticSearchStatus = { enabled: true, configured: true, indexed: 3, total: 3, pending: 0, failed: 0, errorCode: null, state: "ready", revision: "one" };
const result = (title: string, source: "text" | "live" = "text") => ({ id: title, kind: "session" as const, targetId: title, title, excerpt: "fragment", meta: "Agent", source });
async function advance(ms = 600) { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); }

describe("semantic conversation search", () => {
  beforeEach(async () => {
    vi.useFakeTimers();
    await i18n.changeLanguage("es");
    useAppStore.setState({ authState: "authenticated", demoMode: false, userId: "owner", authGeneration: 1,
      csrfToken: "csrf-test", sessions: [], profiles: [], messages: [], workspaces: [], automations: [] });
    vi.spyOn(api, "semanticSearchStatus").mockResolvedValue(ready);
    vi.spyOn(api, "search").mockResolvedValue({ items: [result("Literal")], partial: false });
    vi.spyOn(api, "semanticSearch").mockResolvedValue({ items: [result("Conceptual", "live")], partial: false });
    vi.spyOn(api, "setSemanticSearch").mockResolvedValue(ready);
  });
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

  it("runs both searches automatically and switches tabs without another API call", async () => {
    render(<SearchScreen />);
    await advance(0);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "vacaciones" } });
    await advance(250);
    expect(api.search).toHaveBeenCalledOnce();
    expect(api.semanticSearch).not.toHaveBeenCalled();
    await advance(350);
    expect(api.semanticSearch).toHaveBeenCalledOnce();
    expect(screen.getByText("Literal")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "Semántica" }));
    expect(screen.getByText("Conceptual")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
    expect(screen.queryByText("Literal")).not.toBeInTheDocument();
    await advance(600);
    expect(api.semanticSearch).toHaveBeenCalledOnce();
  });

  it("waits for activation and protects it with the existing CSRF integration", async () => {
    vi.mocked(api.semanticSearchStatus).mockResolvedValue({ ...ready, enabled: false, state: "disabled" });
    render(<SearchScreen />); await advance(0);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "idea" } });
    await advance();
    expect(api.semanticSearch).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("tab", { name: "Semántica" }));
    expect(screen.getByText(/tu clave y cuota existentes/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Activar búsqueda semántica" }));
    await advance(0);
    await advance();
    expect(api.setSemanticSearch).toHaveBeenCalledWith(true, "csrf-test");
    expect(api.semanticSearch).toHaveBeenCalledOnce();
  });

  it("discards out-of-order results even when the transport ignores abort", async () => {
    let finishOld!: (value: { items: ReturnType<typeof result>[]; partial: boolean }) => void;
    vi.mocked(api.semanticSearch).mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve; }));
    render(<SearchScreen />); await advance(0);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "primera" } }); await advance();
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "segunda" } }); await advance();
    await act(async () => { finishOld({ items: [result("Obsoleto")], partial: false }); });
    fireEvent.click(screen.getByRole("tab", { name: "Semántica" }));
    expect(screen.queryByText("Obsoleto")).not.toBeInTheDocument();
    expect(screen.getByText("Conceptual")).toBeInTheDocument();
  });

  it("keeps lexical results usable when semantic search fails and supports keyboard tabs", async () => {
    vi.mocked(api.semanticSearch).mockRejectedValue(new ApiError(429, "provider", "SEMANTIC_QUOTA"));
    render(<SearchScreen />); await advance(0);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "idea" } }); await advance();
    expect(screen.getByText("Literal")).toBeInTheDocument();
    fireEvent.keyDown(screen.getByRole("tab", { name: "Léxica" }), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Semántica" })).toHaveFocus();
    expect(screen.getAllByText(/Se agotó tu cuota/).length).toBeGreaterThan(0);
  });

  it("clears results on account changes and rejects a late response from the old owner", async () => {
    let finish!: (value: { items: ReturnType<typeof result>[]; partial: boolean }) => void;
    vi.mocked(api.semanticSearch).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve; }));
    render(<SearchScreen />); await advance(0);
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "idea" } }); await advance();
    vi.mocked(api.semanticSearchStatus).mockResolvedValue({ ...ready, enabled: false, state: "disabled" });
    act(() => useAppStore.setState({ userId: "different-owner", authGeneration: 2 }));
    await advance(0);
    await act(async () => { finish({ items: [result("Private old owner")], partial: false }); });
    fireEvent.click(screen.getByRole("tab", { name: "Semántica" }));
    expect(screen.queryByText("Private old owner")).not.toBeInTheDocument();
  });
});
