import { ArrowsClockwise, CloudSlash } from "@phosphor-icons/react";
import { useAppStore } from "../store/appStore";

export function ConnectionBanner() {
  const connection = useAppStore((state) => state.connection);
  const demoMode = useAppStore((state) => state.demoMode);
  if (connection === "connected" || demoMode) return null;
  if (connection === "reconnecting") return (
    <div className="connection-banner" role="status">
      <ArrowsClockwise className="spin" />
      <span>Reconectando sin reenviar operaciones…</span>
    </div>
  );
  return (
    <div className="connection-banner connection-banner--danger" role="alert">
      <CloudSlash />
      <span>Conexión degradada. Tus borradores siguen disponibles.</span>
    </div>
  );
}
