import { useEffect, useRef, useState, type FormEvent } from "react";
import { FolderSimple, PushPinSimple, PushPinSimpleSlash, Trash, WarningCircle, X } from "@phosphor-icons/react";
import { Button, IconButton } from "@hermes-control/ui";
import { useTranslation } from "react-i18next";
import { api } from "../lib/api";
import { useOverlayDialog } from "../lib/useOverlayDialog";
import { useAppStore } from "../store/appStore";
import type { SessionSummary } from "../types";

type BulkAction = "move" | "delete" | "pin" | "unpin";

export function useSidebarBulkSelection() {
  const { t } = useTranslation();
  const sessions = useAppStore((state) => state.sessions);
  const profiles = useAppStore((state) => state.profiles);
  const selectedGatewayId = useAppStore((state) => state.selectedGatewayId);
  const authState = useAppStore((state) => state.authState);
  const authGeneration = useAppStore((state) => state.authGeneration);
  const userId = useAppStore((state) => state.userId);
  const demoMode = useAppStore((state) => state.demoMode);
  const disabled = authState !== "authenticated" || demoMode;
  const [active, setActive] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(() => new Set());
  const [dialog, setDialog] = useState<"move" | "delete" | null>(null);
  const [workspaceId, setWorkspaceId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [announcement, setAnnouncement] = useState("");
  const busyRef = useRef(false);
  const generationRef = useRef(0);
  const selected = sessions.filter((session) => selectedIds.has(session.id));
  const canDelete = selected.length > 0 && selected.every((session) => {
    const profile = profiles.find((item) => item.id === session.profileId);
    return Boolean(profile?.mutable && profile.capabilitySet?.methods.includes("session.delete"));
  });

  useEffect(() => {
    generationRef.current += 1;
    busyRef.current = false;
    setBusy(false);
    setActive(false);
    setSelectedIds(new Set());
    setDialog(null);
    setError("");
    setAnnouncement("");
    return () => { generationRef.current += 1; };
  }, [selectedGatewayId, userId, authGeneration, disabled]);

  useEffect(() => {
    const existingIds = new Set(sessions.map((session) => session.id));
    setSelectedIds((current) => {
      if ([...current].every((id) => existingIds.has(id))) return current;
      return new Set([...current].filter((id) => existingIds.has(id)));
    });
  }, [sessions]);

  const exit = () => {
    if (busyRef.current) return;
    setActive(false);
    setSelectedIds(new Set());
    setDialog(null);
    setError("");
    setAnnouncement("");
  };

  const toggle = (id: string) => {
    if (busyRef.current || disabled) return;
    setError("");
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleVisible = (visible: SessionSummary[]) => {
    if (busyRef.current || disabled) return;
    setError("");
    setSelectedIds((current) => {
      const next = new Set(current);
      const allSelected = visible.every((session) => current.has(session.id));
      for (const session of visible) {
        if (allSelected) next.delete(session.id);
        else next.add(session.id);
      }
      return next;
    });
  };

  const openDialog = (action: "move" | "delete") => {
    if (disabled || busyRef.current || !selected.length || (action === "delete" && !canDelete)) return;
    setWorkspaceId(selected[0].workspaceId ?? "");
    setError("");
    setAnnouncement("");
    setDialog(action);
  };

  const closeDialog = () => {
    if (busyRef.current) return;
    setDialog(null);
    setError("");
  };

  const run = async (action: BulkAction) => {
    if (disabled || busyRef.current || !selected.length || (action === "delete" && !canDelete)) return;
    const targets = [...selected];
    const destination = workspaceId;
    const generation = generationRef.current;
    const state = useAppStore.getState();
    const isSameScope = () => useAppStore.getState().selectedGatewayId === selectedGatewayId
      && useAppStore.getState().userId === userId
      && useAppStore.getState().authGeneration === authGeneration
      && useAppStore.getState().authState === "authenticated"
      && !useAppStore.getState().demoMode;
    const isCurrent = () => generationRef.current === generation && isSameScope();
    busyRef.current = true;
    setBusy(true);
    setError("");
    setAnnouncement("");
    const failedIds = new Set<string>();
    const failureMessages = new Set<string>();
    try {
      // Limit simultaneous requests so selecting a long history does not flood the gateway.
      for (let start = 0; start < targets.length; start += 4) {
        if (!isCurrent()) return;
        const batch = targets.slice(start, start + 4);
        const csrfToken = useAppStore.getState().csrfToken;
        const results = await Promise.allSettled(batch.map(async (session) => {
          if (action === "delete") {
            await api.deleteSessionFromHermes(session.id, session.storedSessionId, csrfToken);
            if (isSameScope()) state.removeSession(session.id);
          } else if (action === "move") {
            if ((session.workspaceId ?? "") === destination) return;
            const moved = await api.moveSession(session.id, destination || null, csrfToken);
            if (isSameScope()) state.updateSession(session.id, { workspaceId: moved.workspaceId, updatedAt: moved.updatedAt });
          } else {
            const pinned = action === "pin";
            if (Boolean(session.pinnedAt) === pinned) return;
            const updated = await api.setSessionPinned(session.id, pinned, csrfToken);
            if (isSameScope()) state.updateSession(session.id, { pinnedAt: updated.pinnedAt, updatedAt: updated.updatedAt });
          }
        }));
        results.forEach((result, index) => {
          if (result.status === "rejected") {
            failedIds.add(batch[index].id);
            failureMessages.add(result.reason instanceof Error ? result.reason.message : t("sidebar.bulkActionError"));
          }
        });
      }
      if (!isCurrent()) return;
      setSelectedIds(failedIds);
      const succeeded = targets.length - failedIds.size;
      if (failedIds.size) {
        const summary = t("sidebar.bulkPartialFailure", { succeeded, failed: failedIds.size });
        setError(`${summary} ${[...failureMessages].join(" ")}`);
        setAnnouncement(summary);
      } else {
        setDialog(null);
        const announcementKey = {
          move: "sidebar.bulkMoveAnnouncement",
          delete: "sidebar.bulkDeleteAnnouncement",
          pin: "sidebar.bulkPinAnnouncement",
          unpin: "sidebar.bulkUnpinAnnouncement",
        } as const;
        setAnnouncement(t(announcementKey[action], { count: succeeded }));
      }
    } finally {
      if (generationRef.current === generation) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };

  return {
    active, selectedIds, selected, dialog, workspaceId, busy, error, announcement, disabled, canDelete,
    allPinned: selected.length > 0 && selected.every((session) => Boolean(session.pinnedAt)),
    enter: () => { if (!disabled) { setActive(true); setAnnouncement(""); } },
    exit, toggle, toggleVisible, openDialog, closeDialog, setWorkspaceId, run,
  };
}

type Selection = ReturnType<typeof useSidebarBulkSelection>;

export function SidebarSelectionToolbar({ selection, visible, onExit }: { selection: Selection; visible: SessionSummary[]; onExit: () => void }) {
  const { t } = useTranslation();
  const toolbarRef = useRef<HTMLDivElement>(null);
  const checkboxRef = useRef<HTMLInputElement>(null);
  const wasBusyRef = useRef(selection.busy);
  const visibleSelected = visible.filter((session) => selection.selectedIds.has(session.id)).length;
  useEffect(() => {
    if (checkboxRef.current) checkboxRef.current.indeterminate = visibleSelected > 0 && visibleSelected < visible.length;
  }, [visibleSelected, visible.length]);
  useEffect(() => {
    const finished = wasBusyRef.current && !selection.busy;
    wasBusyRef.current = selection.busy;
    if (!finished || selection.dialog) return;
    // Completed actions clear the selection and disable their trigger buttons.
    // Return keyboard users to an available control instead of a disabled trigger.
    if (checkboxRef.current && !checkboxRef.current.disabled) checkboxRef.current.focus();
    else toolbarRef.current?.querySelector<HTMLButtonElement>("button:not([disabled])")?.focus();
  }, [selection.busy, selection.dialog]);
  if (!selection.active) return null;
  return <div ref={toolbarRef} className="sidebar-selection" aria-label={t("sidebar.selectConversations")} aria-busy={selection.busy || undefined}>
    <div className="sidebar-selection__heading">
      <strong aria-live="polite">{t("sidebar.selectionCount", { count: selection.selected.length })}</strong>
      <IconButton label={t("sidebar.exitSelection")} icon={<X size={17} />} disabled={selection.busy} onClick={onExit} />
    </div>
    <label className="sidebar-selection__all"><input ref={checkboxRef} type="checkbox" checked={visible.length > 0 && visibleSelected === visible.length} disabled={selection.busy || selection.disabled || !visible.length} onChange={() => selection.toggleVisible(visible)} />{t("sidebar.selectVisible")}</label>
    <div className="sidebar-selection__actions">
      <Button variant="ghost" disabled={selection.busy || selection.disabled || !selection.selected.length} leadingIcon={<FolderSimple size={16} />} onClick={() => selection.openDialog("move")}>{t("sidebar.move")}</Button>
      <IconButton label={t(selection.allPinned ? "sidebar.bulkUnpin" : "sidebar.bulkPin")} icon={selection.allPinned ? <PushPinSimpleSlash size={18} /> : <PushPinSimple size={18} />} disabled={selection.busy || selection.disabled || !selection.selected.length} onClick={() => void selection.run(selection.allPinned ? "unpin" : "pin")} />
      <IconButton label={t("activity.deleteEllipsis")} icon={<Trash size={18} />} disabled={selection.busy || selection.disabled || !selection.canDelete} onClick={() => selection.openDialog("delete")} />
    </div>
    {selection.selected.length && !selection.canDelete ? <p className="sidebar-selection__hint">{t("sidebar.bulkDeleteUnavailable")}</p> : null}
    {selection.error && !selection.dialog ? <p className="form-error" role="alert">{selection.error}</p> : null}
  </div>;
}

export function SidebarBulkDialog({ selection }: { selection: Selection }) {
  const { t } = useTranslation();
  const workspaces = useAppStore((state) => state.workspaces);
  const overlay = useOverlayDialog<HTMLDivElement>({ open: Boolean(selection.dialog), onClose: selection.closeDialog, mediaQuery: "(min-width: 0px)" });
  if (!selection.dialog) return null;
  const deleting = selection.dialog === "delete";
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    void selection.run(deleting ? "delete" : "move");
  };
  return <div className="modal-layer" role="presentation">
    <button className="modal-scrim" aria-label={t(deleting ? "activity.cancelDeleteAria" : "sidebar.closeMoveEditor")} onClick={selection.closeDialog} />
    <div ref={overlay.containerRef} tabIndex={-1} className="hc-panel form-modal session-bulk-dialog" role="dialog" aria-modal="true" aria-labelledby="session-bulk-title" aria-describedby="session-bulk-description" aria-busy={selection.busy || undefined}>
      <span className="eyebrow">{t(deleting ? "activity.irreversible" : "sidebar.localOrganization")}</span>
      <h2 id="session-bulk-title">{t(deleting ? "sidebar.bulkDeleteTitle" : "sidebar.bulkMoveTitle", { count: selection.selected.length })}</h2>
      <p id="session-bulk-description">{t(deleting ? "sidebar.bulkDeleteDescription" : "sidebar.bulkMoveDescription", { count: selection.selected.length })}</p>
      <ul className="session-bulk-dialog__list">{selection.selected.map((session) => <li key={session.id}>{session.title}</li>)}</ul>
      <form onSubmit={onSubmit}>
        {!deleting ? <label className="hc-field"><span>{t("sidebar.workspaceDestination")}</span><select value={selection.workspaceId} disabled={selection.busy} onChange={(event) => selection.setWorkspaceId(event.target.value)}><option value="">{t("sidebar.noWorkspace")}</option>{workspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}</select></label> : null}
        {selection.error ? <p className="form-error" role="alert"><WarningCircle /> {selection.error}</p> : null}
        <div><Button type="button" variant="ghost" disabled={selection.busy} onClick={selection.closeDialog}>{t("sidebar.cancel")}</Button><Button type="submit" variant={deleting ? "danger" : "primary"} disabled={selection.busy || selection.disabled || !selection.selected.length || (deleting ? !selection.canDelete : selection.selected.every((session) => (session.workspaceId ?? "") === selection.workspaceId))} leadingIcon={deleting ? <Trash /> : <FolderSimple />}>{t(deleting ? (selection.busy ? "activity.deleting" : "activity.deleteFromHermes") : (selection.busy ? "sidebar.moving" : "sidebar.move"))}</Button></div>
      </form>
    </div>
  </div>;
}
