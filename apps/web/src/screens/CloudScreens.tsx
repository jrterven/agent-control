import { ArrowClockwise, ArrowRight, Desktop, ShieldCheck, WarningCircle } from "@phosphor-icons/react";
import { Badge, Button, Field, Panel, StatusDot } from "@hermes-control/ui";
import { Link } from "@tanstack/react-router";
import type { ConnectorList, ConnectorPairing, ConnectorView } from "@hermes-control/shared-types";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, ApiError } from "../lib/api";
import { formatConversationTimestamp } from "../lib/dateTime";
import { useCloudConfigurationStore } from "../lib/cloud";
import { useAppStore } from "../store/appStore";
import { CloudInstallerOptions } from "../components/CloudInstallerOptions";
import { ConnectorReadiness } from "../components/ConnectorReadiness";

async function refreshAccountBootstrap(csrfToken?: string) {
  const projection = await api.bootstrap();
  const current = useAppStore.getState();
  // Do not hydrate a response from an account that signed out while it loaded.
  if (current.authState === "authenticated" && current.csrfToken === csrfToken) current.hydrateBootstrap(projection);
}

export function CloudEmptyState() {
  const { t } = useTranslation();
  return <div className="empty-chat cloud-empty-state">
    <Desktop size={48} aria-hidden="true" />
    <h2>{t("cloud.empty")}</h2><p>{t("cloud.emptyDescription")}</p>
    <Link className="hc-button hc-button--primary hc-button--md" to="/connect">{t("cloud.connect")}<ArrowRight aria-hidden="true" /></Link>
    <p className="cloud-privacy">{t("cloud.offlineHint")}</p>
  </div>;
}

export function CloudSettingsPanel() {
  const { t } = useTranslation();
  const cloud = useCloudConfigurationStore((state) => state.methods?.mode === "cloud");
  if (!cloud) return null;
  return <Panel className="settings-section">
    <header><Desktop aria-hidden="true" /><div><strong>{t("cloud.title")}</strong><p>{t("cloud.description")}</p></div></header>
    <Link className="hc-button hc-button--secondary hc-button--md" to="/computers">{t("cloud.settingsLink")}</Link>
    <p className="cloud-privacy"><ShieldCheck aria-hidden="true" /> {t("cloud.privacy")}</p>
    <h3>{t("cloud.installPwa")}</h3><p>{t("cloud.installPwaDescription")}</p>
  </Panel>;
}

function pairingErrorKey(error: unknown) {
  if (error instanceof ApiError) {
    if (error.status === 404 || error.status === 410) return "cloud.invalidCode";
    if (error.status === 409) return "cloud.usedCode";
  }
  return "cloud.pairingError";
}

