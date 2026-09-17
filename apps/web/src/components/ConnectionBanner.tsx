import { ArrowsClockwise, CloudSlash, Desktop } from "@phosphor-icons/react";
import { useTranslation } from "react-i18next";
import { useCloudConfigurationStore } from "../lib/cloud";
import { useAppStore } from "../store/appStore";

export function ConnectionBanner() {
  const { t } = useTranslation();
  const connection = useAppStore((state) => state.connection);
  const demoMode = useAppStore((state) => state.demoMode);
  const cloud = useCloudConfigurationStore((state) => state.methods?.mode === "cloud");
  const hasNoComputers = useAppStore((state) => state.authState === "authenticated"
    && state.bootstrapLoaded && state.gateways.length === 0);
  if (connection === "connected" || demoMode) return null;
  if (connection === "reconnecting") return (
    <div className="connection-banner" role="status">
      <ArrowsClockwise className="spin" />
      <span>{t("connection.reconnectingMessage")}</span>
    </div>
  );
  if (cloud && hasNoComputers && connection === "degraded") return (
    <div className="connection-banner connection-banner--setup" role="status">
      <Desktop />
      <span>{t("cloud.empty")}</span>
    </div>
  );
  return (
    <div className="connection-banner connection-banner--danger" role="alert">
      <CloudSlash />
      <span>{t("connection.degradedMessage")}</span>
    </div>
  );
}
