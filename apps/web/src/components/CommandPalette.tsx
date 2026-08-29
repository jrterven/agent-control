import { ArrowRight, ChatText, Folder, Lightning, MagnifyingGlass, Robot, X } from "@phosphor-icons/react";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { IconButton } from "@hermes-control/ui";
import { useTranslation } from "react-i18next";
import { buildSearchResults } from "../lib/search";
import { useOverlayDialog } from "../lib/useOverlayDialog";
import { useAppStore } from "../store/appStore";

const iconByKind = { session: ChatText, message: ChatText, workspace: Folder, automation: Lightning };

export function CommandPalette() {
  const { t } = useTranslation();
  const open = useAppStore((state) => state.commandOpen);
  const setOpen = useAppStore((state) => state.setCommandOpen);
  const [query, setQuery] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const navigate = useNavigate();
  const sessions = useAppStore((state) => state.sessions);
  const workspaces = useAppStore((state) => state.workspaces);
  const automations = useAppStore((state) => state.automations);
  const messages = useAppStore((state) => state.messages);
  const profiles = useAppStore((state) => state.profiles);
  const selectSession = useAppStore((state) => state.selectSession);
  const selectWorkspace = useAppStore((state) => state.selectWorkspace);
  const dialog = useOverlayDialog<HTMLDivElement>({ open, onClose: () => setOpen(false), mediaQuery: "(min-width: 0px)" });
  const results = useMemo(() => buildSearchResults({ sessions, workspaces, automations, messages, profiles }, t)
    .filter((result) => `${result.title} ${result.excerpt}`.toLowerCase().includes(query.toLowerCase()))
    .slice(0, 6), [automations, messages, profiles, query, sessions, t, workspaces]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") { event.preventDefault(); setOpen(!useAppStore.getState().commandOpen); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setOpen]);

  useEffect(() => { if (open) window.setTimeout(() => inputRef.current?.focus(), 30); }, [open]);
  if (!open) return null;

  const openResult = (result: (typeof results)[number]) => {
    setOpen(false);
    if (result.kind === "automation") { void navigate({ to: "/automations" }); return; }
    if (result.kind === "workspace" && result.targetId) selectWorkspace(result.targetId);
    else if (result.targetId) selectSession(result.targetId);
    void navigate({ to: "/chats" });
  };

  return (
    <div className="command-layer" role="dialog" aria-modal="true" aria-label={t("command.dialogLabel")}>
      <button className="command-layer__scrim" aria-label={t("command.closeSearch")} onClick={() => setOpen(false)} />
      <div ref={dialog.containerRef} tabIndex={-1} className="command-palette">
        <div className="command-input"><MagnifyingGlass size={21} /><input ref={inputRef} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("command.placeholder")} aria-label={t("command.searchLabel")} /><IconButton label={t("command.close")} icon={<X />} onClick={() => setOpen(false)} /></div>
        <div className="command-results">
          <span className="eyebrow">{t("command.quickResults")}</span>
          {results.map((result) => {
            const Icon = iconByKind[result.kind];
            return <button key={result.id} type="button" onClick={() => openResult(result)}><span className="command-result__icon"><Icon /></span><span><strong>{result.title}</strong><small>{result.excerpt}</small></span><ArrowRight /></button>;
          })}
          {results.length === 0 ? <div className="command-empty"><Robot size={28} /><span>{t("command.noMatches")}</span></div> : null}
        </div>
        <footer><span><kbd>↵</kbd> {t("command.open")}</span><span><kbd>esc</kbd> {t("command.closeHint")}</span><button type="button" onClick={() => { setOpen(false); void navigate({ to: "/search" }); }}>{t("command.advancedSearch")}</button></footer>
      </div>
    </div>
  );
}