export function ConnectorPairingForm({ initialCode = "" }: { initialCode?: string }) {
  const { t, i18n } = useTranslation();
  const csrfToken = useAppStore((state) => state.csrfToken);
  const timeZone = useAppStore((state) => state.timeZone);
  const offline = useAppStore((state) => state.authState !== "authenticated");
  const [code, setCode] = useState(initialCode.slice(0, 20));
  const [pairing, setPairing] = useState<ConnectorPairing | null>(null);
  const [profiles, setProfiles] = useState<string[]>([]);
  const [busy, setBusy] = useState<"inspect" | "approve" | null>(null);
  const [error, setError] = useState("");
  const [connected, setConnected] = useState<ConnectorView | null>(null);
  const [now, setNow] = useState(Date.now());
  const expiresAt = pairing ? Date.parse(pairing.expiresAt) : Infinity;
  const expired = pairing !== null && (!Number.isFinite(expiresAt) || expiresAt <= now);

  useEffect(() => {
    if (!pairing) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1_000);
    return () => window.clearInterval(timer);
  }, [pairing]);

  const inspect = async () => {
    if (!code.trim() || busy || offline) return;
    setBusy("inspect"); setError(""); setPairing(null);
    try {
      const result = await api.inspectConnectorPairing(code.trim().toUpperCase(), csrfToken);
      setNow(Date.now()); setPairing(result); setProfiles([...result.profiles]);
    } catch (cause) { setError(pairingErrorKey(cause)); }
    finally { setBusy(null); }
  };
  const approve = async () => {
    if (!pairing || busy || expired || offline) return;
    if (!profiles.length) { setError("cloud.selectProfile"); return; }
    setBusy("approve"); setError("");
    try {
      const result = await api.approveConnectorPairing(pairing.code, profiles, csrfToken);
      const current = useAppStore.getState();
      if (current.authState !== "authenticated" || current.csrfToken !== csrfToken) return;
      setConnected(result); setPairing(null); setCode("");
    } catch (cause) { setError(pairingErrorKey(cause)); }
    finally { setBusy(null); }
  };

  if (connected) return <ConnectorReadiness computer={connected} />;

  return <Panel className="settings-section connector-pairing">
    <h2 className="connector-step-heading">{t("cloud.codeTitle")}</h2><p>{t("cloud.codeDescription")}</p>
    <form onSubmit={(event) => { event.preventDefault(); void inspect(); }}>
      <Field label={t("cloud.code")} value={code} maxLength={20} autoComplete="off" autoCapitalize="characters" spellCheck={false} disabled={Boolean(busy)} onChange={(event) => { setCode(event.target.value); setPairing(null); setError(""); }} />
      <Button type="submit" disabled={!code.trim() || Boolean(busy) || offline}>{t(busy === "inspect" ? "cloud.reviewing" : "cloud.inspect")}</Button>
    </form>
    {error ? <p className="form-error" role="alert">{t(error)}</p> : null}
    {pairing ? <div className="connector-review">
      <h3><Desktop aria-hidden="true" /> {pairing.name}</h3>
      <p>{t("cloud.confirmHint")}</p>
      {expired ? <p className="form-error" role="alert">{t("cloud.invalidCode")}</p> : <p>{t("cloud.expires", { time: formatConversationTimestamp(pairing.expiresAt, i18n.resolvedLanguage ?? i18n.language, timeZone) })}</p>}
      <fieldset disabled={Boolean(busy) || expired || offline}>
        <legend>{t("cloud.profiles")}</legend>
        {pairing.profiles.map((profile) => <label className="connector-profile" key={profile}>
          <input type="checkbox" checked={profiles.includes(profile)} onChange={(event) => setProfiles((selected) => event.target.checked ? [...selected, profile] : selected.filter((item) => item !== profile))} />
          <span>{profile}</span>
        </label>)}
      </fieldset>
      {!profiles.length ? <p>{t("cloud.selectProfile")}</p> : null}
      <Button variant="primary" disabled={Boolean(busy) || expired || !profiles.length || offline} onClick={() => void approve()}>{t(busy === "approve" ? "cloud.approving" : "cloud.approve")}</Button>
    </div> : null}
  </Panel>;
}

