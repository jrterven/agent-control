import { useEffect, useState, type ReactNode } from "react";
import { ArrowDown, ArrowUp, ArrowUpRight, ChatTeardropText, Check, Clock, DotsThree, House, Lightning, Plus, PushPinSimple, SlidersHorizontal, Sparkle, Trash, X } from "@phosphor-icons/react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { Button, IconButton } from "@hermes-control/ui";
import type { HomeData, HomeLayout, HomeSources, PanelBlock, PanelDefinition, PanelSnapshot, Suggestion } from "@hermes-control/shared-types";
import { catalog, homeMutation, newPanel, useHome, useHomeChatContext, type PanelPreview } from "../lib/home";
import { ApiError } from "../lib/api";
import { useOverlayDialog } from "../lib/useOverlayDialog";
import { useAppStore } from "../store/appStore";
import { MessageMarkdown } from "./MessageMarkdown";
import "./Home.css";

function HomeDialog({ title, onClose, children }: { title: string; onClose: () => void; children: ReactNode }) {
  const overlay = useOverlayDialog<HTMLDivElement>({ open: true, onClose, mediaQuery: "(min-width: 0px)" });
  const { t } = useTranslation();
  return <div className="home-dialog-backdrop" onClick={(event) => { if (event.target === event.currentTarget) onClose(); }}><div ref={overlay.containerRef} className="home-dialog" role="dialog" aria-modal="true" aria-label={title}><header><h2>{title}</h2><IconButton label={t("home.close")} icon={<X />} onClick={onClose} /></header>{children}</div></div>;
}

export function PanelBlocks({ blocks = [] }: { blocks?: PanelBlock[] }) {
  return <div className="panel-blocks">{blocks.map((block, index) => <section key={index}>
    {block.title && <h3>{block.title}</h3>}
    {block.text && <p>{block.text}</p>}
    {block.items && <ul>{block.items.map((item, i) => <li key={i}>{item}</li>)}</ul>}
    {block.metrics && <dl className="home-metrics">{block.metrics.map((metric, i) => <div key={i}><dt>{metric.label}</dt><dd>{metric.value}</dd></div>)}</dl>}
    {block.columns && block.rows && <div className="home-table"><table><thead><tr>{block.columns.map((column, i) => <th key={i}>{column}</th>)}</tr></thead><tbody>{block.rows.map((row, i) => <tr key={i}>{row.map((cell, j) => <td key={j}>{cell}</td>)}</tr>)}</tbody></table></div>}
    {block.points && <dl className="home-chart">{block.points.map((point, i) => <div key={i}><dt>{point.label}</dt><dd><span style={{ width: `${Math.min(100, Math.abs(point.value) / Math.max(1, ...block.points!.map((p) => Math.abs(p.value))) * 100)}%` }} /><strong>{point.value.toLocaleString()}</strong></dd></div>)}</dl>}
  </section>)}</div>;
}

function useHomeNavigation() {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const openSession = (id: string) => { useAppStore.getState().selectSession(id); void navigate({ to: "/chats" }); };
  const ask = (panel: PanelDefinition, snapshot?: PanelSnapshot, suggestion?: Suggestion) => {
    const state = useAppStore.getState();
    const profile = suggestion?.profileId ?? panel.profileId;
    if (profile) state.selectProfile(profile);
    state.prepareChat();
    useHomeChatContext.getState().set(state.userId ?? "", `${t("home.chatPrompt")}\n\n${JSON.stringify(suggestion ?? { panel: panel.title || t(`home.kinds.${panel.kind}`), snapshot }, null, 2).slice(0, 12000)}`);
    void navigate({ to: "/chats" });
  };
  return { openSession, ask };
}

function SuggestionCard({ item, prepared, onReview, act }: { item: Suggestion; prepared: boolean; onReview: (s: Suggestion) => void; act: (id: string, action: string) => Promise<void> }) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(false);
  const action = async (value: string) => { setBusy(true); setError(false); try { await act(item.id, value); } catch { setError(true); } finally { setBusy(false); } };
  return <article className="home-suggestion"><span className="home-suggestion__title">{item.unread && <span className="home-unread" />}<strong>{item.title}</strong></span><p>{prepared ? item.draft.slice(0, 180) : item.reason}</p><div className="home-suggestion__actions"><Button variant="ghost" onClick={() => onReview(item)}>{t("home.review")} <ArrowUpRight /></Button><button disabled={busy} onClick={() => void action("snooze")}>{t("home.snooze")}</button><button disabled={busy} onClick={() => void action("dismiss")}>{t("home.dismiss")}</button></div>{error && <p role="alert">{t("home.saveError")}</p>}</article>;
}

