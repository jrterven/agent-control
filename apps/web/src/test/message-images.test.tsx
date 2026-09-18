import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MessageMarkdown } from "../components/MessageMarkdown";
import { api, ApiError } from "../lib/api";
import { useAppStore } from "../store/appStore";
import type { ImageMediaMetadata } from "../types";

const first = "a".repeat(32);
const second = "b".repeat(32);
const ready = (id: string): ImageMediaMetadata => ({ id, kind: "image", status: "ready", mediaType: "image/png", width: 800, height: 500, alt: "Figura", caption: "Un resultado verificado", provenance: "web", sourceUrl: "https://example.com/paper", sourceTitle: "Artículo original" });
const markdown = `Texto anterior\n\n![Diagrama](ac-media:${first})\n\n![Resultados](ac-media:${second})\n\nTexto posterior`;

beforeEach(() => {
  useAppStore.setState({ authState: "authenticated", userId: "image-owner", authGeneration: 1, features: undefined });
  vi.spyOn(api, "imageMetadata").mockImplementation(async (_session, id) => ready(id));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); vi.useRealTimers(); });

describe("private images in agent Markdown", () => {
  it("requests metadata through the authenticated same-origin API exactly once", async () => {
    vi.mocked(api.imageMetadata).mockRestore();
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify(ready(first))));
    vi.stubGlobal("fetch", fetchMock);
    await api.imageMetadata("a/session", first);
    expect(fetchMock).toHaveBeenCalledWith(`/api/v1/sessions/a%2Fsession/media/${first}/metadata`, expect.objectContaining({ credentials: "same-origin", cache: "no-store", signal: expect.any(AbortSignal) }));
  });
  it("groups adjacent references in place, loads only same-origin lazy thumbnails and preserves prose", async () => {
    const { container } = render(<MessageMarkdown content={markdown} sessionId="a/session" />);
    const gallery = screen.getByRole("region", { name: "Imágenes de la respuesta" });
    expect(await within(gallery).findAllByRole("img")).toHaveLength(2);
    expect(api.imageMetadata).toHaveBeenCalledWith("a/session", first, expect.any(AbortSignal));
    const images = within(gallery).getAllByRole("img");
    expect(images[0]).toHaveAttribute("src", `/api/v1/sessions/a%2Fsession/media/${first}?variant=thumbnail`);
    expect(images[0]).toHaveAttribute("loading", "lazy");
    expect(images[0]).toHaveAccessibleName("Diagrama");
    expect(container.children[0]).toHaveTextContent("Texto anterior");
    expect(container.children[1]).toBe(gallery);
    expect(container.children[2]).toHaveTextContent("Texto posterior");
    expect(screen.getAllByRole("link", { name: "Artículo original" })[0]).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("does not turn examples in code or arbitrary URLs into image fetches", () => {
    const { container } = render(<MessageMarkdown sessionId="session" content={`\`![Ejemplo](ac-media:${first})\`\n\n![Externa](https://example.com/tracker.png)\n\n![Local](file:///private/photo.png)\n\n<img src="https://example.com/raw.png" onerror="alert(1)" />\n\n![Falsa](ac-media:not-an-id)`} />);
    expect(container.querySelectorAll("img")).toHaveLength(0);
    expect(api.imageMetadata).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Imagen externa: Externa" })).toHaveAttribute("href", "https://example.com/tracker.png");
    expect(container.querySelector("code")).toHaveTextContent(`![Ejemplo](ac-media:${first})`);
    expect(screen.getByText("Local")).toBeVisible();
    expect(screen.getByText("Falsa")).toBeVisible();
  });

  it("handles streamed partial markers without displaying private references", async () => {
    const { container, rerender } = render(<MessageMarkdown sessionId="session" streaming content="Inicio ![Diagrama](ac-media:aaaa" />);
    expect(container).toHaveTextContent("Inicio");
    expect(container).not.toHaveTextContent("ac-media");
    expect(api.imageMetadata).not.toHaveBeenCalled();
    rerender(<MessageMarkdown sessionId="session" streaming content={`Inicio ![Diagrama](ac-media:${first})\n\nFinal`} />);
    expect(await screen.findByRole("img", { name: "Diagrama" })).toBeVisible();
    expect(screen.getByText("Final")).toBeVisible();
  });

  it("keeps existing images mounted when more streamed prose arrives", async () => {
    const content = `![Diagrama](ac-media:${first})\n\nAnálisis`;
    const { rerender } = render(<MessageMarkdown content={content} sessionId="session" streaming />);
    const image = await screen.findByRole("img");
    rerender(<MessageMarkdown content={`${content} ampliado`} sessionId="session" streaming />);
    expect(screen.getByRole("img")).toBe(image);
    expect(api.imageMetadata).toHaveBeenCalledTimes(1);
  });

  it("bounds galleries to six and each response to twenty-four image requests", async () => {
    const content = Array.from({ length: 26 }, (_, index) => `![Imagen ${index}](ac-media:${index.toString(16).padStart(32, "0")})`).join("\n\n");
    render(<MessageMarkdown content={content} sessionId="session" />);
    await waitFor(() => expect(screen.getAllByRole("img")).toHaveLength(24));
    expect(screen.getAllByRole("region", { name: "Imágenes de la respuesta" })).toHaveLength(4);
    expect(api.imageMetadata).toHaveBeenCalledTimes(24);
    expect(screen.getByText("Imagen 25")).toBeVisible();
  });

  it("respects the deployment's lower gallery and response limits", async () => {
    useAppStore.setState({ features: { images: { maxImagesPerGallery: 1, maxImagesPerResponse: 2 }, dictation: { available: false, provider: "elevenlabs", modelId: "scribe_v2_realtime" } } });
    render(<MessageMarkdown content={`${markdown}\n\n![Exceso](ac-media:${"c".repeat(32)})`} sessionId="session" />);
    await waitFor(() => expect(screen.getAllByRole("img")).toHaveLength(2));
    expect(screen.getAllByRole("region", { name: "Imágenes de la respuesta" })).toHaveLength(2);
    expect(api.imageMetadata).toHaveBeenCalledTimes(2);
  });

  it("provides keyboard navigation, zoom, download, source and restores focus after closing", async () => {
    const user = userEvent.setup();
    render(<MessageMarkdown content={markdown} sessionId="session" />);
    const opener = await screen.findByRole("button", { name: "Ampliar imagen: Diagrama" });
    await user.click(opener);
    const viewer = screen.getByRole("dialog", { name: "Visor de imágenes" });
    expect(within(viewer).getByRole("img")).toHaveAttribute("src", `/api/v1/sessions/session/media/${first}?variant=full`);
    expect(within(viewer).getByRole("link", { name: "Descargar imagen" })).toHaveAttribute("download", `${first}.png`);
    await user.click(within(viewer).getByRole("button", { name: "Ampliar" }));
    expect(within(viewer).getByRole("button", { name: "Ajustar a pantalla" })).toBeVisible();
    await user.keyboard("{ArrowRight}");
    expect(within(viewer).getByRole("img")).toHaveAccessibleName("Resultados");
    expect(within(viewer).getByRole("button", { name: "Ampliar" })).toBeVisible();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });

  it("restores the opener even when a pointer click did not focus it in Safari", async () => {
    render(<MessageMarkdown content={`![Diagrama](ac-media:${first})`} sessionId="session" />);
    const opener = await screen.findByRole("button", { name: "Ampliar imagen: Diagrama" });
    expect(opener).not.toHaveFocus();
    fireEvent.click(opener);
    fireEvent.click(screen.getByRole("button", { name: "Cerrar visor" }));
    expect(opener).toHaveFocus();
  });

  it("polls an unpublished reference, then renders it when publication completes", async () => {
    vi.useFakeTimers();
    vi.mocked(api.imageMetadata).mockRejectedValueOnce(new ApiError(404, "Not found")).mockResolvedValueOnce(ready(first));
    render(<MessageMarkdown content={`![Diagrama](ac-media:${first})`} sessionId="session" />);
    await act(async () => {});
    expect(screen.getByText("Publicando imagen…")).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByRole("img")).toHaveAccessibleName("Diagrama");
    expect(api.imageMetadata).toHaveBeenCalledTimes(2);
  });

  it("bounds pending polling and offers an explicit retry", async () => {
    vi.useFakeTimers();
    vi.mocked(api.imageMetadata).mockRejectedValue(new ApiError(404, "Not found"));
    render(<MessageMarkdown content={`![Diagrama](ac-media:${first})`} sessionId="session" />);
    await act(async () => { await vi.advanceTimersByTimeAsync(30000); });
    expect(api.imageMetadata).toHaveBeenCalledTimes(7);
    fireEvent.click(screen.getByRole("button", { name: "Reintentar imagen" }));
    await act(async () => {});
    expect(api.imageMetadata).toHaveBeenCalledTimes(8);
  });

  it("does not retry forbidden media and keeps the rest of the response readable", async () => {
    vi.mocked(api.imageMetadata).mockRejectedValue(new ApiError(403, "Forbidden"));
    render(<MessageMarkdown content={`Antes\n\n![Diagrama](ac-media:${first})\n\nDespués`} sessionId="session" />);
    expect(await screen.findByText("No se pudo cargar la imagen")).toBeVisible();
    expect(screen.getByText("Después")).toBeVisible();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(api.imageMetadata).toHaveBeenCalledTimes(1);
  });

  it("rejects mismatched metadata and active formats, and never links unsafe provenance", async () => {
    vi.mocked(api.imageMetadata).mockResolvedValueOnce({ ...ready(first), id: second });
    const { rerender } = render(<MessageMarkdown content={`![Diagrama](ac-media:${first})`} sessionId="session" />);
    expect(await screen.findByText("No se pudo cargar la imagen")).toBeVisible();
    vi.mocked(api.imageMetadata).mockResolvedValue({ ...ready(first), sourceUrl: "javascript:alert(1)" });
    rerender(<MessageMarkdown content={`![Diagrama](ac-media:${first})`} sessionId="another-session" />);
    expect(await screen.findByRole("img")).toBeVisible();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("discards late metadata from an old authentication lifetime", async () => {
    let complete!: (value: ImageMediaMetadata) => void;
    vi.mocked(api.imageMetadata).mockImplementationOnce(() => new Promise((resolve) => { complete = resolve; })).mockRejectedValue(new ApiError(403, "Forbidden"));
    render(<MessageMarkdown content={`![Diagrama](ac-media:${first})`} sessionId="session" />);
    await act(async () => { useAppStore.setState({ userId: "other-owner", authGeneration: 2 }); });
    await act(async () => { complete(ready(first)); });
    expect(await screen.findByText("No se pudo cargar la imagen")).toBeVisible();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("offers retry when the stored thumbnail cannot be decoded", async () => {
    render(<MessageMarkdown content={`![Diagrama](ac-media:${first})`} sessionId="session" />);
    fireEvent.error(await screen.findByRole("img"));
    expect(screen.getByText("No se pudo cargar la imagen")).toBeVisible();
    expect(screen.getByRole("button", { name: "Reintentar imagen" })).toBeVisible();
  });
});
