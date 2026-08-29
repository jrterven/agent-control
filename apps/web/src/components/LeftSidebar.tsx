import { useState, type FormEvent } from "react";
import { Archive, CaretDown, ChatTeardropText, DotsThree, GearSix, Lightning, MagnifyingGlass, PencilSimple, Plus, Robot, X } from "@phosphor-icons/react";
import { Link, useRouterState } from "@tanstack/react-router";
import { Badge, Button, IconButton, StatusDot, cx } from "@hermes-control/ui";
import { api } from "../lib/api";
import { useOverlayDialog } from "../lib/useOverlayDialog";
import { useAppStore } from "../store/appStore";
import { BrandMark } from "./BrandMark";
import type { Workspace } from "../types";

export function LeftSidebar() {
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
  const addSession = useAppStore((state) => state.addSession);
  const unassignedSessions = sessions.filter((session) => !session.workspaceId);
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const [workspaceEditorOpen, setWorkspaceEditorOpen] = useState(false);
  const [editingWorkspace, setEditingWorkspace] = useState<Workspace | null>(null);
  const [workspaceName, setWorkspaceName] = useState("");
  const [workspaceDescription, setWorkspaceDescription] = useState("");
  const [workspaceError, setWorkspaceError] = useState("");
  const [workspaceBusy, setWorkspaceBusy] = useState(false);
  const gateway = gateways.find((item) => item.id === selectedGatewayId) ?? gateways[0];
  const selectedProfile = profiles.find((item) => item.id === selectedProfileId);
  const canCreateSession = demoMode || (
    authState === "authenticated"
    && selectedProfile?.mutable === true
    && Boolean(selectedProfile.capabilities?.sessions)
  );
  const drawer = useOverlayDialog<HTMLElement>({ open, onClose: () => close(false), mediaQuery: "(max-width: 779px)" });
  const closeWorkspaceEditor = () => {
    if (workspaceBusy) return;
    setWorkspaceEditorOpen(false);
    setEditingWorkspace(null);
    setWorkspaceError("");
  };
  const workspaceDialog = useOverlayDialog<HTMLDivElement>({ open: workspaceEditorOpen, onClose: closeWorkspaceEditor, mediaQuery: "(min-width: 0px)" });
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
      setWorkspaceError(error instanceof Error ? error.message : "No se pudo guardar el workspace.");
    } finally {
      setWorkspaceBusy(false);
    }
  };
  const archiveWorkspace = async () => {
    if (!editingWorkspace || workspaceBusy || !window.confirm(`Archivar “${editingWorkspace.name}”? Sus conversaciones seguirán en Hermes.`)) return;
    setWorkspaceBusy(true);
    setWorkspaceError("");
    try {
      await api.updateWorkspace(editingWorkspace.id, { archived: true }, csrfToken);
      await refreshWorkspaces();
      setWorkspaceEditorOpen(false);
      setEditingWorkspace(null);
    } catch (error) {
      setWorkspaceError(error instanceof Error ? error.message : "No se pudo archivar el workspace.");
    } finally {
      setWorkspaceBusy(false);
    }
  };
  const createChat = async () => {
    if (demoMode) {
      const session = sessions.find((item) => item.profileId === selectedProfileId) ?? sessions[0];
      if (session) selectSession(session.id);
      return;
    }
    if (!selectedProfileId || !canCreateSession) return;
    try {
      addSession(await api.createSession(selectedProfileId, selectedWorkspaceId || undefined, csrfToken));
    } catch {
      useAppStore.getState().setConnection("degraded");
    }
  };

  return (
    <>
      <button className={cx("scrim scrim--left", open && "is-visible")} aria-label="Cerrar navegación" onClick={() => close(false)} />
      <aside
        id="left-sidebar"
        ref={drawer.containerRef}
        className={cx("left-sidebar", open && "is-open")}
        aria-label="Navegación de Agent Control"
        aria-hidden={drawer.isOverlay && !open ? true : undefined}
        aria-modal={drawer.active ? true : undefined}
        role={drawer.isOverlay ? "dialog" : undefined}
        inert={drawer.isOverlay && !open}
        tabIndex={drawer.active ? -1 : undefined}
      >
        <div className="sidebar-brand">
          <BrandMark size="md" />
          <span><strong>Agent</strong><small>Control</small></span>
          <IconButton className="sidebar-close" label="Cerrar navegación" icon={<X size={20} />} onClick={() => close(false)} />
        </div>

        <button className="gateway-select" type="button" aria-expanded={gatewayMenuOpen} onClick={() => setGatewayMenuOpen(!gatewayMenuOpen)}>
          <span className="gateway-select__main"><StatusDot tone={gateway?.status === "connected" ? "positive" : "warning"} /><span><strong>{gateway?.name ?? "Sin gateway"}</strong><small>{gateway?.location ?? "Configura una conexión"}</small></span></span>
          <CaretDown size={16} />
        </button>
        {gatewayMenuOpen ? (
          <div className="gateway-popover">
            {gateways.map((item) => <button type="button" key={item.id} onClick={() => useAppStore.getState().selectGateway(item.id)}><StatusDot tone={item.status === "connected" ? "positive" : "warning"} /><span><strong>{item.name}</strong><small>{item.latencyMs} ms · {item.version}</small></span></button>)}
            <Link to="/gateways"><Plus size={16} /> Gestionar gateways</Link>
          </div>
        ) : null}

        <button className="command-trigger" type="button" onClick={() => setCommandOpen(true)}>
          <MagnifyingGlass size={17} /><span>Buscar en todo</span><kbd>⌘ K</kbd>
        </button>

        <nav className="sidebar-main-nav" aria-label="Secciones principales">
          <Link to="/chats" className={pathname === "/chats" ? "is-active" : ""}><ChatTeardropText /><span>Chats</span></Link>
          <Link to="/agents" className={pathname === "/agents" ? "is-active" : ""}><Robot /><span>Agentes</span></Link>
          <Link to="/automations" className={pathname === "/automations" ? "is-active" : ""}><Lightning /><span>Automat.</span></Link>
          <Link to="/more" className={pathname === "/more" ? "is-active" : ""}><DotsThree /><span>Más</span></Link>
        </nav>

        <nav className="profile-strip" aria-label="Agentes">
          {profiles.filter((profile) => profile.gatewayId === selectedGatewayId).map((profile) => (
            <button key={profile.id} type="button" className={cx(profile.id === selectedProfileId && "is-active")} onClick={() => selectProfile(profile.id)}>
              <span>{profile.displayName.slice(0, 1)}</span>
              {profile.displayName}
            </button>
          ))}
        </nav>

        <div className="sidebar-section">
          <div className="sidebar-section__heading"><span>Workspaces</span>{authState === "authenticated" && !demoMode ? <IconButton label="Crear workspace" icon={<Plus size={16} />} onClick={() => openWorkspaceEditor()} /> : null}</div>
          <div className="workspace-list">
            {workspaces.map((workspace) => (
              <div className="workspace-list__row" key={workspace.id}>
                <button type="button" className={cx("workspace-list__select", workspace.id === selectedWorkspaceId && "is-active")} onClick={() => selectWorkspace(workspace.id)}>
                  <span>{workspace.name}</span><Badge>{workspace.sessionCount}</Badge>
                </button>
                {workspace.id === selectedWorkspaceId && authState === "authenticated" && !demoMode ? <IconButton className="workspace-list__edit" label={`Editar ${workspace.name}`} icon={<PencilSimple size={15} />} onClick={() => openWorkspaceEditor(workspace)} /> : null}
              </div>
            ))}
            {unassignedSessions.length ? <div className="workspace-list__row"><button type="button" className={cx("workspace-list__select", !selectedWorkspaceId && "is-active")} onClick={() => selectWorkspace("")}><span>Sin workspace</span><Badge>{unassignedSessions.length}</Badge></button></div> : null}
            {!workspaces.length ? <button type="button" className="workspace-empty-action" disabled={authState !== "authenticated" || demoMode} onClick={() => openWorkspaceEditor()}><Plus size={17} /> Crea tu primer workspace</button> : null}
          </div>
        </div>

        <div className="sidebar-section sidebar-section--sessions">
          <div className="sidebar-section__heading"><span>Conversaciones</span></div>
          <div className="session-list">
            {sessions.filter((session) => (session.workspaceId ?? "") === selectedWorkspaceId && session.profileId === selectedProfileId).map((session) => (
              <button key={session.id} type="button" className={cx(session.id === selectedSessionId && "is-active")} onClick={() => selectSession(session.id)}>
                <span className="session-list__body"><strong>{session.title}</strong><small>{session.preview}</small></span>
                <span className="session-list__meta">{session.unread ? <i /> : null}{session.updatedAt}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="sidebar-footer">
          {canCreateSession ? <Button variant="primary" leadingIcon={<Plus size={18} />} onClick={() => void createChat()}>Nuevo chat</Button> : null}
          <div>
            <Link to="/chats" className={pathname === "/chats" ? "is-active" : ""}><Archive size={19} /> Chats</Link>
            <Link to="/settings" className={pathname === "/settings" ? "is-active" : ""}><GearSix size={19} /> Ajustes</Link>
          </div>
        </div>
      </aside>
      {workspaceEditorOpen ? <div ref={workspaceDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="workspace-editor-title"><button className="modal-scrim" aria-label="Cerrar editor de workspace" onClick={closeWorkspaceEditor} /><div className="hc-panel form-modal workspace-editor"><span className="eyebrow">Organización local</span><h2 id="workspace-editor-title">{editingWorkspace ? "Editar workspace" : "Nuevo workspace"}</h2><p>Hermes conserva las conversaciones; Control guarda aquí su organización y etiquetas.</p><form onSubmit={(event) => void saveWorkspace(event)}><label className="hc-field"><span>Nombre</span><input autoFocus maxLength={200} value={workspaceName} onChange={(event) => setWorkspaceName(event.target.value)} /></label><label className="hc-field"><span>Descripción</span><textarea rows={3} maxLength={4000} value={workspaceDescription} onChange={(event) => setWorkspaceDescription(event.target.value)} /></label>{workspaceError ? <p className="form-error" role="alert">{workspaceError}</p> : null}<div>{editingWorkspace ? <Button type="button" variant="danger" disabled={workspaceBusy} leadingIcon={<Archive />} onClick={() => void archiveWorkspace()}>Archivar</Button> : <span />}<span><Button type="button" variant="ghost" disabled={workspaceBusy} onClick={closeWorkspaceEditor}>Cancelar</Button><Button type="submit" variant="primary" disabled={workspaceBusy || !workspaceName.trim()}>{workspaceBusy ? "Guardando…" : "Guardar"}</Button></span></div></form></div></div> : null}
    </>
  );
}