function HomePanel({ panel, snapshot, data, edit, onChange, onMove, onRemove, onReview, act, onConfigure }: { panel: PanelDefinition; snapshot?: PanelSnapshot; data: HomeData; edit?: boolean; onChange?: (p: PanelDefinition) => void; onMove?: (direction: number) => void; onRemove?: () => void; onReview: (s: Suggestion) => void; act: (id: string, action: string) => Promise<void>; onConfigure?: () => void }) {
  const { t, i18n } = useTranslation();
  const { openSession, ask } = useHomeNavigation();
  const items = snapshot?.items ?? [];
  const suggestions = data.suggestions.filter((s) => panel.kind === "mail" ? s.sourceKey.startsWith("mail:") : panel.kind !== "prepared" || s.draft);
  const suggestionPanel = panel.kind === "pending" || panel.kind === "prepared" || (panel.kind === "mail" && suggestions.length > 0);
  return <article className={`home-panel home-panel--${panel.size}`} aria-label={panel.title || t(`home.kinds.${panel.kind}`)}><header><span className="home-panel__icon">{panel.kind === "prepared" ? <Sparkle /> : panel.kind === "automations" ? <Lightning /> : panel.kind === "pending" ? <Check /> : <House />}</span><h2>{panel.title || t(`home.kinds.${panel.kind}`)}</h2>{panel.pinned && <PushPinSimple aria-label={t("home.pin")} />}{!edit && panel.kind === "custom" && onConfigure && <IconButton label={t("home.customize")} icon={<SlidersHorizontal />} onClick={onConfigure} />}</header>
    {edit && <div className="home-panel__edit"><IconButton label={t("home.up")} icon={<ArrowUp />} onClick={() => onMove?.(-1)} /><IconButton label={t("home.down")} icon={<ArrowDown />} onClick={() => onMove?.(1)} /><select aria-label={t("home.customize")} value={panel.size} onChange={(e) => onChange?.({ ...panel, size: e.target.value as PanelDefinition["size"] })}><option value="compact">{t("home.compact")}</option><option value="wide">{t("home.wide")}</option></select><IconButton label={t(panel.pinned ? "home.unpin" : "home.pin")} selected={panel.pinned} icon={<PushPinSimple />} onClick={() => onChange?.({ ...panel, pinned: !panel.pinned })} /><IconButton label={t("home.remove")} icon={<Trash />} onClick={onRemove} /></div>}
    {suggestionPanel ? (suggestions.length ? suggestions.map((item) => <SuggestionCard key={item.id} item={item} prepared={panel.kind === "prepared"} onReview={onReview} act={act} />) : <div className="home-panel__empty"><Sparkle size={28} /><p>{t(panel.kind === "prepared" ? "home.noPrepared" : "home.hints.pending")}</p></div>) : panel.kind === "custom" ? <><PanelBlocks blocks={snapshot?.blocks} />{!snapshot?.blocks?.length && <p className="home-muted">{t("home.pending")}</p>}</> : items.length ? <ul className="home-items">{items.map((item) => <li key={item.id}><div><strong>{item.title}</strong>{item.detail && <small>{item.detail}</small>}{item.status && <small>{t(`home.statuses.${item.status}`, { defaultValue: item.status })}</small>}{item.observedAt && <small>{new Date(item.observedAt).toLocaleString(i18n.language)}</small>}</div>{item.sessionId ? <IconButton label={`${t("home.review")}: ${item.title}`} icon={<ArrowUpRight />} onClick={() => openSession(item.sessionId!)} /> : item.url && <Link to={item.url as "/automations"} aria-label={`${t("home.review")}: ${item.title}`}><ArrowUpRight /></Link>}</li>)}</ul> : <div className="home-panel__empty"><Clock size={26} /><p>{t("home.nothing")}</p><small>{t(`home.hints.${panel.kind}`, { defaultValue: "" })}</small></div>}
    {snapshot?.errorCode && <p role="status" className="home-muted">{t("home.stale")}</p>}
    {snapshot?.sources?.length ? <details className="home-sources"><summary>{t("home.sources")}</summary>{snapshot.sources.map((source, i) => source.url?.startsWith("https://") ? <a key={i} href={source.url} target="_blank" rel="noreferrer">{source.title}</a> : source.sessionId ? <button key={i} onClick={() => openSession(source.sessionId!)}>{source.title}</button> : <span key={i}>{source.title}</span>)}</details> : null}
    <footer>{snapshot?.updatedAt ? <small>{t("home.updated")}: {new Date(snapshot.updatedAt).toLocaleString(i18n.language)}</small> : <span />}<IconButton label={t("home.ask")} icon={<ChatTeardropText />} onClick={() => ask(panel, snapshot)} /></footer>
  </article>;
}

