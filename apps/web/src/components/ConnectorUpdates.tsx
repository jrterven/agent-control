import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@hermes-control/ui";
import type { ConnectorView } from "@hermes-control/shared-types";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";

export function ConnectorUpdates({ item, onChange }: { item: ConnectorView; onChange: (item: ConnectorView) => void }) {
  const { t } = useTranslation();
  const offline = useAppStore((state) => state.authState !== "authenticated");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const update = item.update;
  if (item.status === "revoked") return null;
  const perform = async (action: "now" | "postpone" | "preferences", automatic?: boolean) => {
    if (busy || offline) return;
    const scope = useAppStore.getState();
    setBusy(true); setMessage("");
    try {
      const result = await api.updateConnector(item.id, action, automatic, scope.csrfToken);
      const current = useAppStore.getState();
      if (current.userId !== scope.userId || current.authGeneration !== scope.authGeneration || current.authState !== "authenticated") return;
      onChange(result); setMessage("saved");
    } catch { setMessage("error"); }
    finally { setBusy(false); }
  };
  if (!update?.supported) return <div className="connector-updates"><p>{t("connectorUpdates.manual")}</p><p>{t("connectorUpdates.bootstrap")}</p></div>;
  const installing = ["downloading", "installing"].includes(update.state);
  return <div className="connector-updates">
    <p role="status">{t(`connectorUpdates.${update.state}`)}{item.status === "offline" ? ` · ${t("connectorUpdates.offline")}` : ""}</p>
    {update.reason ? <p>{t(`connectorUpdates.reasons.${update.reason}`)}</p> : null}
    {update.availableRelease && update.availableRelease !== update.release ? <p>{t("connectorUpdates.target", { version: update.availableRelease.slice(0, 8) })}</p> : null}
    <label className="connector-profile"><input type="checkbox" checked={update.automatic} disabled={busy || offline} onChange={(event) => void perform("preferences", event.target.checked)} /><span>{t("connectorUpdates.automatic")}</span></label>
    <div className="connector-update-actions">
      <Button size="sm" disabled={busy || offline || installing} onClick={() => void perform("now")}>{t("connectorUpdates.now")}</Button>
      <Button size="sm" variant="ghost" disabled={busy || offline || installing} onClick={() => void perform("postpone")}>{t("connectorUpdates.postpone")}</Button>
    </div>
    {message ? <p role={message === "error" ? "alert" : "status"}>{t(`connectorUpdates.${message}`)}</p> : null}
  </div>;
}
