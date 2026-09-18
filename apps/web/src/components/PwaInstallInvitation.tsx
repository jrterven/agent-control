import { useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { DownloadSimple, X } from "@phosphor-icons/react";
import { Button } from "@hermes-control/ui";
import { useTranslation } from "react-i18next";
import { dismissPwaInstall, requestPwaInstall, usePwaInstallStore } from "../lib/pwaInstall";
import { hasPwaUpdateBlockers, usePwaUpdateStore } from "../lib/pwaUpdate";
import { useOverlayDialog } from "../lib/useOverlayDialog";
import { useAppStore } from "../store/appStore";
import "./PwaInstallInvitation.css";

export function PwaInstallInvitation() {
  const { t } = useTranslation();
  const { mode, installed, dismissedUntil } = usePwaInstallStore();
  const authenticated = useAppStore((state) => state.authState === "authenticated");
  const streaming = useAppStore((state) => Object.keys(state.streamingBySession).length > 0 || Object.keys(state.runtimeTurnBySession).length > 0);
  const overlayOpen = useAppStore((state) => state.leftDrawerOpen || state.activityOpen || state.notificationsOpen || state.commandOpen || state.gatewayMenuOpen);
  const updateBlocked = usePwaUpdateStore((state) => hasPwaUpdateBlockers(state.blockers) || state.status === "applying");
  const [visible, setVisible] = useState(false);
  const [instructions, setInstructions] = useState(false);
  const [error, setError] = useState(false);
  const close = () => { dismissPwaInstall(); setVisible(false); setInstructions(false); setError(false); };
  const dialog = useOverlayDialog<HTMLDivElement>({ open: instructions, onClose: close, mediaQuery: "(min-width: 0px)" });

  useEffect(() => {
    setVisible(false);
    if (!mode || installed || !authenticated || streaming || overlayOpen || updateBlocked || dismissedUntil > Date.now()) return;
    let timer: number;
    const offer = () => {
      const editing = document.activeElement?.matches("input, textarea, select, [contenteditable='true']");
      if (document.visibilityState !== "visible" || editing || document.querySelector('[aria-modal="true"]')) {
        timer = window.setTimeout(offer, 1_000);
      } else setVisible(true);
    };
    timer = window.setTimeout(offer, 8_000);
    return () => window.clearTimeout(timer);
  }, [mode, installed, authenticated, streaming, overlayOpen, updateBlocked, dismissedUntil]);

  useEffect(() => {
    if (installed || !authenticated) { setInstructions(false); setError(false); }
  }, [installed, authenticated]);

  const install = async () => {
    if (mode === "ios") { setInstructions(true); return; }
    const outcome = await requestPwaInstall();
    if (outcome === "error" || outcome === "unavailable") setError(true);
  };

  if (!authenticated || installed || (!visible && !instructions && !error)) return null;
  return createPortal(<>
    {!instructions ? <aside className="pwa-install-invitation" aria-labelledby="pwa-install-title">
      <button type="button" className="pwa-install-close" aria-label={t("pwaInstall.close")} onClick={close}><X size={18} aria-hidden="true" /></button>
      <div className="pwa-install-icon"><DownloadSimple size={24} aria-hidden="true" /></div>
      <h2 id="pwa-install-title">{t("pwaInstall.title")}</h2>
      <p>{t(error ? "pwaInstall.error" : "pwaInstall.description")}</p>
      <div className="pwa-install-actions">
        <Button size="sm" variant="ghost" onClick={close}>{t(error ? "pwaInstall.done" : "pwaInstall.later")}</Button>
        {!error ? <Button size="sm" variant="primary" onClick={() => void install()}>{t(mode === "ios" ? "pwaInstall.instructions" : "pwaInstall.install")}</Button> : null}
      </div>
    </aside> : <div className="modal-layer" role="presentation">
      <button className="modal-scrim" aria-label={t("pwaInstall.close")} onClick={close} />
      <div ref={dialog.containerRef} tabIndex={-1} className="hc-panel form-modal pwa-install-instructions" role="dialog" aria-modal="true" aria-labelledby="pwa-install-instructions-title">
        <h2 id="pwa-install-instructions-title">{t("pwaInstall.instructionsTitle")}</h2>
        <ol><li>{t("pwaInstall.safari")}</li><li>{t("pwaInstall.share")}</li><li>{t("pwaInstall.add")}</li></ol>
        <Button variant="primary" onClick={close}>{t("pwaInstall.done")}</Button>
      </div>
    </div>}
  </>, document.body);
}
