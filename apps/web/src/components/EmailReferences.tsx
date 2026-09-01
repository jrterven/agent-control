import { ArrowSquareOut, EnvelopeSimple, FileText, Info, SpinnerGap, WarningCircle, X } from "@phosphor-icons/react";
import { type MouseEvent, useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../lib/api";
import { formatConversationTimestamp } from "../lib/dateTime";
import { useOverlayDialog } from "../lib/useOverlayDialog";
import { useAppStore } from "../store/appStore";
import type { EmailReference } from "../types";

type PreviewState = "idle" | "loading" | "ready" | "error";

function referenceSender(reference: EmailReference, providerLabel: string) {
  return reference.senderName || reference.senderAddress || providerLabel;
}

function openActionKey(reference: EmailReference) {
  return reference.openMode === "search" ? "searchIn" : "openIn";
}

function isInstalledPwa() {
  const standaloneNavigator = navigator as Navigator & { standalone?: boolean };
  return standaloneNavigator.standalone === true
    || window.matchMedia?.("(display-mode: standalone)").matches === true;
}

function safeResolvedTarget(reference: EmailReference, value: unknown) {
  if (typeof value !== "string" || value.length > 2_048) return "";
  try {
    const target = new URL(value);
    if (target.protocol !== "https:" || target.username || target.password || (target.port && target.port !== "443")) return "";
    const host = target.hostname.toLowerCase().replace(/\.$/, "");
    if (reference.provider === "gmail" && host !== "mail.google.com") return "";
    if (reference.provider === "outlook" && !["outlook.live.com", "outlook.office.com", "outlook.office365.com"].includes(host)) return "";
    if (reference.provider === "imap") return "";
    return target.href;
  } catch {
    return "";
  }
}

function EmailProviderMark({ provider }: { provider: EmailReference["provider"] }) {
  return <span className={`email-provider-mark email-provider-mark--${provider}`} aria-hidden="true">
    {provider === "gmail" ? "G" : provider === "outlook" ? "O" : <EnvelopeSimple weight="bold" />}
  </span>;
}

export function EmailReferences({ references, sessionId, agentName }: { references: EmailReference[]; sessionId: string; agentName: string }) {
  const { t, i18n } = useTranslation();
  const timeZone = useAppStore((state) => state.timeZone);
  const [selected, setSelected] = useState<EmailReference>();
  const [previewState, setPreviewState] = useState<PreviewState>("idle");
  const [bodyText, setBodyText] = useState("");
  const [summaryOnly, setSummaryOnly] = useState(false);
  const [openError, setOpenError] = useState("");
  const requestGenerationRef = useRef(0);
  const headingId = useId();
  const descriptionId = useId();
  const trustId = useId();

  const closePreview = () => {
    requestGenerationRef.current += 1;
    setSelected(undefined);
    setPreviewState("idle");
    setBodyText("");
    setSummaryOnly(false);
    setOpenError("");
  };
  const dialog = useOverlayDialog<HTMLDivElement>({
    open: Boolean(selected),
    onClose: closePreview,
    mediaQuery: "(min-width: 0px)",
  });

  useEffect(() => () => { requestGenerationRef.current += 1; }, []);

  const openPreview = async (reference: EmailReference) => {
    const generation = requestGenerationRef.current + 1;
    requestGenerationRef.current = generation;
    setSelected(reference);
    setBodyText("");
    setSummaryOnly(false);
    setOpenError("");
    setPreviewState("loading");
    try {
      const preview = await api.emailReferencePreview(sessionId, reference.id);
      if (requestGenerationRef.current !== generation) return;
      const fullBody = typeof preview.bodyText === "string" ? preview.bodyText.slice(0, 100_000) : "";
      const fallbackSnippet = typeof preview.snippet === "string"
        ? preview.snippet
        : typeof reference.snippet === "string" ? reference.snippet : "";
      setBodyText(fullBody || fallbackSnippet);
      setSummaryOnly(!fullBody && Boolean(fallbackSnippet));
      setPreviewState("ready");
    } catch {
      if (requestGenerationRef.current !== generation) return;
      setPreviewState("error");
    }
  };

  const openFromInstalledPwa = async (event: MouseEvent<HTMLAnchorElement>, reference: EmailReference) => {
    if (!isInstalledPwa()) return;
    event.preventDefault();
    setOpenError("");
    try {
      const resolved = await api.emailReferenceOpenTarget(sessionId, reference.id);
      const target = safeResolvedTarget(reference, resolved.targetUrl);
      if (!target) throw new Error("Unsafe email target");
      // Navigating the standalone context does not depend on the external
      // browser sharing Agent Control's HttpOnly session cookie.
      window.open(target, "_self");
    } catch {
      setOpenError(t("chat.emailReferences.openError"));
    }
  };

  return <>
    <section className="email-references" aria-label={t("chat.emailReferences.listLabel")}>
      {references.map((reference) => {
        const providerLabel = t(`chat.emailReferences.providers.${reference.provider}`);
        const sender = referenceSender(reference, providerLabel);
        const receivedAt = reference.receivedAt
          ? formatConversationTimestamp(reference.receivedAt, i18n.resolvedLanguage ?? i18n.language, timeZone)
          : "";
        const referenceTrustId = `${trustId}-${reference.id}`;
        return <div className="email-reference-card" key={reference.id}>
          <button
            type="button"
            className="email-reference-card__main"
            aria-label={t("chat.emailReferences.previewAria", { subject: reference.subject })}
            aria-describedby={referenceTrustId}
            onClick={() => void openPreview(reference)}
          >
            <EmailProviderMark provider={reference.provider} />
            <span className="email-reference-card__content">
              <span className="email-reference-card__meta">
                <strong>{sender}</strong>
                {receivedAt ? <time dateTime={reference.receivedAt}>{receivedAt}</time> : null}
              </span>
              {reference.senderName && reference.senderAddress ? <small>{reference.senderAddress}</small> : null}
              <b>{reference.subject}</b>
              <span className="email-reference-trust" id={referenceTrustId}><Info weight="fill" aria-hidden="true" /> {t("chat.emailReferences.citedNotVerified", { agent: agentName })}</span>
              {reference.snippet ? <span className="email-reference-card__snippet">{reference.snippet}</span> : null}
            </span>
          </button>
          {reference.openUrl ? <a
            className="email-reference-card__open"
            href={reference.openUrl}
            target="_blank"
            rel="noopener noreferrer"
            referrerPolicy="no-referrer"
            onClick={(event) => void openFromInstalledPwa(event, reference)}
            aria-label={t(reference.openMode === "search" ? "chat.emailReferences.searchAria" : "chat.emailReferences.openAria", { provider: providerLabel, subject: reference.subject })}
            title={t(`chat.emailReferences.${openActionKey(reference)}`, { provider: providerLabel })}
          ><ArrowSquareOut weight="bold" /></a> : null}
        </div>;
      })}
      {openError && !selected ? <p className="email-references__error" role="alert"><WarningCircle aria-hidden="true" /> {openError}</p> : null}
    </section>

    {selected ? <div
      ref={dialog.containerRef}
      tabIndex={-1}
      className="modal-layer email-preview-layer"
      role="dialog"
      aria-modal="true"
      aria-labelledby={headingId}
      aria-describedby={descriptionId}
    >
      <button className="modal-scrim" tabIndex={-1} aria-label={t("chat.emailReferences.close")} onClick={closePreview} />
      <section className={`hc-panel email-preview-sheet${summaryOnly ? " is-summary-only" : ""}`}>
        <header className="email-preview-sheet__header">
          <EmailProviderMark provider={selected.provider} />
          <span>
            <small>{referenceSender(selected, t(`chat.emailReferences.providers.${selected.provider}`))}</small>
            <h2 id={headingId} title={selected.subject}>{selected.subject}</h2>
          </span>
          <button type="button" className="email-preview-sheet__close" aria-label={t("chat.emailReferences.close")} onClick={closePreview}><X weight="bold" /></button>
        </header>
        <div className="email-preview-sheet__details" id={descriptionId}>
          {selected.senderName && selected.senderAddress ? <span>{selected.senderAddress}</span> : null}
          {selected.receivedAt ? <time dateTime={selected.receivedAt}>{formatConversationTimestamp(selected.receivedAt, i18n.resolvedLanguage ?? i18n.language, timeZone)}</time> : null}
        </div>
        <div className={`email-preview-sheet__body is-${previewState}`}>
          {previewState === "loading" ? <p className="email-preview-sheet__state" role="status"><SpinnerGap className="email-preview-sheet__spinner" aria-hidden="true" /> {t("chat.emailReferences.loading")}</p> : null}
          {previewState === "error" ? <p className="email-preview-sheet__state email-preview-sheet__state--error" role="alert"><WarningCircle aria-hidden="true" /> {t("chat.emailReferences.loadError")}</p> : null}
          {previewState === "ready" && bodyText ? <p className="email-preview-sheet__message">{bodyText}</p> : null}
          {previewState === "ready" && !bodyText ? <p className="email-preview-sheet__state" role="status">{t("chat.emailReferences.emptyBody")}</p> : null}
        </div>
        <footer className="email-preview-sheet__footer">
          <div className="email-preview-sheet__notices">
            <span className="email-reference-trust"><Info weight="fill" aria-hidden="true" /> {t("chat.emailReferences.citedNotVerified", { agent: agentName })}</span>
            <span><FileText aria-hidden="true" /> {t(summaryOnly ? "chat.emailReferences.summaryPreview" : "chat.emailReferences.safePreview")}</span>
            {openError ? <span className="email-preview-sheet__open-error" role="alert"><WarningCircle aria-hidden="true" /> {openError}</span> : null}
          </div>
          {selected.openUrl ? <a
            href={selected.openUrl}
            target="_blank"
            rel="noopener noreferrer"
            referrerPolicy="no-referrer"
            onClick={(event) => void openFromInstalledPwa(event, selected)}
          ><EnvelopeSimple weight="fill" /> {t(`chat.emailReferences.${openActionKey(selected)}`, { provider: t(`chat.emailReferences.providers.${selected.provider}`) })}<ArrowSquareOut /></a> : null}
        </footer>
      </section>
    </div> : null}
  </>;
}