function SourcePicker({ value, onChange, data, web = false }: { value: HomeSources; onChange: (s: HomeSources) => void; data: HomeData; web?: boolean }) {
  const { t } = useTranslation();
  return <div className="home-source-picker">{([["sessionIds", "sessions"], ["workspaceIds", "workspaces"], ["automationIds", "automations"], ["mailAccountIds", "mailAccounts"]] as const).map(([field, key]) => <details key={key}><summary>{t(`home.${key}`)} <small>{value[field].length}</small></summary><div>{data.sources[key].map((source) => <label key={source.id}><input type="checkbox" checked={value[field].includes(source.id)} onChange={(e) => onChange({ ...value, [field]: e.target.checked ? [...value[field], source.id] : value[field].filter((id) => id !== source.id) })} />{source.label}</label>)}</div></details>)}{web && <label>{t("home.urls")}<textarea rows={3} value={value.urls.join("\n")} onChange={(e) => onChange({ ...value, urls: e.target.value.split("\n").filter(Boolean) })} /></label>}</div>;
}

function ReviewDialog({ data, onSave, onClose }: { data: HomeData; onSave: (layout: HomeLayout) => Promise<unknown>; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const [value, setValue] = useState(data.layout.proactive);
  const [paused, setPaused] = useState(data.layout.paused);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const profile = data.sources.profiles.find((p) => p.id === value.profileId);
  return <HomeDialog title={t("home.settings")} onClose={onClose}><form className="home-form" onSubmit={async (e) => { e.preventDefault(); setBusy(true); setError(""); try { await onSave({ ...data.layout, paused, proactive: { ...value, language: i18n.language.split("-")[0] as typeof value.language } }); onClose(); } catch { setError(t("home.saveError")); } finally { setBusy(false); } }}>
    <label className="home-check"><input type="checkbox" checked={paused} onChange={(e) => setPaused(e.target.checked)} />{t("home.pauseAll")}</label>
    <label className="home-check"><input type="checkbox" checked={value.enabled} onChange={(e) => setValue({ ...value, enabled: e.target.checked })} />{t("home.enable")}</label><p>{t("home.reviewHint")}</p>
    <label>{t("home.agent")}<select required={value.enabled} value={value.profileId ?? ""} onChange={(e) => setValue({ ...value, profileId: e.target.value || null })}><option value="">{t("home.chooseAgent")}</option>{data.sources.profiles.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}</select></label>{profile && !profile.available && <p role="status">{t("home.updateAgent")}</p>}
    <SourcePicker data={data} value={value.sources} onChange={(sources) => setValue({ ...value, sources })} />
    <label>{t("home.timezone")}<input value={value.timezone} onChange={(e) => setValue({ ...value, timezone: e.target.value })} required /></label><fieldset><legend>{t("home.quiet")}</legend><div className="home-two-fields"><label>{t("home.quietStart")}<input type="time" value={value.quietStart ?? ""} onChange={(e) => setValue({ ...value, quietStart: e.target.value || null })} /></label><label>{t("home.quietEnd")}<input type="time" value={value.quietEnd ?? ""} onChange={(e) => setValue({ ...value, quietEnd: e.target.value || null })} /></label></div><small>{t("home.quietHint")}</small></fieldset><p>{t("home.noRepeat")}</p>{error && <p role="alert">{error}</p>}<Button type="submit" variant="primary" disabled={busy}>{t("home.save")}</Button>
  </form></HomeDialog>;
}

function CustomPanelDialog({ data, onSave, onClose, original }: { original?: PanelDefinition | null; data: HomeData; onSave: (layout: HomeLayout) => Promise<unknown>; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const [panel, setPanel] = useState(() => original ? { ...original, id: crypto.randomUUID() } : ({ ...newPanel("custom"), profileId: data.layout.proactive.profileId }));
  const [preview, setPreview] = useState<PanelPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const change = (next: PanelDefinition) => { setPanel(next); setPreview(null); };
  return <HomeDialog title={t("home.describe")} onClose={onClose}><form className="home-form" onSubmit={async (e) => { e.preventDefault(); setBusy(true); setError(""); try { setPreview(await homeMutation<PanelPreview>("/panels/preview", { panel, language: i18n.language.split("-")[0] })); } catch { setError(t("home.unavailable")); } finally { setBusy(false); } }}>
    <label>{t("home.titleLabel")}<input value={panel.title} maxLength={100} onChange={(e) => change({ ...panel, title: e.target.value })} /></label><label>{t("home.description")}<textarea required minLength={5} maxLength={2000} rows={4} placeholder={t("home.example")} value={panel.description} onChange={(e) => change({ ...panel, description: e.target.value })} /></label>
    <div className="home-two-fields"><label>{t("home.agent")}<select required value={panel.profileId ?? ""} onChange={(e) => change({ ...panel, profileId: e.target.value || null })}><option value="">{t("home.chooseAgent")}</option>{data.sources.profiles.map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}</select></label><label>{t("home.frequency")}<select value={panel.refreshMinutes} onChange={(e) => change({ ...panel, refreshMinutes: Number(e.target.value) as PanelDefinition["refreshMinutes"] })}><option value={60}>{t("home.hourlyOption")}</option><option value={1440}>{t("home.daily")}</option><option value={10080}>{t("home.weekly")}</option></select></label></div>
    <SourcePicker data={data} web value={panel.sources} onChange={(sources) => change({ ...panel, sources })} />{error && <p role="alert">{error}</p>}<Button type="submit" disabled={busy}>{busy ? t("home.generating") : t("home.generate")}</Button>
    {preview && <section className="home-preview"><h3>{t("home.preview")}: {preview.definition.title}</h3><PanelBlocks blocks={preview.snapshot.blocks} /><Button variant="primary" disabled={busy} onClick={async () => { setBusy(true); try { await onSave({ ...data.layout, panels: original ? data.layout.panels.map((p) => p.id === original.id ? preview.definition : p) : [...data.layout.panels, preview.definition] }); onClose(); } catch { setError(t("home.saveError")); } finally { setBusy(false); } }}>{t("home.addPreview")}</Button></section>}
  </form></HomeDialog>;
}

function SuggestionDialog({ item, data, act, onClose }: { item: Suggestion; data: HomeData; act: (id: string, action: string) => Promise<void>; onClose: () => void }) {
  const { t } = useTranslation();
  const { openSession, ask } = useHomeNavigation();
  const [error, setError] = useState(false);
  useEffect(() => { void act(item.id, "seen").catch(() => undefined); }, [item.id]);
  return <HomeDialog title={item.title} onClose={onClose}><div className="home-form"><h3>{t("home.evidence")}</h3><p>{item.reason}</p><blockquote>{item.evidence.quote}</blockquote>{item.evidence.sessionId ? <button onClick={() => openSession(item.evidence.sessionId!)}>{item.evidence.title} <ArrowUpRight /></button> : item.evidence.url?.startsWith("https://") && <a href={item.evidence.url} target="_blank" rel="noreferrer">{item.evidence.title} <ArrowUpRight /></a>}{item.dueAt && <p>{t("home.due")}: {new Date(item.dueAt).toLocaleString(undefined, { timeZone: data.layout.proactive.timezone, timeZoneName: "short" })}</p>}{item.draft && <><h3>{t("home.draft")}</h3><MessageMarkdown content={item.draft} sessionId={item.evidence.sessionId ?? ""} /></>}<Button variant="primary" onClick={() => ask(data.layout.panels[0] ?? newPanel("pending"), undefined, item)}>{t("home.continue")}</Button><Button onClick={async () => { try { await act(item.id, "resolve"); onClose(); } catch { setError(true); } }}>{t("home.resolve")}</Button>{error && <p role="alert">{t("home.saveError")}</p>}</div></HomeDialog>;
}

export function HomeAppearance() {
  const { data } = useHome();
  useEffect(() => {
    if (data?.layout.theme) useAppStore.getState().setTheme(data.layout.theme);
  }, [data?.layout.theme]);
  useEffect(() => {
    document.documentElement.dataset.homeBackground = data?.layout.background ?? "mist";
    document.documentElement.dataset.reduceTransparency = String(data?.layout.reduceTransparency ?? false);
    return () => { delete document.documentElement.dataset.homeBackground; delete document.documentElement.dataset.reduceTransparency; };
  }, [data?.layout.background, data?.layout.reduceTransparency]);
  return null;
}

export function HomeScreen() {
  const home = useHome();
  const { t, i18n } = useTranslation();
  const [editing, setEditing] = useState<HomeLayout | null>(null);
  const [dialog, setDialog] = useState<"catalog" | "review" | "custom" | null>(null);
  const [selected, setSelected] = useState<Suggestion | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [customPanel, setCustomPanel] = useState<PanelDefinition | null>(null);
  const [dragged, setDragged] = useState<string | null>(null);
  const data = home.data;
  const owner = useAppStore((s) => s.userId);
  useEffect(() => { setEditing(null); setDialog(null); setSelected(null); }, [owner]);
  if (!data) return <div className="home-page"><h1>{t("home.title")}</h1><p role="status">{t(home.isLoading ? "home.loading" : "home.error")}</p><Button onClick={() => void home.refetch()}>{t("home.retry")}</Button><Link to="/chats">{t("nav.chats")}</Link></div>;
  const layout = editing ?? data.layout;
  const move = (id: string, target: number) => { const panels = [...layout.panels]; const at = panels.findIndex((p) => p.id === id); if (at < 0 || target < 0 || target >= panels.length) return; const [panel] = panels.splice(at, 1); panels.splice(target, 0, panel); setEditing({ ...layout, panels }); };
  const save = async () => { if (!editing) return; setBusy(true); setError(""); try { await home.save(editing); setEditing(null); } catch (e) { setError(t(e instanceof ApiError && e.status === 409 ? "home.conflict" : "home.saveError")); } finally { setBusy(false); } };
  return <div className="home-page"><header className="home-hero"><div><span className="home-eyebrow">{t("home.title")} <span> / </span>{new Date().toLocaleDateString(i18n.language, { weekday: "long", month: "long", day: "numeric" })}</span><h1>{t("home.greeting")}</h1><p>{t("home.subtitle")}</p></div><div className="home-toolbar">{editing ? <><Button disabled={busy} onClick={() => setEditing(null)}>{t("home.cancel")}</Button><Button variant="primary" disabled={busy} onClick={() => void save()}>{t("home.done")}</Button></> : <Button leadingIcon={<SlidersHorizontal />} onClick={() => setEditing(structuredClone(layout))}>{t("home.customize")}</Button>}<Button leadingIcon={<Plus />} onClick={() => setDialog("catalog")}>{t("home.add")}</Button></div></header>
    <button disabled={Boolean(editing) || !data.features.proactive} className="home-review-strip" onClick={() => setDialog("review")}><span className={`home-review-dot ${layout.proactive.enabled ? "is-on" : ""}`} /><span><strong>{t(layout.paused ? "home.paused" : layout.proactive.enabled ? "home.hourly" : layout.panels.some((p) => p.kind === "custom") ? "home.panelsActive" : "home.paused")}</strong><small>{data.review.errorCode ? t(`home.errors.${data.review.errorCode}`, { defaultValue: t("home.unavailable") }) : data.review.lastCheckedAt ? `${t("home.reviewed")}: ${new Date(data.review.lastCheckedAt).toLocaleString(i18n.language)}` : t("home.never")}</small></span><ArrowUpRight /></button>
    {data.review.errorCode && <Button onClick={async () => { try { await homeMutation("/review/retry", {}); await home.refetch(); } catch { setError(t("home.saveError")); } }}>{t("home.retry")}</Button>}
    {editing && <div className="home-appearance"><label>{t("home.theme")}<select aria-label={t("home.theme")} value={layout.theme ?? ""} onChange={(e) => setEditing({ ...layout, theme: (e.target.value || null) as HomeLayout["theme"] })}>{["", "light", "dark", "auto"].map((value) => <option key={value} value={value}>{t(`home.themes.${value || "device"}`)}</option>)}</select></label><label>{t("home.background")}<select aria-label={t("home.background")} value={layout.background} onChange={(e) => setEditing({ ...layout, background: e.target.value as HomeLayout["background"] })}>{["mist", "dusk", "plain"].map((value) => <option key={value} value={value}>{t(`home.${value}`)}</option>)}</select></label><label className="home-check"><input type="checkbox" checked={layout.reduceTransparency} onChange={(e) => setEditing({ ...layout, reduceTransparency: e.target.checked })} />{t("home.reduce")}</label></div>}
    {error && <p className="home-alert" role="alert">{error} <button onClick={() => { setEditing(null); void home.refetch(); }}>{t("home.retry")}</button></p>}
    <div className="home-grid">{layout.panels.map((panel, index) => <div key={panel.id} className={panel.size === "wide" ? "home-slot home-slot--wide" : "home-slot"} draggable={Boolean(editing)} onDragStart={() => setDragged(panel.id)} onDragOver={(e) => { if (editing) e.preventDefault(); }} onDrop={(e) => { e.preventDefault(); if (dragged && editing) move(dragged, index); setDragged(null); }}><HomePanel panel={panel} snapshot={data.snapshots[panel.id]} data={data} edit={Boolean(editing)} onChange={(next) => setEditing({ ...layout, panels: layout.panels.map((p) => p.id === next.id ? next : p) })} onMove={(direction) => move(panel.id, index + direction)} onRemove={() => setEditing({ ...layout, panels: layout.panels.filter((p) => p.id !== panel.id) })} onReview={setSelected} act={home.act} onConfigure={() => { setCustomPanel(panel); setDialog("custom"); }} /></div>)}</div>
    {!layout.panels.length && <p className="home-empty">{t("home.empty")}</p>}<button className="home-add-card" onClick={() => setDialog("catalog")}><Plus />{t("home.add")}</button>
    {dialog === "catalog" && <HomeDialog title={t("home.add")} onClose={() => setDialog(null)}><div className="home-catalog">{catalog.filter((kind) => !layout.panels.some((p) => p.kind === kind)).map((kind) => <button key={kind} onClick={async () => { const next = { ...layout, panels: [...layout.panels, newPanel(kind)] }; if (editing) { setEditing(next); setDialog(null); } else { setBusy(true); try { await home.save(next); setDialog(null); } catch { setError(t("home.saveError")); } finally { setBusy(false); } } }} disabled={busy}><Plus /><span><strong>{t(`home.kinds.${kind}`)}</strong><small>{t(`home.hints.${kind}`)}</small></span></button>)}<button disabled={Boolean(editing) || !data.features.customPanels} onClick={() => { setCustomPanel(null); setDialog("custom"); }}><Sparkle /><span><strong>{t("home.describe")}</strong><small>{t("home.example")}</small></span></button></div></HomeDialog>}
    {dialog === "review" && <ReviewDialog data={data} onSave={home.save} onClose={() => setDialog(null)} />}
    {dialog === "custom" && <CustomPanelDialog original={customPanel} data={data} onSave={home.save} onClose={() => setDialog(null)} />}
    {selected && <SuggestionDialog item={selected} data={data} act={home.act} onClose={() => setSelected(null)} />}
  </div>;
}

export function HomePinnedPanels() {
  const home = useHome();
  const { t } = useTranslation();
  const [selected, setSelected] = useState<Suggestion | null>(null);
  const panels = home.data?.layout.panels.filter((p) => p.pinned) ?? [];
  if (!panels.length || !home.data) return null;
  return <><aside className="home-pinned" aria-label={t("home.pinned")}><header><h2>{t("home.pinned")}</h2><Link to="/home"><DotsThree aria-label={t("home.customize")} /></Link></header>{panels.map((panel) => <HomePanel key={panel.id} panel={panel} snapshot={home.data!.snapshots[panel.id]} data={home.data!} onReview={setSelected} act={home.act} />)}</aside>{selected && <SuggestionDialog item={selected} data={home.data} act={home.act} onClose={() => setSelected(null)} />}</>;
}
