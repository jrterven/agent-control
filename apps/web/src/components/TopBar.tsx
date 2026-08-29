import { CaretDown, List, Pulse, SidebarSimple } from "@phosphor-icons/react";
import { IconButton, StatusDot } from "@hermes-control/ui";
import { useAppStore } from "../store/appStore";
import { BrandMark } from "./BrandMark";

const connectionLabels = { connected: "Conectado", reconnecting: "Reconectando", degraded: "Degradado", offline: "Sin conexión" } as const;

export function TopBar() {
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
      <IconButton className="top-bar__menu" label="Abrir navegación" icon={<List size={25} />} selected={leftOpen} aria-controls="left-sidebar" aria-expanded={leftOpen} onClick={() => setLeftOpen(!leftOpen)} />
      <div className="top-bar__identity">
        <BrandMark size="sm" />
        <button className="identity-button" type="button" onClick={() => setLeftOpen(true)}>
          <span className="identity-button__name">{profile?.displayName ?? "Sin agente"}</span>
          <CaretDown size={16} />
          <span className="identity-button__status"><StatusDot tone={connection === "connected" ? "positive" : connection === "reconnecting" ? "warning" : "negative"} />{demoMode ? "Mock local" : connectionLabels[connection]}</span>
        </button>
      </div>
      <button className="workspace-switcher" type="button" onClick={() => setLeftOpen(true)}>
        <span>{workspace?.name ?? "Sin workspace"}</span><CaretDown size={16} />
      </button>
      <IconButton className="top-bar__activity" label="Abrir actividad y contexto" icon={<Pulse size={23} />} selected={activityOpen} aria-controls="activity-panel" aria-expanded={activityOpen} onClick={() => setActivityOpen(!activityOpen)} />
      <IconButton className="top-bar__sidebar" label="Mostrar contexto" icon={<SidebarSimple size={23} />} selected={activityOpen} aria-controls="activity-panel" aria-expanded={activityOpen} onClick={() => setActivityOpen(!activityOpen)} />
    </header>
  );
}
