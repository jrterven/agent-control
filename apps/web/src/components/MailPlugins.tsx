import { EnvelopeSimple, Plugs, Plus, ShieldCheck } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { useNavigate } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";
import { Badge, Button, Panel } from "@hermes-control/ui";
import { api, ApiError } from "../lib/api";
import { mailApi, type MailAccount, type MailInput, type MailProvider } from "../lib/mail";
import { useAppStore } from "../store/appStore";
import "./MailPlugins.css";

export function MailPlugins() {
  const owner = useAppStore((state) => state.userId);
  const generation = useAppStore((state) => state.authGeneration);
  const auth = useAppStore((state) => state.authState);
  return <MailPluginsEditor key={`${owner}:${generation}:${auth}`} owner={owner} generation={generation} />;
}

const blank = (provider: "hostinger" | "imap"): MailInput => ({ provider, address: "", label: "", username: "", password: "", service: provider === "hostinger" ? "hostinger" : "custom", imapHost: "", smtpHost: "", smtpPort: 465 });

function MailPluginsEditor({ owner, generation }: { owner?: string; generation: number }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const profiles = useAppStore((s) => s.profiles);
  const gateways = useAppStore((s) => s.gateways);
  const auth = useAppStore((s) => s.authState);
  const demo = useAppStore((s) => s.demoMode);
  const [online, setOnline] = useState(navigator.onLine);
  const [providers, setProviders] = useState<{ id: MailProvider; enabled: boolean }[]>([]);
  const [accounts, setAccounts] = useState<MailAccount[]>([]);
  const [form, setForm] = useState<MailInput | null>(null);
  const [editing, setEditing] = useState<MailAccount | null>(null);
  const [label, setLabel] = useState("");
  const [chosen, setChosen] = useState<string[]>([]);
  const [removing, setRemoving] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [reload, setReload] = useState(0);
  const mounted = useRef(true);
  const pending = useRef<AbortController | null>(null);
  const blocked = auth !== "authenticated" || demo || !online;
  const disabled = blocked || busy;
  const permitted = () => mounted.current && useAppStore.getState().userId === owner && useAppStore.getState().authGeneration === generation && useAppStore.getState().authState === "authenticated" && !useAppStore.getState().demoMode;
  const providerName = (p: MailProvider) => ({ gmail: "Gmail", outlook: "Outlook", hostinger: "Hostinger", imap: t("plugins.other") })[p];
  const stateLabel = (state: string) => t(`plugins.${state === "unavailable" ? "unavailableState" : ["connected", "reconnect_required", "ready", "pending", "pending_removal", "unsupported", "conflict", "idle", "setup_required"].includes(state) ? state : "pending"}`);
  const field = (key: keyof MailInput, value: string | number) => setForm((old) => old ? { ...old, [key]: value } : old);

  useEffect(() => {
    mounted.current = true;
    const changed = () => { setOnline(navigator.onLine); if (!navigator.onLine) { pending.current?.abort(); setForm(null); } };
    window.addEventListener("online", changed); window.addEventListener("offline", changed);
    const url = new URL(window.location.href);
    const result = url.searchParams.get("mailResult");
    if (result) {
      if (result === "connected") setNotice("saved"); else setError("failed");
      url.searchParams.delete("mailResult");
      window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
    }
    return () => { mounted.current = false; pending.current?.abort(); window.removeEventListener("online", changed); window.removeEventListener("offline", changed); };
  }, []);

  useEffect(() => {
    if (blocked) { setLoading(false); return; }
    const controller = new AbortController();
    void Promise.all([mailApi.providers(controller.signal), mailApi.accounts(controller.signal)]).then(([catalog, rows]) => {
      if (permitted() && !controller.signal.aborted) { setProviders(catalog); setAccounts(rows); }
    }).catch(() => { if (permitted() && !controller.signal.aborted) setError("loadError"); }).finally(() => { if (permitted()) setLoading(false); });
    return () => controller.abort();
  }, [blocked, reload]);

  const waiting = accounts.some((a) => a.agents.some((p) => ["pending", "setup_required"].includes(p.state)));
  useEffect(() => {
    if (!waiting || blocked) return;
    const timer = window.setInterval(() => { if (document.visibilityState === "visible") setReload((n) => n + 1); }, 15000);
    return () => window.clearInterval(timer);
  }, [waiting, blocked]);

  const run = async (action: (signal: AbortSignal) => Promise<void>) => {
    if (disabled || pending.current || !permitted()) return;
    const controller = new AbortController(); pending.current = controller;
    setBusy(true); setError(""); setNotice("");
    try { await action(controller.signal); }
    catch (e) {
      if (permitted() && !controller.signal.aborted) setError(e instanceof ApiError && e.code === "MAIL_RECONNECT_REQUIRED" ? "denied" : e instanceof ApiError && e.code === "MAIL_DIFFERENT_ACCOUNT" ? "wrongAccount" : e instanceof ApiError && e.code === "MAIL_INVALID_HOST" ? "invalidHost" : "error");
    } finally {
      if (pending.current === controller) pending.current = null;
      if (permitted()) { setBusy(false); setForm((old) => old ? { ...old, password: "" } : old); setReload((n) => n + 1); }
    }
  };
  const oauth = (provider: MailProvider, accountId?: string) => run(async (signal) => {
    const result = await mailApi.oauth(provider, accountId, useAppStore.getState().csrfToken, signal);
    if (!permitted() || signal.aborted) return;
    const url = new URL(result.authorizationUrl);
    const expected = provider === "gmail" ? "https://accounts.google.com" : "https://login.microsoftonline.com";
    if (url.origin !== expected) throw new Error("Invalid authorization URL");
    window.location.assign(url.href);
  });
  const connect = (provider: MailProvider) => {
    setEditing(null); setForm(null); setLabel(""); setChosen([]); setNotice(""); setError(""); setRemoving(null);
    if (provider === "gmail" || provider === "outlook") void oauth(provider);
    else setForm(blank(provider));
  };
  const edit = (account: MailAccount, credentials = false) => {
    setEditing(account); setLabel(account.label); setChosen(account.agents.map((a) => a.profileId)); setRemoving(null);
    if (credentials && (account.provider === "imap" || account.provider === "hostinger")) {
      setForm({ ...blank(account.provider), ...account.config, provider: account.provider, address: account.address, label: account.label, username: account.config.username ?? account.address, password: "", accountId: account.id });
    } else setForm(null);
  };
  const save = () => run(async (signal) => {
    const csrf = useAppStore.getState().csrfToken;
    const account = form ? await mailApi.connect({ ...form, username: form.username || form.address, label: form.label || form.address }, csrf, signal) : editing;
    if (!account || !permitted() || signal.aborted) return;
    const updated = await mailApi.update(account.id, form ? form.label || account.address : label || account.address, chosen, useAppStore.getState().csrfToken, signal);
    if (!permitted() || signal.aborted) return;
    setAccounts((old) => [...old.filter((a) => a.id !== updated.id), updated]); setForm(null); setEditing(null); setNotice("updated");
  });
  const openChat = (profileId: string) => run(async () => {
    const session = await api.createSession(profileId, undefined, useAppStore.getState().csrfToken);
    if (!permitted()) return;
    useAppStore.getState().selectProfile(profileId); useAppStore.getState().addSession(session);
    await navigate({ to: "/chats" });
  });

  return <Panel id="plugins" className="settings-section mail-plugins" aria-labelledby="plugins-title">
    <header><Plugs aria-hidden="true" /><div><strong id="plugins-title">{t("plugins.title")}</strong><p>{t("plugins.description")}</p></div></header>
    <div className="mail-plugins__heading mail-plugins__category"><EnvelopeSimple aria-hidden="true" /><h3>{t("plugins.mail")}</h3></div>
    <p>{t("plugins.intro")}</p>
    <div className="mail-plugins__providers">{(["gmail", "outlook", "hostinger", "imap"] as MailProvider[]).map((provider) => {
      const enabled = providers.find((p) => p.id === provider)?.enabled;
      return <div key={provider}><Button variant="secondary" leadingIcon={<Plus />} disabled={disabled || loading || !enabled} onClick={() => connect(provider)}>{t("plugins.connect", { provider: providerName(provider) })}</Button>{!loading && !enabled ? <small>{t("plugins.unavailable")}</small> : null}</div>;
    })}</div>
    <p className="mail-plugins__privacy"><ShieldCheck aria-hidden="true" />{t("plugins.privacy")}</p>
    <p><a href={`/about${["es", "fr", "de", "pt"].includes(i18n.language.split("-")[0]) ? `-${i18n.language.split("-")[0]}` : ""}.html`} target="_blank" rel="noopener noreferrer">Agent Control · JemAI Labs</a></p>
    {(form || editing) && <form className="mail-plugins__form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <fieldset disabled={disabled}><legend>{form ? t("plugins.connect", { provider: providerName(form.provider) }) : t("plugins.edit")}</legend>
        <label>{t("plugins.label")}<input value={form ? form.label : label} maxLength={120} onChange={(e) => form ? field("label", e.target.value) : setLabel(e.target.value)} /></label>
        {form && <>
          <p>{t("plugins.credentials")}</p>
          <label>{t("plugins.address")}<input type="email" required value={form.address} maxLength={320} onChange={(e) => field("address", e.target.value)} autoComplete="email" /></label>
          <label>{t("plugins.username")}<input value={form.username} placeholder={form.address} maxLength={320} onChange={(e) => field("username", e.target.value)} autoComplete="username" /></label>
          <label>{t("plugins.password")}<input type="password" required value={form.password} maxLength={1024} onChange={(e) => field("password", e.target.value)} autoComplete="new-password" /></label>
          {form.provider === "hostinger" ? <label>{t("plugins.service")}<select value={form.service} onChange={(e) => field("service", e.target.value)}><option value="hostinger">Hostinger Email</option><option value="titan">Titan Email</option></select></label> : <>
            <label>{t("plugins.imapHost")}<input required value={form.imapHost} maxLength={253} onChange={(e) => field("imapHost", e.target.value)} placeholder="imap.example.com" /></label>
            <label>{t("plugins.smtpHost")}<input required value={form.smtpHost} maxLength={253} onChange={(e) => field("smtpHost", e.target.value)} placeholder="smtp.example.com" /></label>
          </>}
          <label>{t("plugins.smtpPort")}<select value={form.smtpPort} onChange={(e) => field("smtpPort", Number(e.target.value))}><option value={465}>465 · TLS</option><option value={587}>587 · STARTTLS</option></select></label>
          <p className="form-hint">{t("plugins.imapHint")}</p>
        </>}
        <fieldset className="mail-plugins__agents"><legend>{t("plugins.agents")}</legend>{profiles.length ? profiles.map((profile) => <label key={profile.id}><input type="checkbox" checked={chosen.includes(profile.id)} onChange={(e) => setChosen((old) => e.target.checked ? [...old, profile.id] : old.filter((id) => id !== profile.id))} /><span>{profile.displayName}<small>{gateways.find((g) => g.id === profile.gatewayId)?.name}</small></span></label>) : <p>{t("plugins.noAgents")}</p>}</fieldset>
        <div className="mail-plugins__actions"><Button type="submit">{busy ? t("plugins.connecting") : t("plugins.save")}</Button><Button type="button" variant="ghost" onClick={() => { setForm(null); setEditing(null); }}>{t("plugins.cancel")}</Button></div>
      </fieldset>
    </form>}
    <div className="mail-plugins__heading"><h3>{t("plugins.accounts")}</h3><Button variant="ghost" disabled={disabled || loading} onClick={() => { setError(""); setReload((n) => n + 1); }}>{t("plugins.refresh")}</Button></div>
    {loading ? <p role="status">{t("plugins.loading")}</p> : accounts.length === 0 ? <p>{t("plugins.empty")}</p> : accounts.map((account) => <article className="mail-plugins__account" key={account.id} aria-label={account.address}>
      <div className="mail-plugins__heading"><div><strong>{account.label}</strong><small>{account.address} · {providerName(account.provider)}</small></div><Badge>{stateLabel(account.status)}</Badge></div>
      {account.agents.map((agent) => { const profile = profiles.find((p) => p.id === agent.profileId); return <div className="mail-plugins__assignment" key={agent.profileId}><span>{profile?.displayName ?? t("plugins.unavailableState")} · {stateLabel(agent.state)}</span>{profile && agent.state === "ready" && <Button variant="ghost" disabled={disabled} onClick={() => void openChat(profile.id)}>{t("plugins.open", { agent: profile.displayName })}</Button>}</div>; })}
      <div className="mail-plugins__actions">
        <Button variant="secondary" disabled={disabled} onClick={() => edit(account)}>{t("plugins.edit")}</Button>
        <Button variant="ghost" disabled={disabled} onClick={() => void run(async (signal) => { await mailApi.test(account.id, useAppStore.getState().csrfToken, signal); if (permitted()) setNotice("tested"); })}>{t("plugins.test")}</Button>
        <Button variant="ghost" disabled={disabled || !providers.find((p) => p.id === account.provider)?.enabled} onClick={() => account.provider === "gmail" || account.provider === "outlook" ? void oauth(account.provider, account.id) : edit(account, true)}>{t("plugins.reconnect")}</Button>
        <Button variant="ghost" disabled={disabled} onClick={() => setRemoving(account.id)}>{t("plugins.disconnect")}</Button>
      </div>
      {removing === account.id && <div role="group" aria-label={t("plugins.disconnect")}><p>{t("plugins.confirm", { address: account.address })}</p><div className="mail-plugins__actions"><Button variant="danger" disabled={disabled} onClick={() => void run(async (signal) => { await mailApi.disconnect(account.id, useAppStore.getState().csrfToken, signal); if (permitted()) { setRemoving(null); setAccounts((old) => old.filter((a) => a.id !== account.id)); setNotice("removed"); } })}>{t("plugins.disconnect")}</Button><Button variant="ghost" disabled={disabled} onClick={() => setRemoving(null)}>{t("plugins.cancel")}</Button></div></div>}
    </article>)}
    <p className="form-hint">{t("plugins.activation")}</p>
    {notice && <p role="status">{t(`plugins.${notice}`)}</p>}{error && <p className="form-error" role="alert">{t(`plugins.${error}`)}</p>}{blocked && <p className="form-warning">{t("plugins.offline")}</p>}
  </Panel>;
}
