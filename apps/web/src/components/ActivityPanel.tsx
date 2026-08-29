import { useState, type FormEvent } from "react";
import { Archive, CaretDown, CheckCircle, Clock, DownloadSimple, Gauge, Pulse, Robot, Trash, WarningCircle, Wrench, X } from "@phosphor-icons/react";
import { Badge, Button, IconButton, StatusDot, Switch, cx } from "@hermes-control/ui";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";
import { useOverlayDialog } from "../lib/useOverlayDialog";

const usageNumber = new Intl.NumberFormat("es-MX", { maximumFractionDigits: 3 });

function formatUsageNumber(value: number) {
  return usageNumber.format(value);
}

export function ActivityPanel() {
  const open = useAppStore((state) => state.activityOpen);
  const close = useAppStore((state) => state.setActivityOpen);
  const advancedMode = useAppStore((state) => state.advancedMode);
  const setAdvancedMode = useAppStore((state) => state.setAdvancedMode);
  const profileId = useAppStore((state) => state.selectedProfileId);
  const workspaceId = useAppStore((state) => state.selectedWorkspaceId);
  const sessionId = useAppStore((state) => state.selectedSessionId);
  const gatewayId = useAppStore((state) => state.selectedGatewayId);
  const gateways = useAppStore((state) => state.gateways);
  const profiles = useAppStore((state) => state.profiles);
  const workspaces = useAppStore((state) => state.workspaces);
  const sessions = useAppStore((state) => state.sessions);
  const sessionUsageById = useAppStore((state) => state.sessionUsageById);
  const messages = useAppStore((state) => state.messages);
  const demoMode = useAppStore((state) => state.demoMode);
  const authState = useAppStore((state) => state.authState);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const connection = useAppStore((state) => state.connection);
  const removeSession = useAppStore((state) => state.removeSession);
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const setConnection = useAppStore((state) => state.setConnection);
  const workspace = workspaces.find((item) => item.id === workspaceId);
  // No selected session must remain "sin sesión". Falling back to sessions[0]
  // can expose context from a different profile or workspace in this panel.
  const session = sessions.find((item) => item.id === sessionId);
  const profile = profiles.find((item) => item.id === session?.profileId)
    ?? profiles.find((item) => item.id === profileId)
    ?? profiles[0];
  const gateway = gateways.find((item) => item.id === gatewayId) ?? gateways[0];
  const usage = session ? sessionUsageById[session.id] : undefined;
  const contextPercent = usage?.contextPercent !== undefined
    ? Math.min(100, Math.max(0, usage.contextPercent))
    : usage?.contextUsed !== undefined && usage.contextMax !== undefined && usage.contextMax > 0
      ? Math.min(100, (usage.contextUsed / usage.contextMax) * 100)
      : undefined;
  const roundedContextPercent = contextPercent === undefined ? undefined : Math.round(contextPercent);
  const inputTokens = usage?.inputTokens ?? usage?.promptTokens;
  const outputTokens = usage?.outputTokens ?? usage?.completionTokens;
  const latestTools = [...messages].reverse().find((message) => message.sessionId === sessionId && message.tools?.length)?.tools ?? [];
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; title: string; storedSessionId: string } | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [actionError, setActionError] = useState("");
  const [deleteError, setDeleteError] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const canDeleteFromHermes = Boolean(
    session
    && profile?.mutable
    && profile.capabilitySet?.methods.includes("session.delete"),
  );
  const mutationsDisabled = authState !== "authenticated" || demoMode;
  const exportDisabled = authState !== "authenticated" || demoMode;
  const closeDeleteDialog = () => {
    if (deleteBusy) return;
    setDeleteTarget(null);
    setDeleteConfirmation("");
    setDeleteError("");
  };
  const drawer = useOverlayDialog<HTMLElement>({ open: open && !deleteTarget, onClose: () => close(false), mediaQuery: "(max-width: 1199px)" });
  const deleteDialog = useOverlayDialog<HTMLDivElement>({ open: Boolean(deleteTarget), onClose: closeDeleteDialog, mediaQuery: "(min-width: 0px)" });

  const refreshBootstrap = async () => {
    try {
      hydrateBootstrap(await api.bootstrap());
    } catch {
      setConnection("degraded");
    }
  };

  const archiveSession = async () => {
    if (!session || archiveBusy || mutationsDisabled) return;
    const target = { id: session.id, title: session.title };
    setArchiveBusy(true);
    setActionError("");
    setAnnouncement("");
    try {
      await api.archiveSession(target.id, csrfToken);
      removeSession(target.id);
      await refreshBootstrap();
      setAnnouncement(`“${target.title}” se archivó solo en Agent Control. La conversación sigue en Hermes.`);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "No se pudo archivar la sesión.");
    } finally {
      setArchiveBusy(false);
    }
  };

  const exportSession = async () => {
    if (!session || exportBusy || exportDisabled) return;
    setExportBusy(true);
    setActionError("");
    setAnnouncement("");
    try {
      const { blob, filename } = await api.exportSession(session.id);
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = filename ?? `hermes-session-${session.id}.json`;
      link.click();
      URL.revokeObjectURL(url);
      setAnnouncement(`Se exportó “${session.title}” desde el historial autoritativo de Hermes.`);
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "No se pudo exportar la sesión.");
    } finally {
      setExportBusy(false);
    }
  };

  const openDeleteDialog = () => {
    if (!session || !canDeleteFromHermes || mutationsDisabled) return;
    setDeleteTarget({ id: session.id, title: session.title, storedSessionId: session.storedSessionId });
    setDeleteConfirmation("");
    setDeleteError("");
    setActionError("");
  };

  const deleteSession = async (event: FormEvent) => {
    event.preventDefault();
    if (!deleteTarget || deleteBusy || deleteConfirmation !== deleteTarget.storedSessionId) return;
    const target = deleteTarget;
    setDeleteBusy(true);
    setDeleteError("");
    setAnnouncement("");
    try {
      await api.deleteSessionFromHermes(target.id, target.storedSessionId, csrfToken);
      removeSession(target.id);
      await refreshBootstrap();
      setDeleteTarget(null);
      setDeleteConfirmation("");
      setAnnouncement(`“${target.title}” se eliminó de Hermes y de Agent Control.`);
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : "No se pudo eliminar la sesión de Hermes.");
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <>
      <button className={cx("scrim scrim--activity", open && "is-visible")} aria-label="Cerrar actividad" onClick={() => close(false)} />
      <aside
        id="activity-panel"
        ref={drawer.containerRef}
        className={cx("activity-panel", open && "is-open")}
        aria-label="Actividad y contexto"
        aria-hidden={deleteTarget || (drawer.isOverlay && !open) ? true : undefined}
        aria-modal={drawer.active ? true : undefined}
        role={drawer.isOverlay ? "dialog" : undefined}
        inert={Boolean(deleteTarget) || (drawer.isOverlay && !open)}
        tabIndex={drawer.active ? -1 : undefined}
      >
        <header>
          <div><span className="eyebrow">Contexto activo</span><h2>Detalles de sesión</h2></div>
          <IconButton className="activity-close" label="Cerrar actividad" icon={<X size={20} />} onClick={() => close(false)} />
        </header>

        <section className="context-card">
          <div className="context-card__agent"><span className="agent-initial">{profile?.displayName[0] ?? "?"}</span><span><strong>{profile?.displayName ?? "Sin agente"}</strong><small>{profile?.model ?? "sin detectar"}</small></span><Badge tone={profile?.status === "ready" ? "positive" : "warning"}>{profile?.status === "ready" ? "Listo" : "No disponible"}</Badge></div>
          <dl>
            <div><dt>Workspace</dt><dd>{workspace?.name ?? "Sin workspace"}</dd></div>
            <div><dt>Gateway</dt><dd><StatusDot tone={gateway?.status === "connected" ? "positive" : "warning"} /> {gateway?.name ?? "Sin gateway"}</dd></div>
            <div><dt>Sesión</dt><dd>{session?.storedSessionId ?? "sin sesión"}</dd></div>
          </dl>
        </section>

        {demoMode ? <section className="metric-section"><div className="section-title"><span><Gauge size={18} /> Uso de contexto</span><strong>42%</strong></div><div className="meter" role="progressbar" aria-label="Uso de contexto · demo" aria-valuemin={0} aria-valuemax={100} aria-valuenow={42}><span style={{ width: "42%" }} /></div><div className="metric-grid"><span><strong>53k</strong><small>tokens usados · demo</small></span><span><strong>128k</strong><small>ventana · demo</small></span></div></section> : usage ? <section className="metric-section">
          <div className="section-title"><span><Gauge size={18} /> Uso de contexto</span><strong>{roundedContextPercent === undefined ? "—" : `${roundedContextPercent}%`}</strong></div>
          {contextPercent !== undefined ? <div
            className="meter"
            role="progressbar"
            aria-label="Uso de contexto"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={roundedContextPercent}
            aria-valuetext={`${formatUsageNumber(contextPercent)} por ciento`}
          ><span style={{ width: `${contextPercent}%` }} /></div> : <p className="muted-copy">Hermes reportó contadores, pero no la ocupación actual del contexto.</p>}
          <div className="metric-grid">
            {usage.totalTokens !== undefined ? <span><strong>{formatUsageNumber(usage.totalTokens)}</strong><small>tokens acumulados</small></span> : null}
            {inputTokens !== undefined ? <span><strong>{formatUsageNumber(inputTokens)}</strong><small>tokens de entrada</small></span> : null}
            {outputTokens !== undefined ? <span><strong>{formatUsageNumber(outputTokens)}</strong><small>tokens de salida</small></span> : null}
            {usage.contextUsed !== undefined ? <span><strong>{formatUsageNumber(usage.contextUsed)}</strong><small>contexto actual</small></span> : null}
            {usage.contextMax !== undefined ? <span><strong>{formatUsageNumber(usage.contextMax)}</strong><small>ventana de contexto</small></span> : null}
            {usage.apiCalls !== undefined ? <span><strong>{formatUsageNumber(usage.apiCalls)}</strong><small>llamadas</small></span> : null}
          </div>
        </section> : <section className="metric-section"><div className="section-title"><span><Gauge size={18} /> Uso de contexto</span><strong>—</strong></div><p className="muted-copy">Hermes no anunció telemetría de uso para esta sesión.</p></section>}

        <details className="activity-details" open>
          <summary><span><Wrench size={18} /> Herramientas</span><span><Badge tone="info">{latestTools.length}</Badge><CaretDown size={15} /></span></summary>
          <div className="activity-timeline">
            {latestTools.length ? latestTools.map((tool) => <div key={tool.id}><CheckCircle weight="fill" /><span><strong>{tool.label}</strong><small>{tool.summary}</small></span></div>) : <div><Wrench /><span><strong>Sin actividad</strong><small>Las herramientas aparecerán cuando Hermes las reporte.</small></span></div>}
          </div>
        </details>

        {demoMode ? <details className="activity-details">
          <summary><span><Robot size={18} /> Subagentes</span><span><Badge>1</Badge><CaretDown size={15} /></span></summary>
          <div className="activity-timeline"><div><Pulse /><span><strong>Investigador</strong><small>Trabajo finalizado · demo</small></span></div></div>
        </details> : null}

        <section className="recent-activity">
          <div className="section-title"><span><Clock size={18} /> Actividad reciente</span></div>
          <ol>
            <li><i /><span><strong>Transporte {connection}</strong><small>Estado observado por Agent Control</small></span></li>
            <li><i /><span><strong>{session?.runtimeSessionId ? "Runtime enlazado" : "Runtime pendiente"}</strong><small>{session?.runtimeSessionId ? "Identidad confirmada" : "Se reanudará antes del próximo comando"}</small></span></li>
          </ol>
        </section>

        {session ? <section className="session-actions" aria-labelledby="session-actions-title">
          <div className="section-title"><span id="session-actions-title"><Archive size={18} /> Acciones de sesión</span></div>
          <div className="session-actions__item">
            <span><strong>Exportar conversación</strong><small>Descarga una copia saneada del historial que conserva Hermes.</small></span>
            <Button size="sm" disabled={exportBusy || exportDisabled} leadingIcon={<DownloadSimple />} onClick={() => void exportSession()}>{exportBusy ? "Exportando…" : "Exportar"}</Button>
          </div>
          <div className="session-actions__item">
            <span><strong>Archivar en Control</strong><small>Oculta esta referencia local. La conversación permanece intacta en Hermes.</small></span>
            <Button size="sm" disabled={archiveBusy || mutationsDisabled} leadingIcon={<Archive />} onClick={() => void archiveSession()}>{archiveBusy ? "Archivando…" : "Archivar"}</Button>
          </div>
          {canDeleteFromHermes ? <div className="session-actions__item session-actions__item--danger">
            <span><strong>Eliminar de Hermes</strong><small>Borra la conversación en la infraestructura Hermes existente. Requiere escribir su ID exacto.</small></span>
            <Button size="sm" variant="danger" disabled={deleteBusy || mutationsDisabled} leadingIcon={<Trash />} onClick={openDeleteDialog}>Eliminar…</Button>
          </div> : null}
          {mutationsDisabled ? <p className="session-actions__hint">Estas acciones requieren una sesión online de Agent Control.</p> : null}
          {actionError ? <p className="form-error" role="alert"><WarningCircle /> {actionError}</p> : null}
        </section> : null}

        <div className="activity-panel__footer">
          <Switch checked={advancedMode} onChange={setAdvancedMode} label="Modo avanzado" description="IDs técnicos y diagnósticos" />
          {advancedMode ? <code>runtime {session?.runtimeSessionId ?? "sin asignar"}</code> : null}
        </div>
      </aside>
      <p className="session-action-announcement" role="status" aria-live="polite">{announcement}</p>
      {deleteTarget ? <div className="modal-layer" role="presentation">
        <button className="modal-scrim" aria-label="Cancelar eliminación de sesión" onClick={closeDeleteDialog} />
        <div
          ref={deleteDialog.containerRef}
          tabIndex={-1}
          className="hc-panel form-modal session-delete-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="session-delete-title"
          aria-describedby="session-delete-description"
        >
          <span className="eyebrow">Acción irreversible en Hermes</span>
          <h2 id="session-delete-title">Eliminar “{deleteTarget.title}”</h2>
          <p id="session-delete-description">Esto borra la conversación de Hermes, no solo su referencia en Control. Escribe el ID persistente exacto para continuar.</p>
          <form onSubmit={(event) => void deleteSession(event)}>
            <label className="hc-field" htmlFor="session-delete-confirmation">
              <span className="hc-field__label">ID persistente: <code>{deleteTarget.storedSessionId}</code></span>
              <input
                id="session-delete-confirmation"
                className="hc-input"
                autoComplete="off"
                spellCheck={false}
                value={deleteConfirmation}
                onChange={(event) => setDeleteConfirmation(event.target.value)}
                aria-invalid={Boolean(deleteConfirmation) && deleteConfirmation !== deleteTarget.storedSessionId}
              />
            </label>
            {deleteError ? <p className="form-error" role="alert"><WarningCircle /> {deleteError}</p> : null}
            <div>
              <Button type="button" variant="ghost" disabled={deleteBusy} onClick={closeDeleteDialog}>Cancelar</Button>
              <Button type="submit" variant="danger" disabled={deleteBusy || deleteConfirmation !== deleteTarget.storedSessionId} leadingIcon={<Trash />}>{deleteBusy ? "Eliminando…" : "Eliminar de Hermes"}</Button>
            </div>
          </form>
        </div>
      </div> : null}
    </>
  );
}
