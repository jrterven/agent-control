import { useEffect, useMemo, useRef, useState, type KeyboardEvent as ReactKeyboardEvent } from "react";
import { CaretDown, Check, FolderSimple } from "@phosphor-icons/react";
import { Badge, cx } from "@hermes-control/ui";
import { useTranslation } from "react-i18next";
import { useAppStore } from "../store/appStore";

type WorkspaceOption = {
  id: string;
  name: string;
  description?: string;
  sessionCount: number;
};

export function WorkspaceSwitcher() {
  const { t } = useTranslation();
  const selectedWorkspaceId = useAppStore((state) => state.selectedWorkspaceId);
  const selectWorkspace = useAppStore((state) => state.selectWorkspace);
  const workspaces = useAppStore((state) => state.workspaces);
  const unassignedCount = useAppStore((state) => state.sessions.filter((session) => !session.workspaceId).length);
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const initialFocusRef = useRef<"selected" | "first" | "last">("selected");

  const options = useMemo<WorkspaceOption[]>(() => [
    {
      id: "",
      name: t("nav.noWorkspace"),
      sessionCount: unassignedCount,
    },
    ...workspaces.map((workspace) => ({
      id: workspace.id,
      name: workspace.name,
      description: workspace.description,
      sessionCount: workspace.sessionCount,
    })),
  ], [t, unassignedCount, workspaces]);
  const selectedWorkspace = options.find((option) => option.id === selectedWorkspaceId) ?? options[0];

  const closeMenu = (restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) requestAnimationFrame(() => triggerRef.current?.focus());
  };

  useEffect(() => {
    if (!open) return;
    const selected = menuRef.current?.querySelector<HTMLButtonElement>("[role='menuitemradio'][aria-checked='true']");
    const items = menuRef.current?.querySelectorAll<HTMLButtonElement>("[role='menuitemradio']");
    const initial = initialFocusRef.current === "first"
      ? items?.[0]
      : initialFocusRef.current === "last"
        ? items?.[items.length - 1]
        : selected ?? items?.[0];
    initialFocusRef.current = "selected";
    requestAnimationFrame(() => initial?.focus());

    const closeOnOutsidePointer = (event: PointerEvent) => {
      if (!containerRef.current?.contains(event.target as Node)) closeMenu();
    };
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      closeMenu(true);
    };
    document.addEventListener("pointerdown", closeOnOutsidePointer);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("pointerdown", closeOnOutsidePointer);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [open]);

  const chooseWorkspace = (workspaceId: string) => {
    selectWorkspace(workspaceId);
    closeMenu(true);
  };

  const moveFocus = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Tab") {
      closeMenu();
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
    const items = Array.from(menuRef.current?.querySelectorAll<HTMLButtonElement>("[role='menuitemradio']") ?? []);
    if (!items.length) return;
    event.preventDefault();
    const currentIndex = Math.max(0, items.indexOf(document.activeElement as HTMLButtonElement));
    const nextIndex = event.key === "Home"
      ? 0
      : event.key === "End"
        ? items.length - 1
        : event.key === "ArrowDown"
          ? (currentIndex + 1) % items.length
          : (currentIndex - 1 + items.length) % items.length;
    items[nextIndex]?.focus();
  };

  return (
    <div className={cx("workspace-switcher-wrap", open && "is-open")} ref={containerRef}>
      <button
        ref={triggerRef}
        className="workspace-switcher"
        type="button"
        aria-label={`${t("nav.chooseWorkspace")}: ${selectedWorkspace.name}`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls="top-workspace-menu"
        onClick={() => {
          initialFocusRef.current = "selected";
          setOpen((current) => !current);
        }}
        onKeyDown={(event) => {
          if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
          event.preventDefault();
          initialFocusRef.current = event.key === "ArrowDown" ? "first" : "last";
          setOpen(true);
        }}
      >
        <span>{selectedWorkspace.name}</span><CaretDown size={16} />
      </button>
      {open ? <div
        id="top-workspace-menu"
        ref={menuRef}
        className="workspace-switcher__menu"
        role="menu"
        aria-label={t("sidebar.workspaces")}
        onKeyDown={moveFocus}
      >
        {options.map((option) => {
          const selected = option.id === selectedWorkspaceId;
          return <button
            key={option.id || "no-workspace"}
            type="button"
            role="menuitemradio"
            aria-checked={selected}
            tabIndex={-1}
            className={cx("workspace-switcher__option", selected && "is-active")}
            onClick={() => chooseWorkspace(option.id)}
          >
            <span className="workspace-switcher__icon"><FolderSimple size={19} weight={selected ? "fill" : "regular"} /></span>
            <span className="workspace-switcher__copy"><strong>{option.name}</strong>{option.description ? <small>{option.description}</small> : null}</span>
            <Badge>{option.sessionCount}</Badge>
            <Check className="workspace-switcher__check" size={17} weight="bold" aria-hidden="true" />
          </button>;
        })}
      </div> : null}
    </div>
  );
}
