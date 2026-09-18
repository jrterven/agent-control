import { ArrowSquareOut, CaretLeft, CaretRight, CircleNotch, DownloadSimple, ImageSquare, MagnifyingGlassMinus, MagnifyingGlassPlus, WarningCircle, X } from "@phosphor-icons/react";
import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { api, ApiError } from "../lib/api";
import { safeImageSource, type ImageReference } from "../lib/messageImages";
import { useAppStore } from "../store/appStore";
import type { ImageMediaMetadata } from "../types";

const acceptedTypes = new Set(["image/png", "image/jpeg", "image/webp"]);
const maxPolls = 7;
type ImageState = { metadata?: ImageMediaMetadata; failed?: boolean; waiting?: boolean };

function validMetadata(metadata: ImageMediaMetadata, id: string) {
  return metadata.id === id && metadata.kind === "image"
    && ["ready", "pending", "failed"].includes(metadata.status)
    && (metadata.status !== "ready" || acceptedTypes.has(metadata.mediaType ?? ""));
}

export function MessageImageGallery({ sessionId, references }: { sessionId: string; references: ImageReference[] }) {
  const { t } = useTranslation();
  const generation = useAppStore((state) => state.authGeneration);
  const owner = useAppStore((state) => state.userId);
  const identity = `${generation}:${owner}:${sessionId}`;
  const [state, setState] = useState<{ identity: string; images: Record<string, ImageState> }>({ identity, images: {} });
  const [retry, setRetry] = useState(0);
  const [opened, setOpened] = useState<number | null>(null);
  const openerRef = useRef<HTMLButtonElement | null>(null);
  const ids = references.map((reference) => reference.id).join(",");
  const images = state.identity === identity ? state.images : {};

  useEffect(() => {
    const controller = new AbortController();
    const timers = new Set<ReturnType<typeof setTimeout>>();
    setState({ identity, images: {} });
    setOpened(null);
    const update = (id: string, value: ImageState) => {
      if (!controller.signal.aborted) setState((current) => ({ identity, images: { ...current.images, [id]: value } }));
    };
    const load = async (id: string, attempt: number) => {
      let result: ImageState;
      try {
        const metadata = await api.imageMetadata(sessionId, id, controller.signal);
        if (!validMetadata(metadata, id)) result = { failed: true };
        else result = { metadata, failed: metadata.status === "failed", waiting: metadata.status === "pending" };
      } catch (error) {
        if (controller.signal.aborted) return;
        // An unpublished reference has no row yet. Never retry forbidden or expired access.
        result = error instanceof ApiError && error.status === 404 ? { waiting: true } : { failed: true };
      }
      update(id, result);
      if (result.waiting && attempt + 1 < maxPolls && !controller.signal.aborted) {
        const timer = setTimeout(() => { timers.delete(timer); void load(id, attempt + 1); }, 2000);
        timers.add(timer);
      }
    };
    for (const id of new Set(ids.split(","))) void load(id, 0);
    return () => { controller.abort(); timers.forEach(clearTimeout); };
  }, [ids, identity, sessionId, retry]);

  const markFailed = (id: string) => setState((current) => ({ identity, images: { ...current.images, [id]: { ...current.images[id], failed: true } } }));
  const reset = () => setRetry((value) => value + 1);
  return <section className={`message-image-gallery${references.length === 1 ? " is-single" : ""}`} aria-label={t("images.gallery")}>
    {references.map((reference, index) => {
      const image = images[reference.id];
      const metadata = image?.metadata;
      const ready = metadata?.status === "ready" && !image?.failed;
      const alt = reference.alt || metadata?.alt || t("images.untitled");
      const source = safeImageSource(metadata?.sourceUrl);
      return <figure className="message-image" key={`${reference.id}:${index}`}>
        {ready ? <button className="message-image__open" type="button" aria-label={t("images.open", { alt })} onClick={(event) => { openerRef.current = event.currentTarget; setOpened(index); }}>
          <img src={api.sessionMediaUrl(sessionId, reference.id, "thumbnail")} alt={alt} loading="lazy" decoding="async" width={metadata.width} height={metadata.height} onError={() => markFailed(reference.id)} />
          <span className="message-image__expand" aria-hidden="true"><MagnifyingGlassPlus size={19} /></span>
        </button> : <div className="message-image__placeholder" role="status">
          {image?.failed ? <WarningCircle aria-hidden="true" size={25} /> : <ImageSquare aria-hidden="true" size={25} />}
          <span>{image?.failed ? t("images.failed") : t("images.pending")}</span>
          <small>{alt}</small>
          <button type="button" onClick={reset}>{t("images.retry")}</button>
        </div>}
        {metadata ? <figcaption>
          {metadata.caption ? <span>{metadata.caption}</span> : null}
          <small>{t(`images.${metadata.provenance === "web" || metadata.provenance === "generated" ? metadata.provenance : "local"}`)}</small>
          {source ? <a href={source} target="_blank" rel="noopener noreferrer">{metadata.sourceTitle || t("images.source")} <ArrowSquareOut aria-hidden="true" /></a> : null}
        </figcaption> : null}
      </figure>;
    })}
    {opened !== null && state.identity === identity ? <ImageViewer
      references={references} images={images} sessionId={sessionId} index={opened} returnFocus={openerRef.current}
      onIndex={setOpened} onClose={() => setOpened(null)} onFailure={markFailed} onRetry={reset}
    /> : null}
  </section>;
}

