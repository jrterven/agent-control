import { useEffect, useRef, useState, type ReactNode } from "react";
import { DownloadSimple } from "@phosphor-icons/react";
import { useTranslation } from "react-i18next";
import { useAppStore } from "../store/appStore";

const errors: Record<string, string> = {
  es: "No se pudo descargar el archivo. Comprueba que el equipo del agente esté conectado y vuelve a intentarlo.",
  en: "Could not download the file. Check that the agent’s computer is connected and try again.",
  fr: "Téléchargement impossible. Vérifiez que l’ordinateur de l’agent est connecté et réessayez.",
  de: "Download fehlgeschlagen. Prüfe die Verbindung zum Computer des Agenten und versuche es erneut.",
  pt: "Não foi possível baixar o arquivo. Verifique se o computador do agente está conectado e tente novamente.",
};

export function FileDownloadLink({ href, children }: { href: string; children: ReactNode }) {
  const { i18n } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  const controller = useRef<AbortController | undefined>(undefined);
  const generation = useAppStore((state) => state.authGeneration);
  useEffect(() => () => controller.current?.abort(), [href, generation]);
  async function download() {
    if (controller.current && !controller.current.signal.aborted) return;
    const request = new AbortController();
    controller.current = request;
    setBusy(true);
    setFailed(false);
    try {
      const response = await fetch(href, {
        credentials: "same-origin", cache: "no-store", redirect: "error",
        signal: AbortSignal.any([request.signal, AbortSignal.timeout(60000)]),
      });
      if (!response.ok) throw new Error("Download unavailable");
      const disposition = response.headers.get("Content-Disposition") ?? "";
      const encoded = /^attachment; filename\*=UTF-8''(.+)$/i.exec(disposition)?.[1];
      if (!encoded) throw new Error("Invalid download");
      const filename = decodeURIComponent(encoded);
      const blob = await response.blob();
      if (request.signal.aborted || useAppStore.getState().authGeneration !== generation) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 60000);
    } catch {
      if (!request.signal.aborted) setFailed(true);
    } finally {
      if (controller.current === request) {
        controller.current = undefined;
        if (!request.signal.aborted) setBusy(false);
      }
    }
  }
  return <span className="file-download">
    <a href={href} download aria-disabled={busy || undefined} aria-busy={busy || undefined}
      onClick={(event) => { event.preventDefault(); void download(); }}>
      <DownloadSimple aria-hidden="true" />{children}
    </a>
    {failed ? <span role="alert">{errors[i18n.resolvedLanguage?.split("-")[0] ?? "en"] ?? errors.en}</span> : null}
  </span>;
}
