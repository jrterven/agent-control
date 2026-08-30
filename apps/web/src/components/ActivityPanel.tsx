import { useMemo, useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { Archive, ArrowClockwise, CaretDown, CheckCircle, Clock, DownloadSimple, Gauge, Pulse, Robot, Trash, WarningCircle, Wrench, X } from "@phosphor-icons/react";
import { Badge, Button, IconButton, StatusDot, Switch, cx } from "@hermes-control/ui";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";
import { useOverlayDialog } from "../lib/useOverlayDialog";
import { hasPwaUpdateBlockers, requestPwaUpdate, usePwaUpdateStore } from "../lib/pwaUpdate";
import { ProfileAvatar } from "./ProfileAvatar";

export function ActivityPanel() {
  const { t, i18n } = useTranslation();
  const language = (i18n.resolvedLanguage ?? i18n.language).split("-")[0];
  const numberLocale = { es: "es-MX", en: "en-US", fr: "fr-FR", de: "de-DE", pt: "pt-BR" }[language] ?? language;
  const usageNumber = useMemo(
    () => new Intl.NumberFormat(numberLocale, { maximumFractionDigits: 3 }),
    [numberLocale],
  );
  const formatUsageNumber = (value: number) => usageNumber.format(value);
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
  const updateStatus = usePwaUpdateStore((state) => state.status);
  const updateDeferred = usePwaUpdateStore((state) => state.deferred);
  const updateBlockers = usePwaUpdateStore((state) => state.blockers);
  const removeSession = useAppStore((state) => state.removeSession);
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const setConnection = useAppStore((state) => state.setConnection);
  const workspace = workspaces.find((item) => item.id === workspaceId);
  const updateBlocked = hasPwaUpdateBlockers(updateBlockers);
  const showUpdate = updateStatus === "available" || updateStatus === "applying";
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
      setAnnouncement(t("activity.archivedAnnouncement", { title: target.title }));
    } catch (error) {
      setActionError(error instanceof Error ? error.message : t("activity.archiveError"));
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
      setAnnouncement(t("activity.exportedAnnouncement", { title: session.title }));
    } catch (error) {
      setActionError(error instanceof Error ? error.message : t("activity.exportError"));
    } finally {
      setExportBusy(false);
    }
  };

  const openDeleteDialog = () => {
    if (!session || !canDeleteFromHermes || mutationsDisabled) return;
    setDeleteTarget({ id: session.id, title: session.title, storedSessionId: session.storedSessionId });
    setDeleteError("");
    setActionError("");
  };

  const deleteSession = async (event: FormEvent) => {
    event.preventDefault();
    if (!deleteTarget || deleteBusy) return;
    const target = deleteTarget;
    setDeleteBusy(true);
    setDeleteError("");
    setAnnouncement("");
    try {
      await api.deleteSessionFromHermes(target.id, target.storedSessionId, csrfToken);
      removeSession(target.id);
      await refreshBootstrap();
      setDeleteTarget(null);
      setAnnouncement(t("activity.deletedAnnouncement", { title: target.title }));
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : t("activity.deleteError"));
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <>
      <button className={cx("scrim scrim--activity", open && "is-visible")} aria-label={t("activity.close")} onClick={() => close(false)} />
      <aside
        id="activity-panel"
        ref={drawer.containerRef}
        className={cx("activity-panel", open && "is-open")}
        aria-label={t("activity.panelAria")}
        aria-hidden={deleteTarget || (drawer.isOverlay && !open) ? true : undefined}
        aria-modal={drawer.active ? true : undefined}
        role={drawer.isOverlay ? "dialog" : undefined}
        inert={Boolean(deleteTarget) || (drawer.isOverlay && !open)}
        tabIndex={drawer.active ? -1 : undefined}
      >
        <header>
          <div><span className="eyebrow">{t("activity.activeContext")}</span><h2>{t("activity.sessionDetails")}</h2></div>
          <IconButton className="activity-close" label={t("activity.close")} icon={<X size={20} />} onClick={() => close(false)} />
        </header>

        {showUpdate ? <section className="app-update-card" aria-labelledby="app-update-title">
          <span className="app-update-card__icon"><ArrowClockwise weight="bold" /></span>
          <div>
            <strong id="app-update-title">{t(updateDeferred ? "updates.waitingTitle" : "updates.availableTitle")}</strong>
            <p>{t(updateDeferred ? "updates.waitingBody" : "updates.availableBody")}</p>
            <Button
              size="sm"
              disabled={updateStatus === "applying" || updateDeferred}
              leadingIcon={<ArrowClockwise />}
              onClick={() => void requestPwaUpdate()}
            >
              {t(updateStatus === "applying" ? "updates.applying" : updateBlocked ? "updates.updateWhenReady" : "updates.updateNow")}
            </Button>
          </div>
        </section> : null}

        <section className="context-card">
          <div className="context-card__agent"><ProfileAvatar profile={profile} size="activity" fallback="initial" /><span><strong>{profile?.displayName ?? t("activity.noAgent")}</strong><small>{profile?.model ?? t("activity.undetected")}</small></span><Badge tone={profile?.status === "ready" ? "positive" : "warning"}>{t(profile?.status === "ready" ? "activity.ready" : "activity.unavailable")}</Badge></div>
          <dl>
            <div><dt>{t("activity.workspace")}</dt><dd>{workspace?.name ?? t("activity.noWorkspace")}</dd></div>
            <div><dt>{t("activity.gateway")}</dt><dd><StatusDot tone={gateway?.status === "connected" ? "positive" : "warning"} /> {gateway?.name ?? t("activity.noGateway")}</dd></div>
            <div><dt>{t("activity.session")}</dt><dd>{session?.storedSessionId ?? t("activity.noSession")}</dd></div>
          </dl>
        </section>

        {demoMode ? <section className="metric-section"><div className="section-title"><span><Gauge size={18} /> {t("activity.usage.title")}</span><strong>42%</strong></div><div className="meter" role="progressbar" aria-label={t("activity.usage.demoAria")} aria-valuemin={0} aria-valuemax={100} aria-valuenow={42}><span style={{ width: "42%" }} /></div><div className="metric-grid"><span><strong>53k</strong><small>{t("activity.usage.tokensUsedDemo")}</small></span><span><strong>128k</strong><small>{t("activity.usage.contextWindowDemo")}</small></span></div></section> : usage ? <section className="metric-section">
          <div className="section-title"><span><Gauge size={18} /> {t("activity.usage.title")}</span><strong>{roundedContextPercent === undefined ? "—" : `${roundedContextPercent}%`}</strong></div>
          {contextPercent !== undefined ? <div
            className="meter"
            role="progressbar"
            aria-label={t("activity.usage.title")}
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={roundedContextPercent}
            aria-valuetext={t("activity.usage.percentValue", { value: formatUsageNumber(contextPercent) })}
          ><span style={{ width: `${contextPercent}%` }} /></div> : <p className="muted-copy">{t("activity.usage.countersWithoutOccupancy")}</p>}
          <div className="metric-grid">
            {usage.totalTokens !== undefined ? <span><strong>{formatUsageNumber(usage.totalTokens)}</strong><small>{t("activity.usage.totalTokens")}</small></span> : null}
            {inputTokens !== undefined ? <span><strong>{formatUsageNumber(inputTokens)}</strong><small>{t("activity.usage.inputTokens")}</small></span> : null}
            {outputTokens !== undefined ? <span><strong>{formatUsageNumber(outputTokens)}</strong><small>{t("activity.usage.outputTokens")}</small></span> : null}
            {usage.contextUsed !== undefined ? <span><strong>{formatUsageNumber(usage.contextUsed)}</strong><small>{t("activity.usage.currentContext")}</small></span> : null}
            {usage.contextMax !== undefined ? <span><strong>{formatUsageNumber(usage.contextMax)}</strong><small>{t("activity.usage.contextWindow")}</small></span> : null}
            {usage.apiCalls !== undefined ? <span><strong>{formatUsageNumber(usage.apiCalls)}</strong><small>{t("activity.usage.calls")}</small></span> : null}
          </div>
        </section> : <section className="metric-section"><div className="section-title"><span><Gauge size={18} /> {t("activity.usage.title")}</span><strong>—</strong></div><p className="muted-copy">{t("activity.usage.noTelemetry")}</p></section>}

        <details className="activity-details" open>
          <summary><span><Wrench size={18} /> {t("activity.tools")}</span><span><Badge tone="info">{latestTools.length}</Badge><CaretDown size={15} /></span></summary>
          <div className="activity-timeline">
            {latestTools.length ? latestTools.map((tool) => <div key={tool.id}><CheckCircle weight="fill" /><span><strong>{tool.label}</strong><small>{tool.summary}</small></span></div>) : <div><Wrench /><span><strong>{t("activity.noActivity")}</strong><small>{t("activity.toolsWhenReported")}</small></span></div>}
          </div>
        </details>

        {demoMode ? <details className="activity-details">
          <summary><span><Robot size={18} /> {t("activity.subagents")}</span><span><Badge>1</Badge><CaretDown size={15} /></span></summary>
          <div className="activity-timeline"><div><Pulse /><span><strong>{t("activity.researcher")}</strong><small>{t("activity.workFinishedDemo")}</small></span></div></div>
        </details> : null}

        <section className="recent-activity">
          <div className="section-title"><span><Clock size={18} /> {t("activity.recentActivity")}</span></div>
          <ol>
            <li><i /><span><strong>{t("activity.transport", { status: t(`activity.connection.${connection}`) })}</strong><small>{t("activity.stateObserved")}</small></span></li>
            <li><i /><span><strong>{t(session?.runtimeSessionId ? "activity.runtimeLinked" : "activity.runtimePending")}</strong><small>{t(session?.runtimeSessionId ? "activity.identityConfirmed" : "activity.resumesBeforeCommand")}</small></span></li>
          </ol>
        </section>

        {session ? <section className="session-actions" aria-labelledby="session-actions-title">
          <div className="section-title"><span id="session-actions-title"><Archive size={18} /> {t("activity.sessionActions")}</span></div>
          <div className="session-actions__item">
            <span><strong>{t("activity.exportConversation")}</strong><small>{t("activity.exportDescription")}</small></span>
            <Button size="sm" disabled={exportBusy || exportDisabled} leadingIcon={<DownloadSimple />} onClick={() => void exportSession()}>{t(exportBusy ? "activity.exporting" : "activity.export")}</Button>
          </div>
          <div className="session-actions__item">
            <span><strong>{t("activity.archiveInControl")}</strong><small>{t("activity.archiveDescription")}</small></span>
            <Button size="sm" disabled={archiveBusy || mutationsDisabled} leadingIcon={<Archive />} onClick={() => void archiveSession()}>{t(archiveBusy ? "activity.archiving" : "activity.archive")}</Button>
          </div>
          {canDeleteFromHermes ? <div className="session-actions__item session-actions__item--danger">
            <span><strong>{t("activity.deleteFromHermes")}</strong><small>{t("activity.deleteDescription")}</small></span>
            <Button size="sm" variant="danger" disabled={deleteBusy || mutationsDisabled} leadingIcon={<Trash />} onClick={openDeleteDialog}>{t("activity.deleteEllipsis")}</Button>
          </div> : null}
          {mutationsDisabled ? <p className="session-actions__hint">{t("activity.actionsRequireOnline")}</p> : null}
          {actionError ? <p className="form-error" role="alert"><WarningCircle /> {actionError}</p> : null}
        </section> : null}

        <div className="activity-panel__footer">
          <Switch checked={advancedMode} onChange={setAdvancedMode} label={t("activity.advancedMode")} description={t("activity.technicalIds")} />
          {advancedMode ? <code>runtime {session?.runtimeSessionId ?? t("activity.unassigned")}</code> : null}
        </div>
      </aside>
      <p className="session-action-announcement" role="status" aria-live="polite">{announcement}</p>
      {deleteTarget ? <div className="modal-layer" role="presentation">
        <button className="modal-scrim" aria-label={t("activity.cancelDeleteAria")} onClick={closeDeleteDialog} />
        <div
          ref={deleteDialog.containerRef}
          tabIndex={-1}
          className="hc-panel form-modal session-delete-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="session-delete-title"
          aria-describedby="session-delete-description"
        >
          <span className="eyebrow">{t("activity.irreversible")}</span>
          <h2 id="session-delete-title">{t("activity.deleteTitle", { title: deleteTarget.title })}</h2>
          <p id="session-delete-description">{t("activity.deleteDialogDescription")}</p>
          <form onSubmit={(event) => void deleteSession(event)}>
            {deleteError ? <p className="form-error" role="alert"><WarningCircle /> {deleteError}</p> : null}
            <div>
              <Button type="button" variant="ghost" disabled={deleteBusy} onClick={closeDeleteDialog}>{t("activity.cancel")}</Button>
              <Button type="submit" variant="danger" disabled={deleteBusy} leadingIcon={<Trash />}>{t(deleteBusy ? "activity.deleting" : "activity.deleteFromHermes")}</Button>
            </div>
          </form>
        </div>
      </div> : null}
    </>
  );
}
