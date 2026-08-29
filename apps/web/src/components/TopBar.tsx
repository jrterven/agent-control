import { CaretDown, List, Pulse, SidebarSimple } from "@phosphor-icons/react";
import { IconButton, StatusDot } from "@hermes-control/ui";
import { useTranslation } from "react-i18next";
import { useAppStore } from "../store/appStore";
import { BrandMark } from "./BrandMark";

const connectionLabelKeys = {
  connected: "connection.connected",
  reconnecting: "connection.reconnecting",
  degraded: "connection.degraded",
  offline: "connection.offline",
} as const;

export function TopBar() {
  const { t } = useTranslation();
  const profileId = useAppStore((state) => state.selectedProfileId);
  const workspaceId = useAppStore((state) => state.selectedWorkspaceId);
  const connection = useAppStore((state) => state.connection);
  const setLeftOpen = useAppStore((state) => state.setLeftDrawerOpen);
  const setActivityOpen = useAppStore((state) => state.setActivityOpen);
  const leftOpen = useAppStore((state) => state.leftDrawerOpen);
  const activityOpen = useAppStore((state) => state.activityOpen);
  const demoMode = useAppStore((state) => state.demoMode);
  const profiles = useAppStore((state) => state.profiles);
  const workspaces = useAppStore((state) => state.workspaces);
  const profile = profiles.find((item) => item.id === profileId) ?? profiles[0];
  const workspace = workspaces.find((item) => item.id === workspaceId);

  return (
    <header className="top-bar">
      <IconButton className="top-bar__menu" label={t("nav.openNavigation")} icon={<List size={25} />} selected={leftOpen} aria-controls="left-sidebar" aria-expanded={leftOpen} onClick={() => setLeftOpen(!leftOpen)} />
      <div className="top-bar__identity">
        <BrandMark size="sm" />
        <button className="identity-button" type="button" onClick={() => setLeftOpen(true)}>
          <span className="identity-button__name">{profile?.displayName ?? t("nav.noAgent")}</span>
          <CaretDown size={16} />
          <span className="identity-button__status"><StatusDot tone={connection === "connected" ? "positive" : connection === "reconnecting" ? "warning" : "negative"} />{demoMode ? t("connection.localMock") : t(connectionLabelKeys[connection])}</span>
        </button>
      </div>
      <button className="workspace-switcher" type="button" onClick={() => setLeftOpen(true)}>
        <span>{workspace?.name ?? t("nav.noWorkspace")}</span><CaretDown size={16} />
      </button>
      <IconButton className="top-bar__activity" label={t("nav.openActivityContext")} icon={<Pulse size={23} />} selected={activityOpen} aria-controls="activity-panel" aria-expanded={activityOpen} onClick={() => setActivityOpen(!activityOpen)} />
      <IconButton className="top-bar__sidebar" label={t("nav.showContext")} icon={<SidebarSimple size={23} />} selected={activityOpen} aria-controls="activity-panel" aria-expanded={activityOpen} onClick={() => setActivityOpen(!activityOpen)} />
    </header>
  );
}
