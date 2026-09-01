import { act, render, screen, waitFor, within } from "@testing-library/react";
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

  afterEach(() => {
    vi.restoreAllMocks();
    Reflect.deleteProperty(window.navigator, "clipboard");
  });

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

  it("renders mail-tool references as cards and loads a plain-text preview on demand", async () => {
    const previewUrl = "/api/v1/sessions/session-papers/email-references/0123456789abcdef0123456789abcdef";
    const openUrl = `${previewUrl}/open`;
    vi.spyOn(api, "sessionHistory").mockResolvedValue({
      sessionStatus: "ready",
      activeOperation: null,
      items: [
        { id: "user-mail", role: "user", content: "¿Hay algo urgente?" },
        {
          id: "mail-tool",
          role: "tool",
          tool_name: "mail.read",
          content: "Correo encontrado",
          controlEmailReferences: [{
            schemaVersion: 1,
            id: "0123456789abcdef0123456789abcdef",
            provider: "gmail",
            senderName: "Google Ads",
            senderAddress: "noreply@ads.google.com",
            subject: "[Action required] Your account will be paused",
            receivedAt: "2026-08-31T07:29:00Z",
            snippet: "Complete advertiser verification before the deadline.",
            previewUrl,
            openUrl,
            openMode: "search",
          }],
        },
        { id: "assistant-mail", role: "assistant", content: "Sí, este correo requiere atención." },
      ],
    });
    const preview = vi.spyOn(api, "emailReferencePreview").mockResolvedValue({
      schemaVersion: 1,
      id: "0123456789abcdef0123456789abcdef",
      provider: "gmail",
      senderName: "Google Ads",
      senderAddress: "noreply@ads.google.com",
      subject: "[Action required] Your account will be paused",
      receivedAt: "2026-08-31T07:29:00Z",
      snippet: "Complete advertiser verification before the deadline.",
      previewUrl,
      openUrl,
      openMode: "search",
      bodyText: "Start advertiser verification now.\n\nYour account will be paused in 10 days.",
    });
    const user = userEvent.setup();
    render(<ReopenedChat />);

    const card = await screen.findByRole("button", { name: "Ver correo: [Action required] Your account will be paused" });
    expect(card).toBeVisible();
    expect(screen.getByText("Google Ads")).toBeVisible();
    expect(screen.getByText("Referencia de Newton · confirma en tu buzón")).toBeVisible();
    const searchLink = screen.getByRole("link", { name: "Buscar en Gmail: [Action required] Your account will be paused" });
    expect(searchLink).toHaveAttribute("href", openUrl);
    expect(searchLink).toHaveAttribute("target", "_blank");
    expect(searchLink).toHaveAttribute("rel", expect.stringContaining("noopener"));

    await user.click(card);
    expect(preview).toHaveBeenCalledWith("session-papers", "0123456789abcdef0123456789abcdef");
    expect(await screen.findByText(/Your account will be paused in 10 days/)).toBeVisible();
    const dialog = screen.getByRole("dialog", { name: "[Action required] Your account will be paused" });
    expect(dialog).toBeVisible();
    expect(dialog).toHaveTextContent("Vista en texto plano");
    expect(dialog).toHaveTextContent("Referencia de Newton · confirma en tu buzón");
    expect(dialog.querySelector("img")).toBeNull();
    expect(screen.getByRole("link", { name: "Buscar en Gmail" })).toHaveAttribute("href", openUrl);

    await user.click(dialog.querySelector(".email-preview-sheet__close")!);
    const targetUrl = "https://mail.google.com/mail/#search/rfc822msgid%3Atest%40example.com";
    const resolveTarget = vi.spyOn(api, "emailReferenceOpenTarget").mockResolvedValue({ targetUrl });
    vi.spyOn(window, "matchMedia").mockImplementation((query: string) => ({
      matches: query === "(display-mode: standalone)",
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    }));
    vi.spyOn(window.navigator, "userAgent", "get").mockReturnValue(
      "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/128.0 Mobile Safari/537.36",
    );
    const clipboardWrite = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: { writeText: clipboardWrite },
    });

    await user.click(searchLink);
    await waitFor(() => expect(resolveTarget).toHaveBeenCalledWith(
      "session-papers",
      "0123456789abcdef0123456789abcdef",
    ));
    const gmailWebLink = await screen.findByRole("link", { name: "Buscar correo en Gmail Web" });
    expect(gmailWebLink).toHaveAttribute("href", targetUrl);
    expect(gmailWebLink).toHaveAttribute("target", "_blank");
    expect(dialog.querySelector('a[href^="intent:"]')).toBeNull();

    const exactSearch = screen.getByRole("textbox", { name: "Búsqueda exacta para Gmail" });
    expect(exactSearch).toHaveValue("rfc822msgid:test@example.com");
    await user.click(screen.getByRole("button", { name: "Copiar búsqueda" }));
    expect(clipboardWrite).toHaveBeenCalledWith("rfc822msgid:test@example.com");
    expect(await screen.findByRole("status")).toHaveTextContent("Búsqueda copiada");

    clipboardWrite.mockRejectedValueOnce(new Error("Clipboard denied"));
    await user.click(screen.getByRole("button", { name: "Copiada" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Mantén pulsado el campo");

    Reflect.deleteProperty(window.navigator, "clipboard");
    await user.click(screen.getByRole("button", { name: "Copiar búsqueda" }));
    expect(clipboardWrite).toHaveBeenCalledTimes(2);
    expect(await screen.findByRole("alert")).toHaveTextContent("Mantén pulsado el campo");

    let resolvePendingCopy!: () => void;
    Object.defineProperty(window.navigator, "clipboard", {
      configurable: true,
      value: {
        writeText: vi.fn(() => new Promise<void>((resolve) => { resolvePendingCopy = resolve; })),
      },
    });
    await user.click(screen.getByRole("button", { name: "Copiar búsqueda" }));
    expect(screen.getByRole("button", { name: "Copiando…" })).toBeDisabled();
    await user.click(dialog.querySelector(".email-preview-sheet__close")!);
    await user.click(searchLink);
    await screen.findByRole("link", { name: "Buscar correo en Gmail Web" });
    await act(async () => { resolvePendingCopy(); });
    expect(screen.queryByText("Búsqueda copiada. Abre Gmail y pégala en Buscar correo.")).not.toBeInTheDocument();

    await user.click(screen.getByRole("dialog").querySelector(".email-preview-sheet__close")!);
    resolveTarget.mockResolvedValueOnce({
      targetUrl: "https://mail.google.com/mail/#search/rfc822msgid%3Abad%ZZexample.com",
    });
    await user.click(searchLink);
    await screen.findByRole("link", { name: "Buscar correo en Gmail Web" });
    expect(screen.queryByRole("textbox", { name: "Búsqueda exacta para Gmail" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copiar búsqueda" })).not.toBeInTheDocument();
  });

  it("drops mail cards whose preview route escapes the same-origin session contract", async () => {
    vi.spyOn(api, "sessionHistory").mockResolvedValue({
      sessionStatus: "ready",
      activeOperation: null,
      items: [{
        id: "assistant-unsafe-mail",
        role: "assistant",
        content: "No abriré referencias no verificadas por Control.",
        controlEmailReferences: [{
          schemaVersion: 1,
          id: "fedcba9876543210fedcba9876543210",
          provider: "gmail",
          subject: "Referencia insegura",
          previewUrl: "https://attacker.example/email",
          openUrl: "javascript:alert(1)",
        }],
      }],
    });
    render(<ReopenedChat />);

    expect(await screen.findByText("No abriré referencias no verificadas por Control.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Ver correo: Referencia insegura" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Referencia insegura/ })).not.toBeInTheDocument();
  });

  it("renders a Hostinger/Himalaya reference as a preview-only IMAP card", async () => {
    const previewUrl = "/api/v1/sessions/session-papers/email-references/abcdef0123456789abcdef0123456789";
    vi.spyOn(api, "sessionHistory").mockResolvedValue({
      sessionStatus: "ready",
      activeOperation: null,
      items: [{
        id: "assistant-imap-mail",
        role: "assistant",
        content: "Encontré este correo en Hostinger.",
        controlEmailReferences: [{
          schemaVersion: 1,
          id: "abcdef0123456789abcdef0123456789",
          provider: "imap",
          subject: "Factura disponible",
          previewUrl,
        }],
      }],
    });
    vi.spyOn(api, "emailReferencePreview").mockResolvedValue({
      schemaVersion: 1,
      id: "abcdef0123456789abcdef0123456789",
      provider: "imap",
      subject: "Factura disponible",
      previewUrl,
      bodyText: "Tu factura ya está disponible.",
    });

    const user = userEvent.setup();
    render(<ReopenedChat />);
    const card = await screen.findByRole("button", { name: "Ver correo: Factura disponible" });
    expect(screen.getByText("Correo IMAP")).toBeVisible();
    expect(screen.queryByRole("link", { name: /Factura disponible/ })).not.toBeInTheDocument();

    await user.click(card);
    expect(await screen.findByText("Tu factura ya está disponible.")).toBeVisible();
    expect(screen.getByRole("dialog", { name: "Factura disponible" })).toBeVisible();
  });

  it("shows a compact summary when the agent did not retain the full email body", async () => {
    const previewUrl = "/api/v1/sessions/session-papers/email-references/abcdef0123456789abcdef0123456790";
    vi.spyOn(api, "sessionHistory").mockResolvedValue({
      sessionStatus: "ready",
      activeOperation: null,
      items: [{
        id: "assistant-summary-mail",
        role: "assistant",
        content: "Encontré un correo que requiere atención.",
        controlEmailReferences: [{
          schemaVersion: 1,
          id: "abcdef0123456789abcdef0123456790",
          provider: "imap",
          subject: "Resumen disponible",
          snippet: "Solicita una respuesta antes del viernes.",
          previewUrl,
        }],
      }],
    });
    vi.spyOn(api, "emailReferencePreview").mockResolvedValue({
      schemaVersion: 1,
      id: "abcdef0123456789abcdef0123456790",
      provider: "imap",
      subject: "Resumen disponible",
      snippet: "Solicita una respuesta antes del viernes.",
      previewUrl,
    });

    const user = userEvent.setup();
    render(<ReopenedChat />);
    await user.click(await screen.findByRole("button", { name: "Ver correo: Resumen disponible" }));

    const dialog = screen.getByRole("dialog", { name: "Resumen disponible" });
    expect(within(dialog).getByText("Solicita una respuesta antes del viernes.")).toBeVisible();
    expect(within(dialog).getByText("Resumen proporcionado por el agente; no es el cuerpo completo del correo.")).toBeVisible();
    expect(within(dialog).queryByText("Este correo no tiene contenido de texto disponible.")).not.toBeInTheDocument();
    expect(dialog.querySelector(".email-preview-sheet")).toHaveClass("is-summary-only");
  });
});
