import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MessageMarkdown } from "../components/MessageMarkdown";
import { useAppStore } from "../store/appStore";

const url = `/api/v1/sessions/session/files/${"a".repeat(32)}`;
const content = `Documento listo. [Descargar propuesta](${url})`;

beforeEach(() => useAppStore.setState({ authGeneration: 1 }));
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it("downloads on demand with authentication, the original filename and no navigation", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response("# Documento", { headers: {
    "Content-Disposition": "attachment; filename*=UTF-8''curr%C3%ADcula%20IA.md",
  } }));
  vi.stubGlobal("fetch", fetchMock);
  const create = vi.fn().mockReturnValue("blob:document");
  vi.stubGlobal("URL", class extends URL { static createObjectURL = create; static revokeObjectURL = vi.fn(); });
  const clicked: HTMLAnchorElement[] = [];
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) { clicked.push(this); });
  render(<MessageMarkdown sessionId="session" content={content} />);
  expect(fetchMock).not.toHaveBeenCalled();
  const link = screen.getByRole("link", { name: "Descargar propuesta" });
  expect(link).toHaveAttribute("download");
  fireEvent.click(link);
  await waitFor(() => expect(clicked).toHaveLength(1));
  expect(fetchMock).toHaveBeenCalledWith(url, expect.objectContaining({ credentials: "same-origin", cache: "no-store", redirect: "error" }));
  expect(clicked[0].download).toBe("currícula IA.md");
  expect(clicked[0].href).toBe("blob:document");
  // Node's fetch Blob and JSDOM's Blob can belong to different realms.
  // Verify the delivered payload size, not the runner's constructor identity.
  expect(create).toHaveBeenCalledWith(expect.objectContaining({ size: 11 }));
});

it("reports unavailable files and lets the user retry without leaving the chat", async () => {
  const fetchMock = vi.fn().mockResolvedValue(new Response("", { status: 404 }));
  vi.stubGlobal("fetch", fetchMock);
  render(<MessageMarkdown sessionId="session" content={content} />);
  fireEvent.click(screen.getByRole("link", { name: "Descargar propuesta" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("equipo del agente esté conectado");
  fireEvent.click(screen.getByRole("link", { name: "Descargar propuesta" }));
  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
});

it("keeps unverified file markers inert while streaming and preserves code examples", () => {
  const fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  const { container } = render(<MessageMarkdown sessionId="session" streaming content={
    "[Descargar](ac-file:documento.md)\n\n`[Ejemplo](ac-file:documento.md)`\n\n[Web](https://example.com/file.md)"
  } />);
  expect(screen.queryByRole("link", { name: "Descargar" })).not.toBeInTheDocument();
  expect(screen.getByRole("link", { name: "Web" })).not.toHaveAttribute("download");
  expect(container.querySelector("code")).toHaveTextContent("ac-file:documento.md");
  expect(fetchMock).not.toHaveBeenCalled();
});

it("cancels a pending download on logout", async () => {
  let signal: AbortSignal | undefined;
  vi.stubGlobal("fetch", vi.fn((_url, options) => { signal = options.signal; return new Promise(() => undefined); }));
  const view = render(<MessageMarkdown sessionId="session" content={content} />);
  fireEvent.click(screen.getByRole("link", { name: "Descargar propuesta" }));
  view.unmount();
  expect(signal?.aborted).toBe(true);
});