function ImageViewer({ references, images, sessionId, index, onIndex, onClose, onFailure, onRetry, returnFocus }: {
  references: ImageReference[]; images: Record<string, ImageState>; sessionId: string; index: number; returnFocus: HTMLElement | null;
  onIndex: (index: number) => void; onClose: () => void; onFailure: (id: string) => void; onRetry: () => void;
}) {
  const { t } = useTranslation();
  const titleId = useId();
  const descriptionId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const [zoomed, setZoomed] = useState(false);
  const reference = references[index];
  const image = images[reference.id];
  const metadata = image?.metadata;
  const ready = metadata?.status === "ready" && !image?.failed;
  const alt = reference.alt || metadata?.alt || t("images.untitled");
  const source = safeImageSource(metadata?.sourceUrl);
  useEffect(() => { setZoomed(false); stageRef.current?.scrollTo(0, 0); }, [index]);
  useEffect(() => {
    // Safari does not focus buttons on pointer clicks; restore the actual opener.
    const previous = returnFocus ?? (document.activeElement instanceof HTMLElement ? document.activeElement : null);
    const originalOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const root = document.getElementById("root");
    const originalInert = root?.inert;
    if (root) root.inert = true;
    dialogRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    return () => { document.body.style.overflow = originalOverflow; if (root) root.inert = originalInert ?? false; if (previous?.isConnected) previous.focus(); };
  }, []);
  return createPortal(<div className="image-viewer-layer" onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <div ref={dialogRef} className="image-viewer" role="dialog" aria-modal="true" aria-labelledby={titleId} aria-describedby={descriptionId} tabIndex={-1} onKeyDown={(event) => {
      if (event.key === "Escape") { event.preventDefault(); onClose(); }
      if (event.key === "ArrowLeft" && index > 0) { event.preventDefault(); onIndex(index - 1); }
      if (event.key === "ArrowRight" && index + 1 < references.length) { event.preventDefault(); onIndex(index + 1); }
      if (event.key === "Tab") {
        const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>("button:not([disabled]), a[href]") ?? []);
        const first = controls[0];
        const last = controls.at(-1);
        if (!controls.includes(document.activeElement as HTMLElement)) { event.preventDefault(); (event.shiftKey ? last : first)?.focus(); }
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
      }
    }}>
      <header className="image-viewer__toolbar">
        <h2 id={titleId}>{t("images.viewer")}</h2>
        <span aria-live="polite">{t("images.position", { current: index + 1, total: references.length })}</span>
        <button type="button" aria-label={t(zoomed ? "images.zoomOut" : "images.zoomIn")} disabled={!ready} onClick={() => setZoomed((current) => !current)}>{zoomed ? <MagnifyingGlassMinus /> : <MagnifyingGlassPlus />}</button>
        <button type="button" aria-label={t("images.close")} onClick={onClose}><X /></button>
      </header>
      <div className={`image-viewer__stage${zoomed ? " is-zoomed" : ""}`} ref={stageRef}>
        {ready ? <img src={api.sessionMediaUrl(sessionId, reference.id, "full")} alt={alt} onError={() => onFailure(reference.id)} /> : <div className="message-image__placeholder" role="status">{image?.failed ? <WarningCircle /> : <CircleNotch />}<span>{t(image?.failed ? "images.failed" : "images.pending")}</span><button type="button" onClick={onRetry}>{t("images.retry")}</button></div>}
      </div>
      <footer className="image-viewer__footer">
        <div className="image-viewer__caption" id={descriptionId}><strong>{alt}</strong>{metadata?.caption ? <p>{metadata.caption}</p> : null}{metadata ? <small>{t(`images.${metadata.provenance === "web" || metadata.provenance === "generated" ? metadata.provenance : "local"}`)}</small> : null}</div>
        <nav className="image-viewer__actions" aria-label={t("images.gallery")}>
          {source ? <a href={source} target="_blank" rel="noopener noreferrer"><ArrowSquareOut aria-hidden="true" />{metadata?.sourceTitle || t("images.source")}</a> : null}
          {ready ? <a href={api.sessionMediaUrl(sessionId, reference.id, "full")} download={`${reference.id}.${metadata.mediaType === "image/jpeg" ? "jpg" : metadata.mediaType === "image/webp" ? "webp" : "png"}`}><DownloadSimple aria-hidden="true" />{t("images.download")}</a> : null}
          <button type="button" aria-label={t("images.previous")} disabled={index === 0} onClick={() => onIndex(index - 1)}><CaretLeft /></button>
          <button type="button" aria-label={t("images.next")} disabled={index + 1 === references.length} onClick={() => onIndex(index + 1)}><CaretRight /></button>
        </nav>
      </footer>
    </div>
  </div>, document.body);
}
