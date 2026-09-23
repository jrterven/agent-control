import { ArrowRight, FileText, FolderOpen, Lightning, MagnifyingGlass, WarningCircle } from "@phosphor-icons/react";
import { useNavigate } from "@tanstack/react-router";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@hermes-control/ui";
import { api, ApiError, type SemanticSearchStatus } from "../lib/api";
import { buildSearchResults } from "../lib/search";
import { useAppStore } from "../store/appStore";
import type { SearchResult } from "../types";

const errorKey = (code?: string | null) => {
  if (code === "OPENAI_NOT_CONFIGURED") return "key";
  if (code === "SEMANTIC_CONNECTOR_UPDATE") return "update";
  if (code === "SEMANTIC_CONNECTION") return "connection";
  if (code === "SEMANTIC_QUOTA") return "quota";
  if (code === "SEMANTIC_RATE_LIMIT") return "rate";
  if (["SEMANTIC_AUTH", "OPENAI_SECRET_UNAVAILABLE"].includes(code ?? "")) return "auth";
  return "error";
};

export function SearchScreen() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [tab, setTab] = useState<"lexical" | "semantic">("lexical");
  const [filter, setFilter] = useState<"all" | SearchResult["kind"]>("all");
  const [lexical, setLexical] = useState<SearchResult[]>([]);
  const [semantic, setSemantic] = useState<SearchResult[]>([]);
  const [lexicalPartial, setLexicalPartial] = useState(false);
  const [semanticPartial, setSemanticPartial] = useState(false);
  const [lexicalLoading, setLexicalLoading] = useState(false);
  const [semanticLoading, setSemanticLoading] = useState(false);
  const [lexicalError, setLexicalError] = useState("");
  const [semanticError, setSemanticError] = useState<string | null>(null);
  const [statusError, setStatusError] = useState(false);
  const [status, setStatus] = useState<SemanticSearchStatus | null>(null);
  const [saving, setSaving] = useState(false);
  const [retry, setRetry] = useState(0);
  const viewportRef = useRef<HTMLDivElement>(null);
  const lexicalTab = useRef<HTMLButtonElement>(null);
  const semanticTab = useRef<HTMLButtonElement>(null);
  const authState = useAppStore((s) => s.authState);
  const demoMode = useAppStore((s) => s.demoMode);
  const userId = useAppStore((s) => s.userId);
  const authGeneration = useAppStore((s) => s.authGeneration);
  const csrfToken = useAppStore((s) => s.csrfToken);
  const sessions = useAppStore((s) => s.sessions);
  const workspaces = useAppStore((s) => s.workspaces);
  const automations = useAppStore((s) => s.automations);
  const messages = useAppStore((s) => s.messages);
  const profiles = useAppStore((s) => s.profiles);
  const selectSession = useAppStore((s) => s.selectSession);
  const selectWorkspace = useAppStore((s) => s.selectWorkspace);
  const local = demoMode || authState !== "authenticated";
  const ownerScope = `${userId}:${authGeneration}:${local}`;
  const scopeRef = useRef(ownerScope);
  scopeRef.current = ownerScope;
  const settingsRevision = useRef(0);

  useEffect(() => {
    setQuery("");
    setStatus(null);
    setStatusError(false);
    setSaving(false);
    if (local) return;
    let cancelled = false;
    let timer: number;
    const controller = new AbortController();
    async function poll() {
      const version = settingsRevision.current;
      try {
        const next = await api.semanticSearchStatus(controller.signal);
        if (!cancelled && version === settingsRevision.current) {
          setStatus(next);
          setStatusError(false);
        }
      } catch {
        if (!cancelled) setStatusError(true);
      } finally {
        if (!cancelled) timer = window.setTimeout(() => void poll(), 5000);
      }
    }
    void poll();
    return () => { cancelled = true; controller.abort(); window.clearTimeout(timer); };
  }, [local, ownerScope]);

  useEffect(() => {
    setLexical([]);
    setLexicalPartial(false);
    setLexicalError("");
    setLexicalLoading(false);
    if (local || query.trim().length < 2) return;
    let cancelled = false;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setLexicalLoading(true);
      void api.search(query.trim(), filter, 100, controller.signal).then((result) => {
        if (!cancelled) { setLexical(result.items); setLexicalPartial(result.partial); }
      }).catch(() => { if (!cancelled) setLexicalError(t("searchPage.queryError")); })
        .finally(() => { if (!cancelled) setLexicalLoading(false); });
    }, 250);
    return () => { cancelled = true; window.clearTimeout(timer); controller.abort(); };
  }, [query, filter, local, ownerScope, t]);

  const enabled = !!status?.enabled && !!status.configured;
  useEffect(() => {
    setSemantic([]);
    setSemanticPartial(false);
    setSemanticError(null);
    setSemanticLoading(false);
    if (local || !enabled || query.trim().length < 2 || status?.state === "blocked") return;
    let cancelled = false;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setSemanticLoading(true);
      void api.semanticSearch(query.trim(), controller.signal).then((result) => {
        if (!cancelled) { setSemantic(result.items); setSemanticPartial(result.partial); }
      }).catch((error: unknown) => {
        if (!cancelled) setSemanticError(error instanceof ApiError ? error.code ?? "SEMANTIC_ERROR" : "SEMANTIC_ERROR");
      }).finally(() => { if (!cancelled) setSemanticLoading(false); });
    }, 600);
    return () => { cancelled = true; window.clearTimeout(timer); controller.abort(); };
    // The active tab is deliberately absent: both searches run independently.
  }, [query, enabled, local, ownerScope, status?.revision, status?.state, retry]);

  const allLocal = useMemo(() => buildSearchResults({ sessions, workspaces, automations, messages, profiles }, t),
    [sessions, workspaces, automations, messages, profiles, t]);
  const localResults = useMemo(() => {
    const locale = i18n.resolvedLanguage ?? i18n.language;
    const needle = query.trim().toLocaleLowerCase(locale);
    return allLocal.filter((r) => (filter === "all" || r.kind === filter)
      && (!needle || `${r.title} ${r.excerpt} ${r.meta}`.toLocaleLowerCase(locale).includes(needle)));
  }, [allLocal, filter, query, i18n.language, i18n.resolvedLanguage]);
  const semanticVisible = tab === "semantic";
  const results = semanticVisible ? semantic : local ? localResults : lexical;
  const loading = semanticVisible ? semanticLoading : lexicalLoading;
  const partial = semanticVisible ? semanticPartial : lexicalPartial;
  const virtualizer = useVirtualizer({ count: results.length, getScrollElement: () => viewportRef.current, estimateSize: () => 100, overscan: 6 });
  useEffect(() => { viewportRef.current?.scrollTo?.({ top: 0 }); }, [tab, query, filter]);

  async function changeEnabled(value: boolean) {
    const scope = scopeRef.current;
    settingsRevision.current += 1;
    setSaving(true);
    setSemanticError(null);
    try {
      const next = await api.setSemanticSearch(value, csrfToken ?? undefined);
      if (scope === scopeRef.current) { setStatus(next); setStatusError(false); setRetry((r) => r + 1); }
    } catch (error) {
      if (scope === scopeRef.current) setSemanticError(error instanceof ApiError ? error.code ?? "SEMANTIC_ERROR" : "SEMANTIC_ERROR");
    } finally {
      if (scope === scopeRef.current) { settingsRevision.current += 1; setSaving(false); }
    }
  }
  function openResult(result: SearchResult) {
    if (result.kind === "automation") { void navigate({ to: "/automations" }); return; }
    if (result.kind === "workspace" && result.targetId) selectWorkspace(result.targetId);
    else if (result.targetId) selectSession(result.targetId);
    void navigate({ to: "/chats" });
  }
  const filters: Array<[typeof filter, string]> = [["all", t("searchPage.all")], ["message", t("searchPage.messages")], ["session", t("searchPage.sessions")], ["workspace", t("searchPage.workspaces")], ["automation", t("searchPage.automations")]];
  const problem = semanticError || status?.errorCode;
  const empty = query.trim().length < 2 ? t("searchPage.minChars") : loading
    ? t(semanticVisible ? "semanticSearch.loading" : "searchPage.loading")
    : semanticVisible && problem ? t(`semanticSearch.${errorKey(problem)}`) : lexicalError && !semanticVisible ? lexicalError : t("searchPage.noMatches");

  return <div className="page-wrap search-page">
    <header className="page-header"><div><span className="eyebrow">{t("searchPage.eyebrow")}</span><h1>{t("searchPage.title")}</h1><p>{t("semanticSearch.description")}</p></div></header>
    <label className="search-box"><MagnifyingGlass /><input autoFocus aria-label={t("semanticSearch.input")} value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("searchPage.placeholder")} /><kbd>⌘ K</kbd></label>
    <div className="search-filters" role="tablist" aria-label={t("semanticSearch.tabs")} onKeyDown={(e) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      e.preventDefault();
      const next = e.key === "Home" ? "lexical" : e.key === "End" ? "semantic" : tab === "lexical" ? "semantic" : "lexical";
      setTab(next); (next === "lexical" ? lexicalTab : semanticTab).current?.focus();
    }}>
      {(["lexical", "semantic"] as const).map((value) => <button key={value} ref={value === "lexical" ? lexicalTab : semanticTab} id={`search-tab-${value}`} role="tab" aria-selected={tab === value} aria-controls={`search-panel-${value}`} tabIndex={tab === value ? 0 : -1} className={tab === value ? "is-active" : ""} onClick={() => setTab(value)}>{t(`semanticSearch.${value}`)}</button>)}
    </div>
    <div role="tabpanel" id={`search-panel-${tab === "lexical" ? "semantic" : "lexical"}`} aria-labelledby={`search-tab-${tab === "lexical" ? "semantic" : "lexical"}`} hidden />
    <div role="tabpanel" id={`search-panel-${tab}`} aria-labelledby={`search-tab-${tab}`}>
      {!semanticVisible ? <div className="search-filters">{filters.map(([value, label]) => <button key={value} type="button" aria-pressed={filter === value} className={filter === value ? "is-active" : ""} onClick={() => setFilter(value)}>{label}</button>)}</div> :
        <div className="semantic-search-status">
          {local ? <p>{t("semanticSearch.demo")}</p> : <>
            {!status?.enabled && <p>{t("semanticSearch.disclosure")}</p>}
            {status && !status.configured && <p>{t("semanticSearch.key")} <button type="button" onClick={() => void navigate({ to: "/settings" })}>{t("semanticSearch.settings")}</button></p>}
            {status && <div className="search-filters">
              <Button disabled={saving || !status.configured} onClick={() => void changeEnabled(!status.enabled)}>{saving ? t("semanticSearch.saving") : t(status.enabled ? "semanticSearch.disable" : "semanticSearch.enable")}</Button>
              {status.enabled && <span role="status">{t("semanticSearch.progress", { indexed: status.indexed, total: status.total })}</span>}
            </div>}
            {statusError && <p role="status">{t("semanticSearch.statusError")}</p>}
            {problem && <p className="form-warning" role="status"><WarningCircle /> {t(`semanticSearch.${errorKey(problem)}`)} <button disabled={saving} type="button" onClick={() => void changeEnabled(true)}>{t("semanticSearch.retry")}</button></p>}
          </>}
        </div>}
      {partial && <p className="form-warning" role="status"><WarningCircle /> {t(semanticVisible ? "semanticSearch.partial" : "searchPage.partial")}</p>}
      {(!semanticVisible || enabled) && <div className="virtual-results" ref={viewportRef} aria-busy={loading}>
        <div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>{virtualizer.getVirtualItems().map((virtual) => {
          const result = results[virtual.index];
          return <button key={result.id} type="button" className="search-result" style={{ transform: `translateY(${virtual.start}px)`, height: virtual.size }} onClick={() => openResult(result)}>
            <span className="search-result__icon">{result.kind === "automation" ? <Lightning /> : result.kind === "workspace" ? <FolderOpen /> : <FileText />}</span>
            <span><strong>{result.title}</strong><small>{result.excerpt}</small>{semanticVisible && <small>{t(result.source === "live" ? "semanticSearch.live" : "semanticSearch.text")}</small>}</span>
            <span className="search-result__meta">{result.meta}<ArrowRight /></span>
          </button>;
        })}</div>
        {results.length === 0 && <p className="empty-state" role="status">{empty}</p>}
      </div>}
    </div>
  </div>;
}