export function ConnectorsScreen({ pairing = false }: { pairing?: boolean }) {
  const { t, i18n } = useTranslation();
  const cloud = useCloudConfigurationStore((state) => state.methods?.mode === "cloud");
  const openRegistration = useCloudConfigurationStore((state) => state.methods?.registrationMode === "open");
  const csrfToken = useAppStore((state) => state.csrfToken);
  const timeZone = useAppStore((state) => state.timeZone);
  const offline = useAppStore((state) => state.authState !== "authenticated");
  const [data, setData] = useState<ConnectorList | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [revokeId, setRevokeId] = useState<string | null>(null);
  const [revoking, setRevoking] = useState(false);
  const initialCode = new URLSearchParams(window.location.search).get("code") ?? "";

  useEffect(() => {
    if (!cloud || offline) { setLoading(false); return; }
    let active = true;
    let inFlight = false;
    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const result = await api.connectors();
        if (active) { setData(result); setError(""); }
      } catch { if (active) setError("cloud.loadError"); }
      finally { inFlight = false; if (active) setLoading(false); }
    };
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState !== "hidden") void load(); }, 15_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [cloud, csrfToken, offline, revision]);

  const revoke = async () => {
    if (!revokeId || revoking || offline) return;
    setRevoking(true); setError("");
    try {
      await api.revokeConnector(revokeId, csrfToken);
      setData((current) => current ? { ...current, items: current.items.map((item) => item.id === revokeId ? { ...item, status: "revoked" } : item) } : current);
      setRevokeId(null); setRevision((value) => value + 1);
      await refreshAccountBootstrap(csrfToken).catch(() => undefined);
    } catch { setError("cloud.revokeError"); }
    finally { setRevoking(false); }
  };
  if (!cloud) return <div className="page-wrap"><p>{t("cloud.privateOnly")}</p></div>;
  return <div className="page-wrap cloud-page">
    <header className="page-header"><div><span className="eyebrow">{t(openRegistration ? "cloud.publicBeta" : "cloud.beta")}</span><h1>{t(pairing ? "cloud.connect" : "cloud.title")}</h1><p>{t("cloud.description")}</p></div>
      {pairing ? <Link to="/computers" className="hc-button hc-button--ghost hc-button--md">{t("cloud.title")}</Link> : <Link to="/connect" className="hc-button hc-button--primary hc-button--md">{t("cloud.connect")}</Link>}
    </header>
    <p className="cloud-privacy"><ShieldCheck aria-hidden="true" /> {t("cloud.privacy")}</p>
    <p>{t("cloud.offlineHint")}</p>
    {error ? <p className="form-error" role="alert"><WarningCircle aria-hidden="true" /> {t(error)} <Button size="sm" variant="ghost" onClick={() => setRevision((value) => value + 1)} disabled={offline}>{t("cloud.retry")}</Button></p> : null}
    {loading ? <p role="status">{t("cloud.loading")}</p> : null}
    {pairing ? <div className="settings-layout">
      <CloudInstallerOptions existingCommand={data?.installCommand} loading={loading} initialExisting={Boolean(initialCode)} />
      <ConnectorPairingForm initialCode={initialCode} />
      <Panel className="settings-section"><h2>{t("cloud.installPwa")}</h2><p>{t("cloud.installPwaDescription")}</p></Panel>
    </div> : <>
      <Button size="sm" variant="ghost" leadingIcon={<ArrowClockwise aria-hidden="true" />} disabled={offline || loading} onClick={() => setRevision((value) => value + 1)}>{t("cloud.refresh")}</Button>
      {data?.items.length === 0 ? <CloudEmptyState /> : null}
      <div className="connector-grid">{data?.items.map((item) => <Panel className="settings-section connector-card" key={item.id}>
        <header><Desktop aria-hidden="true" /><div><strong>{item.name}</strong><p><StatusDot tone={item.status === "online" ? "positive" : "warning"} /> {t(`cloud.${item.status}`)}</p></div></header>
        <dl><div><dt>{t("cloud.version")}</dt><dd>{item.version ?? t("cloud.unknown")}</dd></div><div><dt>{t("cloud.lastSeen")}</dt><dd>{item.lastSeenAt ? formatConversationTimestamp(item.lastSeenAt, i18n.resolvedLanguage ?? i18n.language, timeZone) : t("cloud.neverSeen")}</dd></div></dl>
        {item.installationKind || item.hermesVersion ? <dl>
          {item.installationKind === "managed" || item.installationKind === "existing" ? <div><dt>{t("onboarding.installationKind")}</dt><dd>{t(item.installationKind === "managed" ? "onboarding.kindManaged" : "onboarding.kindExisting")}</dd></div> : null}
          {item.hermesVersion ? <div><dt>{t("onboarding.hermesVersion")}</dt><dd>{item.hermesVersion}</dd></div> : null}
        </dl> : null}
        <div className="connector-profiles">{item.profiles.map((profile) => <Badge key={profile}>{profile}</Badge>)}</div>
        {item.status !== "revoked" ? revokeId === item.id ? <div className="connector-revoke"><p>{t("cloud.revokeConfirm", { name: item.name })}</p><Button variant="danger" disabled={revoking || offline} onClick={() => void revoke()}>{t("cloud.confirmRevoke")}</Button><Button variant="ghost" disabled={revoking} onClick={() => setRevokeId(null)}>{t("cloud.cancel")}</Button></div> : <Button variant="ghost" disabled={revoking || offline} onClick={() => setRevokeId(item.id)}>{t("cloud.revoke")}</Button> : null}
      </Panel>)}</div>
    </>}
  </div>;
}
