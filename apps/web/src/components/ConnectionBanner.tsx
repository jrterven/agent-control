import { ArrowsClockwise, CloudSlash } from "@phosphor-icons/react";
import { useTranslation } from "react-i18next";
import { useAppStore } from "../store/appStore";

export function ConnectionBanner() {
  const { t } = useTranslation();
  const connection = useAppStore((state) => state.connection);
  const demoMode = useAppStore((state) => state.demoMode);
  if (connection === "connected" || demoMode) return null;
  if (connection === "reconnecting") return (
    <div className="connection-banner" role="status">
      <ArrowsClockwise className="spin" />
      <span>{t("connection.reconnectingMessage")}</span>
    </div>
  );
  return (
    <div className="connection-banner connection-banner--danger" role="alert">
      <CloudSlash />
      <span>{t("connection.degradedMessage")}</span>
    </div>
  );
}
