import { useEffect, useRef, useState, type FormEvent } from "react";
import { Archive, CaretDown, ChatTeardropText, DotsThree, DotsThreeVertical, FolderSimple, GearSix, Lightning, MagnifyingGlass, PencilSimple, Plus, Robot, Trash, WarningCircle, X } from "@phosphor-icons/react";
import { Link, useRouterState } from "@tanstack/react-router";
import { Badge, Button, IconButton, StatusDot, cx } from "@hermes-control/ui";
import { useTranslation } from "react-i18next";
import { api } from "../lib/api";
import { createChatForCurrentContext } from "../hooks";
import { useOverlayDialog } from "../lib/useOverlayDialog";
import { useAppStore } from "../store/appStore";
import { BrandMark } from "./BrandMark";
import { ProfileAvatar } from "./ProfileAvatar";
import type { SessionSummary, Workspace } from "../types";

export function LeftSidebar() {
  const { t } = useTranslation();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const open = useAppStore((state) => state.leftDrawerOpen);
  const close = useAppStore((state) => state.setLeftDrawerOpen);
  const selectedGatewayId = useAppStore((state) => state.selectedGatewayId);
  const selectedProfileId = useAppStore((state) => state.selectedProfileId);
  const selectedWorkspaceId = useAppStore((state) => state.selectedWorkspaceId);
  const selectedSessionId = useAppStore((state) => state.selectedSessionId);
  const selectProfile = useAppStore((state) => state.selectProfile);
  const selectWorkspace = useAppStore((state) => state.selectWorkspace);
  const selectSession = useAppStore((state) => state.selectSession);
  const setGatewayMenuOpen = useAppStore((state) => state.setGatewayMenuOpen);
  const gatewayMenuOpen = useAppStore((state) => state.gatewayMenuOpen);
  const setCommandOpen = useAppStore((state) => state.setCommandOpen);
  const gateways = useAppStore((state) => state.gateways);
  const profiles = useAppStore((state) => state.profiles);
  const workspaces = useAppStore((state) => state.workspaces);
  const sessions = useAppStore((state) => state.sessions);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const demoMode = useAppStore((state) => state.demoMode);
  const authState = useAppStore((state) => state.authState);
  const unassignedSessions = sessions.filter((session) => !session.workspaceId);
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const updateSession = useAppStore((state) => state.updateSession);
  const removeSession = useAppStore((state) => state.removeSession);
  const [workspaceEditorOpen, setWorkspaceEditorOpen] = useState(false);
  const [editingWorkspace, setEditingWorkspace] = useState<Workspace | null>(null);
  const [workspaceName, setWorkspaceName] = useState("");
  const [workspaceDescription, setWorkspaceDescription] = useState("");
  const [workspaceError, setWorkspaceError] = useState("");
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const [sessionMenuId, setSessionMenuId] = useState("");
  const [renameTarget, setRenameTarget] = useState<SessionSummary | null>(null);
  const [renameTitle, setRenameTitle] = useState("");
  const [renameBusy, setRenameBusy] = useState(false);
  const [renameError, setRenameError] = useState("");
  const [moveTarget, setMoveTarget] = useState<SessionSummary | null>(null);
  const [moveWorkspaceId, setMoveWorkspaceId] = useState("");
  const [moveBusy, setMoveBusy] = useState(false);
  const [moveError, setMoveError] = useState("");
  const [deleteTarget, setDeleteTarget] = useState<SessionSummary | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [sessionAnnouncement, setSessionAnnouncement] = useState("");
  const sessionMenuRef = useRef<HTMLDivElement>(null);
  const sessionMenuTriggerRef = useRef<HTMLButtonElement | null>(null);
  const gateway = gateways.find((item) => item.id === selectedGatewayId) ?? gateways[0];
  const selectedProfile = profiles.find((item) => item.id === selectedProfileId);
  const canCreateSession = demoMode || (
    authState === "authenticated"
    && selectedProfile?.mutable === true
    && Boolean(selectedProfile.capabilities?.sessions)
  );
  const sessionDialogOpen = Boolean(renameTarget || moveTarget || deleteTarget);
  const mutationsDisabled = authState !== "authenticated" || demoMode;
  const drawer = useOverlayDialog<HTMLElement>({ open: open && !sessionDialogOpen, onClose: () => close(false), mediaQuery: "(max-width: 779px)" });
  const closeWorkspaceEditor = () => {
    if (workspaceBusy) return;
    setWorkspaceEditorOpen(false);
    setEditingWorkspace(null);
    setWorkspaceError("");
  };
  const workspaceDialog = useOverlayDialog<HTMLDivElement>({ open: workspaceEditorOpen, onClose: closeWorkspaceEditor, mediaQuery: "(min-width: 0px)" });
  const closeRenameDialog = () => {
    if (renameBusy) return;
    setRenameTarget(null);
    setRenameTitle("");
    setRenameError("");
  };
  const closeDeleteDialog = () => {
    if (deleteBusy) return;
    setDeleteTarget(null);
    setDeleteError("");
  };
  const closeMoveDialog = () => {
    if (moveBusy) return;
    setMoveTarget(null);
    setMoveWorkspaceId("");
    setMoveError("");
  };
  const renameDialog = useOverlayDialog<HTMLDivElement>({ open: Boolean(renameTarget), onClose: closeRenameDialog, mediaQuery: "(min-width: 0px)" });
  const moveDialog = useOverlayDialog<HTMLDivElement>({ open: Boolean(moveTarget), onClose: closeMoveDialog, mediaQuery: "(min-width: 0px)" });
  const deleteDialog = useOverlayDialog<HTMLDivElement>({ open: Boolean(deleteTarget), onClose: closeDeleteDialog, mediaQuery: "(min-width: 0px)" });

  useEffect(() => {
    if (!sessionMenuId) return;
    const firstAction = sessionMenuRef.current?.querySelector<HTMLButtonElement>("[role='menuitem']");
    firstAction?.focus();
    const closeOnOutsidePointer = (event: PointerEvent) => {
      const target = event.target as Node;
      if (!sessionMenuRef.current?.contains(target) && !sessionMenuTriggerRef.current?.contains(target)) setSessionMenuId("");
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    return () => document.removeEventListener("pointerdown", closeOnOutsidePointer);
  }, [sessionMenuId]);

  const closeSessionMenu = (restoreFocus = false) => {
    setSessionMenuId("");
    if (restoreFocus) requestAnimationFrame(() => sessionMenuTriggerRef.current?.focus());
  };

  const openRenameDialog = (session: SessionSummary) => {
    closeSessionMenu();
    setRenameTarget(session);
    setRenameTitle(session.title);
    setRenameError("");
    setSessionAnnouncement("");
  };

  const saveSessionTitle = async (event: FormEvent) => {
    event.preventDefault();
    const title = renameTitle.trim();
    if (!renameTarget || !title || renameBusy || mutationsDisabled) return;
    const target = renameTarget;
    setRenameBusy(true);
    setRenameError("");
    try {
      const renamed = await api.renameSession(target.id, title, csrfToken);
      updateSession(target.id, { title: renamed.title, updatedAt: renamed.updatedAt });
      setRenameTarget(null);
      setRenameTitle("");
      setSessionAnnouncement(t("sidebar.renamedAnnouncement", { title: renamed.title }));
    } catch (error) {
      setRenameError(error instanceof Error ? error.message : t("sidebar.renameError"));
    } finally {
      setRenameBusy(false);
    }
  };

  const openMoveDialog = (session: SessionSummary) => {
    closeSessionMenu();
    setMoveTarget(session);
    setMoveWorkspaceId(session.workspaceId ?? "");
    setMoveError("");
    setSessionAnnouncement("");
  };

  const moveSession = async (event: FormEvent) => {
    event.preventDefault();
    if (!moveTarget || moveBusy || mutationsDisabled || moveWorkspaceId === (moveTarget.workspaceId ?? "")) return;
    const target = moveTarget;
    const destinationName = workspaces.find((workspace) => workspace.id === moveWorkspaceId)?.name ?? t("sidebar.noWorkspace");
    setMoveBusy(true);
    setMoveError("");
    try {
      const moved = await api.moveSession(target.id, moveWorkspaceId || null, csrfToken);
      updateSession(target.id, { workspaceId: moved.workspaceId, updatedAt: moved.updatedAt });
      setMoveTarget(null);
      setMoveWorkspaceId("");
      setSessionAnnouncement(t("sidebar.movedAnnouncement", { title: target.title, workspace: destinationName }));
    } catch (error) {
      setMoveError(error instanceof Error ? error.message : t("sidebar.moveError"));
    } finally {
      setMoveBusy(false);
    }
  };

  const canDeleteSession = (session: SessionSummary) => {
    const profile = profiles.find((item) => item.id === session.profileId);
    return Boolean(profile?.mutable && profile.capabilitySet?.methods.includes("session.delete"));
  };

  const openDeleteDialog = (session: SessionSummary) => {
    if (!canDeleteSession(session) || mutationsDisabled) return;
    closeSessionMenu();
    setDeleteTarget(session);
    setDeleteError("");
    setSessionAnnouncement("");
  };

  const deleteSession = async (event: FormEvent) => {
    event.preventDefault();
    if (!deleteTarget || deleteBusy) return;
    const target = deleteTarget;
    setDeleteBusy(true);
    setDeleteError("");
    try {
      await api.deleteSessionFromHermes(target.id, target.storedSessionId, csrfToken);
      removeSession(target.id);
      setDeleteTarget(null);
      setSessionAnnouncement(t("activity.deletedAnnouncement", { title: target.title }));
    } catch (error) {
      setDeleteError(error instanceof Error ? error.message : t("activity.deleteError"));
    } finally {
      setDeleteBusy(false);
    }
  };
  const openWorkspaceEditor = (workspace?: Workspace) => {
    setEditingWorkspace(workspace ?? null);
    setWorkspaceName(workspace?.name ?? "");
    setWorkspaceDescription(workspace?.description ?? "");
    setWorkspaceError("");
    setWorkspaceEditorOpen(true);
  };
  const refreshWorkspaces = async (selectedId?: string) => {
    const next = await api.bootstrap();
    hydrateBootstrap(next);
    if (selectedId && next.workspaces.some((workspace) => workspace.id === selectedId)) selectWorkspace(selectedId);
  };
  const saveWorkspace = async (event: FormEvent) => {
    event.preventDefault();
    const name = workspaceName.trim();
    if (!name || workspaceBusy || demoMode || authState !== "authenticated") return;
    setWorkspaceBusy(true);
    setWorkspaceError("");
    try {
      const workspace = editingWorkspace
        ? await api.updateWorkspace(editingWorkspace.id, { name, description: workspaceDescription.trim() }, csrfToken)
        : await api.createWorkspace({ name, description: workspaceDescription.trim() || undefined }, csrfToken);
      await refreshWorkspaces(workspace.id);
      setWorkspaceEditorOpen(false);
      setEditingWorkspace(null);
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : t("sidebar.saveWorkspaceError"));
    } finally {
      setWorkspaceBusy(false);
    }
  };
  const archiveWorkspace = async () => {
    if (!editingWorkspace || workspaceBusy || !window.confirm(t("sidebar.archiveWorkspaceConfirm", { name: editingWorkspace.name }))) return;
    setWorkspaceBusy(true);
    setWorkspaceError("");
    try {
      await api.updateWorkspace(editingWorkspace.id, { archived: true }, csrfToken);
      await refreshWorkspaces();
      setWorkspaceEditorOpen(false);
      setEditingWorkspace(null);
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : t("sidebar.archiveWorkspaceError"));
    } finally {
      setWorkspaceBusy(false);
    }
  };
  return (
    <>
      <button className={cx("scrim scrim--left", open && "is-visible")} aria-label={t("nav.closeNavigation")} onClick={() => close(false)} />
      <aside
        id="left-sidebar"
        ref={drawer.containerRef}
        className={cx("left-sidebar", open && "is-open")}
        aria-label={t("sidebar.navigationLabel")}
        aria-hidden={sessionDialogOpen || (drawer.isOverlay && !open) ? true : undefined}
        aria-modal={drawer.active ? true : undefined}
        role={drawer.isOverlay ? "dialog" : undefined}
        inert={sessionDialogOpen || (drawer.isOverlay && !open)}
        tabIndex={drawer.active ? -1 : undefined}
      >
        <div className="sidebar-brand">
          <BrandMark size="md" />
          <span><strong>Agent</strong><small>Control</small></span>
          <IconButton className="sidebar-close" label={t("nav.closeNavigation")} icon={<X size={20} />} onClick={() => close(false)} />
        </div>

        <button className="gateway-select" type="button" aria-expanded={gatewayMenuOpen} onClick={() => setGatewayMenuOpen(!gatewayMenuOpen)}>
          <span className="gateway-select__main"><StatusDot tone={gateway?.status === "connected" ? "positive" : "warning"} /><span><strong>{gateway?.name ?? t("sidebar.noGateway")}</strong><small>{gateway?.location ?? t("sidebar.configureConnection")}</small></span></span>
          <CaretDown size={16} />
        </button>
        {gatewayMenuOpen ? (
          <div className="gateway-popover">
            {gateways.map((item) => <button type="button" key={item.id} onClick={() => useAppStore.getState().selectGateway(item.id)}><StatusDot tone={item.status === "connected" ? "positive" : "warning"} /><span><strong>{item.name}</strong><small>{item.latencyMs} ms · {item.version}</small></span></button>)}
            <Link to="/gateways"><Plus size={16} /> {t("sidebar.manageGateways")}</Link>
          </div>
        ) : null}

        <button className="command-trigger" type="button" onClick={() => setCommandOpen(true)}>
          <MagnifyingGlass size={17} /><span>{t("sidebar.searchAll")}</span><kbd>⌘ K</kbd>
        </button>

        <nav className="sidebar-main-nav" aria-label={t("sidebar.mainSections")}>
          <Link to="/chats" className={pathname === "/chats" ? "is-active" : ""}><ChatTeardropText /><span>{t("nav.chats")}</span></Link>
          <Link to="/agents" className={pathname === "/agents" ? "is-active" : ""}><Robot /><span>{t("nav.agents")}</span></Link>
          <Link to="/automations" className={pathname === "/automations" ? "is-active" : ""}><Lightning /><span>{t("nav.automationsShort")}</span></Link>
          <Link to="/more" className={pathname === "/more" ? "is-active" : ""}><DotsThree /><span>{t("nav.more")}</span></Link>
        </nav>

        <nav className="profile-strip" aria-label={t("sidebar.agentProfiles")}>
          {profiles.filter((profile) => profile.gatewayId === selectedGatewayId).map((profile) => (
            <button key={profile.id} type="button" className={cx(profile.id === selectedProfileId && "is-active")} onClick={() => selectProfile(profile.id)}>
              <ProfileAvatar profile={profile} size="compact" fallback="initial" />
              {profile.displayName}
            </button>
          ))}
        </nav>

        <div className="sidebar-section">
          <div className="sidebar-section__heading"><span>{t("sidebar.workspaces")}</span>{authState === "authenticated" && !demoMode ? <IconButton label={t("sidebar.createWorkspace")} icon={<Plus size={16} />} onClick={() => openWorkspaceEditor()} /> : null}</div>
          <div className="workspace-list">
            {workspaces.map((workspace) => (
              <div className="workspace-list__row" key={workspace.id}>
                <button type="button" className={cx("workspace-list__select", workspace.id === selectedWorkspaceId && "is-active")} onClick={() => selectWorkspace(workspace.id)}>
                  <span>{workspace.name}</span><Badge>{workspace.sessionCount}</Badge>
                </button>
                {workspace.id === selectedWorkspaceId && authState === "authenticated" && !demoMode ? <IconButton className="workspace-list__edit" label={t("sidebar.editWorkspace", { name: workspace.name })} icon={<PencilSimple size={15} />} onClick={() => openWorkspaceEditor(workspace)} /> : null}
              </div>
            ))}
            {unassignedSessions.length ? <div className="workspace-list__row"><button type="button" className={cx("workspace-list__select", !selectedWorkspaceId && "is-active")} onClick={() => selectWorkspace("")}><span>{t("sidebar.noWorkspace")}</span><Badge>{unassignedSessions.length}</Badge></button></div> : null}
            {!workspaces.length ? <button type="button" className="workspace-empty-action" disabled={authState !== "authenticated" || demoMode} onClick={() => openWorkspaceEditor()}><Plus size={17} /> {t("sidebar.createFirstWorkspace")}</button> : null}
          </div>
        </div>

        <div className="sidebar-section sidebar-section--sessions">
          <div className="sidebar-section__heading"><span>{t("sidebar.conversations")}</span></div>
          <div className="session-list">
            {sessions.filter((session) => (session.workspaceId ?? "") === selectedWorkspaceId && session.profileId === selectedProfileId).map((session) => (
              <div className={cx("session-list__row", session.id === selectedSessionId && "is-active")} key={session.id}>
                <button type="button" className="session-list__select" onClick={() => selectSession(session.id)}>
                  <span className="session-list__body"><strong>{session.title}</strong><small>{session.preview}</small></span>
                  <span className="session-list__meta">{session.unread ? <i /> : null}{session.updatedAt}</span>
                </button>
                {!mutationsDisabled ? <IconButton
                  className="session-list__more"
                  label={t("sidebar.conversationMenu", { title: session.title })}
                  icon={<DotsThreeVertical size={20} weight="bold" />}
                  aria-haspopup="menu"
                  aria-expanded={sessionMenuId === session.id}
                  onClick={(event) => {
                    sessionMenuTriggerRef.current = event.currentTarget;
                    setSessionMenuId((current) => current === session.id ? "" : session.id);
                  }}
                /> : null}
                {sessionMenuId === session.id ? <div
                  ref={sessionMenuRef}
                  className="session-list__menu"
                  role="menu"
                  aria-label={t("sidebar.conversationMenu", { title: session.title })}
                  onKeyDown={(event) => {
                    if (event.key === "Escape") {
                      event.preventDefault();
                      event.stopPropagation();
                      closeSessionMenu(true);
                    }
                  }}
                >
                  <button type="button" role="menuitem" onClick={() => openRenameDialog(session)}><PencilSimple size={18} /> {t("sidebar.renameConversation")}</button>
                  <button type="button" role="menuitem" onClick={() => openMoveDialog(session)}><FolderSimple size={18} /> {t("sidebar.moveConversation")}</button>
                  {canDeleteSession(session) ? <button type="button" role="menuitem" className="is-danger" onClick={() => openDeleteDialog(session)}><Trash size={18} /> {t("activity.deleteEllipsis")}</button> : null}
                </div> : null}
              </div>
            ))}
          </div>
        </div>

        <div className="sidebar-footer">
          {canCreateSession ? <Button variant="primary" leadingIcon={<Plus size={18} />} onClick={() => void createChatForCurrentContext().catch(() => undefined)}>{t("sidebar.newChat")}</Button> : null}
          <div>
            <Link to="/chats" className={pathname === "/chats" ? "is-active" : ""}><Archive size={19} /> {t("nav.chats")}</Link>
            <Link to="/settings" className={pathname === "/settings" ? "is-active" : ""}><GearSix size={19} /> {t("nav.settings")}</Link>
          </div>
        </div>
      </aside>
      <p className="session-action-announcement" role="status" aria-live="polite">{sessionAnnouncement}</p>
      {workspaceEditorOpen ? <div ref={workspaceDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="workspace-editor-title"><button className="modal-scrim" aria-label={t("sidebar.closeWorkspaceEditor")} onClick={closeWorkspaceEditor} /><div className="hc-panel form-modal workspace-editor"><span className="eyebrow">{t("sidebar.localOrganization")}</span><h2 id="workspace-editor-title">{editingWorkspace ? t("sidebar.editWorkspaceTitle") : t("sidebar.newWorkspaceTitle")}</h2><p>{t("sidebar.workspaceExplanation")}</p><form onSubmit={(event) => void saveWorkspace(event)}><label className="hc-field"><span>{t("sidebar.name")}</span><input autoFocus maxLength={200} value={workspaceName} onChange={(event) => setWorkspaceName(event.target.value)} /></label><label className="hc-field"><span>{t("sidebar.description")}</span><textarea rows={3} maxLength={4000} value={workspaceDescription} onChange={(event) => setWorkspaceDescription(event.target.value)} /></label>{workspaceError ? <p className="form-error" role="alert">{workspaceError}</p> : null}<div>{editingWorkspace ? <Button type="button" variant="danger" disabled={workspaceBusy} leadingIcon={<Archive />} onClick={() => void archiveWorkspace()}>{t("sidebar.archive")}</Button> : <span />}<span><Button type="button" variant="ghost" disabled={workspaceBusy} onClick={closeWorkspaceEditor}>{t("sidebar.cancel")}</Button><Button type="submit" variant="primary" disabled={workspaceBusy || !workspaceName.trim()}>{workspaceBusy ? t("sidebar.saving") : t("sidebar.save")}</Button></span></div></form></div></div> : null}
      {renameTarget ? <div className="modal-layer" role="presentation"><button className="modal-scrim" aria-label={t("sidebar.closeRenameEditor")} onClick={closeRenameDialog} /><div ref={renameDialog.containerRef} tabIndex={-1} className="hc-panel form-modal session-rename-dialog" role="dialog" aria-modal="true" aria-labelledby="session-rename-title" aria-describedby="session-rename-description"><span className="eyebrow">{t("sidebar.localOrganization")}</span><h2 id="session-rename-title">{t("sidebar.renameTitle")}</h2><p id="session-rename-description">{t("sidebar.renameDescription")}</p><form onSubmit={(event) => void saveSessionTitle(event)}><label className="hc-field"><span>{t("sidebar.conversationName")}</span><input autoFocus maxLength={300} value={renameTitle} onChange={(event) => setRenameTitle(event.target.value)} /></label>{renameError ? <p className="form-error" role="alert"><WarningCircle /> {renameError}</p> : null}<div><Button type="button" variant="ghost" disabled={renameBusy} onClick={closeRenameDialog}>{t("sidebar.cancel")}</Button><Button type="submit" variant="primary" disabled={renameBusy || !renameTitle.trim()}>{t(renameBusy ? "sidebar.renaming" : "sidebar.rename")}</Button></div></form></div></div> : null}
      {moveTarget ? <div className="modal-layer" role="presentation"><button className="modal-scrim" aria-label={t("sidebar.closeMoveEditor")} onClick={closeMoveDialog} /><div ref={moveDialog.containerRef} tabIndex={-1} className="hc-panel form-modal session-move-dialog" role="dialog" aria-modal="true" aria-labelledby="session-move-title" aria-describedby="session-move-description"><span className="eyebrow">{t("sidebar.localOrganization")}</span><h2 id="session-move-title">{t("sidebar.moveTitle")}</h2><p id="session-move-description">{t("sidebar.moveDescription", { title: moveTarget.title })}</p><form onSubmit={(event) => void moveSession(event)}><label className="hc-field"><span>{t("sidebar.workspaceDestination")}</span><select autoFocus value={moveWorkspaceId} onChange={(event) => setMoveWorkspaceId(event.target.value)}><option value="">{t("sidebar.noWorkspace")}</option>{workspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}</select></label>{moveError ? <p className="form-error" role="alert"><WarningCircle /> {moveError}</p> : null}<div><Button type="button" variant="ghost" disabled={moveBusy} onClick={closeMoveDialog}>{t("sidebar.cancel")}</Button><Button type="submit" variant="primary" disabled={moveBusy || moveWorkspaceId === (moveTarget.workspaceId ?? "")} leadingIcon={<FolderSimple />}>{t(moveBusy ? "sidebar.moving" : "sidebar.move")}</Button></div></form></div></div> : null}
      {deleteTarget ? <div className="modal-layer" role="presentation"><button className="modal-scrim" aria-label={t("activity.cancelDeleteAria")} onClick={closeDeleteDialog} /><div ref={deleteDialog.containerRef} tabIndex={-1} className="hc-panel form-modal session-delete-dialog" role="dialog" aria-modal="true" aria-labelledby="sidebar-session-delete-title" aria-describedby="sidebar-session-delete-description"><span className="eyebrow">{t("activity.irreversible")}</span><h2 id="sidebar-session-delete-title">{t("activity.deleteTitle", { title: deleteTarget.title })}</h2><p id="sidebar-session-delete-description">{t("activity.deleteDialogDescription")}</p><form onSubmit={(event) => void deleteSession(event)}>{deleteError ? <p className="form-error" role="alert"><WarningCircle /> {deleteError}</p> : null}<div><Button type="button" variant="ghost" disabled={deleteBusy} onClick={closeDeleteDialog}>{t("activity.cancel")}</Button><Button type="submit" variant="danger" disabled={deleteBusy} leadingIcon={<Trash />}>{t(deleteBusy ? "activity.deleting" : "activity.deleteFromHermes")}</Button></div></form></div></div> : null}
    </>
  );
}
