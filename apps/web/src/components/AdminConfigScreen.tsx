import {
  Brain,
  ChartLineUp,
  CheckCircle,
  Key,
  ArrowsLeftRight,
  HardDrives,
  PlugsConnected,
  Robot,
  ShieldCheck,
  SpinnerGap,
  Trash,
  Wrench,
  WarningCircle,
} from "@phosphor-icons/react";
import { Badge, Button, Panel, Switch } from "@hermes-control/ui";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, ApiError, type AdminResourceName, type AdminResourceView } from "../lib/api";
import { clearSessionLocalData, invalidatePrivateSnapshots } from "../lib/db";
import { useOverlayDialog } from "../lib/useOverlayDialog";
import { useAppStore } from "../store/appStore";
import type { Gateway, Profile } from "../types";

type AdminTab = "general" | "identity" | "tools" | "integrations" | "secrets" | "management";
type SnapshotMap = Partial<Record<AdminResourceName, Record<string, unknown>>>;
type Route = { gatewayId: string; profileName: string };

const tabDefinitions: Array<{
  id: AdminTab;
  methods: string[];
}> = [
  { id: "general", methods: ["models.list", "config.get", "usage.get"] },
  { id: "identity", methods: ["soul.get"] },
  { id: "tools", methods: ["skills.list", "toolsets.list"] },
  { id: "integrations", methods: ["mcp.list", "channels.list"] },
  { id: "secrets", methods: ["secrets.list"] },
  { id: "management", methods: [] },
];

const resourcesByTab: Record<AdminTab, AdminResourceName[]> = {
  general: ["models", "config", "usage"],
  identity: ["soul"],
  tools: ["skills", "toolsets"],
  integrations: ["mcp", "channels"],
  secrets: ["secrets"],
  management: [],
};

const readMethods: Record<AdminResourceName, string> = {
  models: "models.list",
  config: "config.get",
  soul: "soul.get",
  skills: "skills.list",
  toolsets: "toolsets.list",
  mcp: "mcp.list",
  channels: "channels.list",
  usage: "usage.get",
  secrets: "secrets.list",
};

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function recordsFrom(data: Record<string, unknown> | undefined, key?: string): Array<Record<string, unknown>> {
  const candidate = key ? data?.[key] : data?.items;
  return Array.isArray(candidate) ? candidate.map(asRecord) : [];
}

function stringValue(value: unknown, fallback = "") {
  return typeof value === "string" ? value : fallback;
}

function booleanValue(value: unknown) {
  return value === true;
}

type LifecycleCapability = "profileDelete" | "profileExport" | "profileImport" | "profileTransfer";

const lifecycleMethods: Record<LifecycleCapability, string> = {
  profileDelete: "profiles.delete",
  profileExport: "profiles.export",
  profileImport: "profiles.import",
  profileTransfer: "profiles.transfer",
};

function hasLifecycleCapability(profile: Profile | undefined, capability: LifecycleCapability) {
  return Boolean(
    profile?.capabilities?.[capability]
    && profile.capabilitySet?.methods.includes(lifecycleMethods[capability]),
  );
}

function lifecycleIsAdvertised(profile: Profile | undefined) {
  return (Object.keys(lifecycleMethods) as LifecycleCapability[])
    .some((capability) => hasLifecycleCapability(profile, capability));
}

function displayNumber(value: unknown, locale: string) {
  return typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat(locale).format(value) : "—";
}

async function readResource(route: Route, resource: AdminResourceName): Promise<AdminResourceView> {
  if (resource === "models") return api.adminModels(route.gatewayId, route.profileName);
  if (resource === "config") return api.adminConfig(route.gatewayId, route.profileName);
  if (resource === "soul") return api.adminSoul(route.gatewayId, route.profileName);
  if (resource === "skills") return api.adminSkills(route.gatewayId, route.profileName);
  if (resource === "toolsets") return api.adminToolsets(route.gatewayId, route.profileName);
  if (resource === "mcp") return api.adminMcpServers(route.gatewayId, route.profileName);
  if (resource === "channels") return api.adminChannels(route.gatewayId, route.profileName);
  if (resource === "usage") return api.adminUsage(route.gatewayId, route.profileName);
  return api.adminSecrets(route.gatewayId, route.profileName);
}

function SectionHeader({ icon, title, description, badge }: { icon: React.ReactNode; title: string; description: string; badge?: string }) {
  return <header className="admin-section__header"><span>{icon}</span><div><h2>{title}</h2><p>{description}</p></div>{badge ? <Badge>{badge}</Badge> : null}</header>;
}

function EmptySection({ children }: { children: React.ReactNode }) {
  return <div className="admin-empty"><ShieldCheck size={22} /><p>{children}</p></div>;
}

function ModelsSection({ data, canWrite, busy, onSave }: {
  data?: Record<string, unknown>;
  canWrite: boolean;
  busy: boolean;
  onSave: (provider: string, model: string, confirmExpensiveModel: boolean) => Promise<boolean>;
}) {
  const { t } = useTranslation();
  const current = asRecord(data?.current);
  const providers = recordsFrom(data, "providers");
  const [provider, setProvider] = useState("");
  const [model, setModel] = useState("");
  const [confirmed, setConfirmed] = useState(false);

  useEffect(() => {
    const nextProvider = stringValue(current.provider, stringValue(providers[0]?.id));
    const selected = providers.find((item) => stringValue(item.id) === nextProvider);
    const models = Array.isArray(selected?.models) ? selected.models.filter((item): item is string => typeof item === "string") : [];
    setProvider(nextProvider);
    setModel(stringValue(current.model, models[0] ?? ""));
    setConfirmed(false);
  }, [data]);

  const selectedProvider = providers.find((item) => stringValue(item.id) === provider);
  const models = Array.isArray(selectedProvider?.models) ? selectedProvider.models.filter((item): item is string => typeof item === "string") : [];
  const changeProvider = (next: string) => {
    setProvider(next);
    const nextProvider = providers.find((item) => stringValue(item.id) === next);
    const nextModels = Array.isArray(nextProvider?.models) ? nextProvider.models.filter((item): item is string => typeof item === "string") : [];
    setModel(nextModels[0] ?? "");
  };

  return <Panel className="admin-section">
    <SectionHeader icon={<Brain weight="duotone" />} title={t("adminConfig.models.title")} description={t("adminConfig.models.description")} badge={canWrite ? t("adminConfig.common.editable") : t("adminConfig.common.readOnly")} />
    {providers.length ? <form className="admin-form-grid" onSubmit={(event) => { event.preventDefault(); void onSave(provider, model, confirmed); }}>
      <label className="hc-field"><span className="hc-field__label">{t("adminConfig.models.provider")}</span><select value={provider} onChange={(event) => changeProvider(event.target.value)} disabled={!canWrite || busy}>{providers.map((item) => <option key={stringValue(item.id)} value={stringValue(item.id)}>{stringValue(item.label, stringValue(item.id))}</option>)}</select></label>
      <label className="hc-field"><span className="hc-field__label">{t("adminConfig.models.model")}</span><select value={model} onChange={(event) => setModel(event.target.value)} disabled={!canWrite || busy}>{models.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
      {canWrite ? <label className="admin-check"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>{t("adminConfig.models.confirmExpensive")}</span></label> : null}
      {canWrite ? <Button type="submit" variant="primary" disabled={busy || !provider || !model}>{t("adminConfig.models.save")}</Button> : null}
    </form> : <EmptySection>{t("adminConfig.models.empty")}</EmptySection>}
  </Panel>;
}

function ConfigDocumentSection({ data, canWrite, busy, onSave }: { data?: Record<string, unknown>; canWrite: boolean; busy: boolean; onSave: (config: Record<string, unknown>) => Promise<boolean> }) {
  const { t } = useTranslation();
  const document = Object.keys(asRecord(data?.config)).length ? asRecord(data?.config) : (data ?? {});
  const [value, setValue] = useState("");
  const [error, setError] = useState("");
  useEffect(() => { setValue(JSON.stringify(document, null, 2)); setError(""); }, [data]);
  const save = async () => {
    try {
      const parsed = JSON.parse(value) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
      setError("");
      await onSave(parsed as Record<string, unknown>);
    } catch {
      setError(t("adminConfig.config.invalidJson"));
    }
  };
  return <Panel className="admin-section">
    <SectionHeader icon={<Robot weight="duotone" />} title={t("adminConfig.config.title")} description={t("adminConfig.config.description")} badge={canWrite ? t("adminConfig.config.advanced") : t("adminConfig.common.readOnly")} />
    <label className="hc-field"><span className="hc-field__label">{t("adminConfig.config.json")}</span><textarea className="admin-code-editor" spellCheck={false} value={value} readOnly={!canWrite} onChange={(event) => setValue(event.target.value)} aria-describedby={error ? "config-json-error" : undefined} /></label>
    {error ? <p id="config-json-error" className="form-error" role="alert"><WarningCircle /> {error}</p> : null}
    {canWrite ? <div className="admin-section__actions"><Button variant="primary" disabled={busy} onClick={() => void save()}>{t("adminConfig.config.apply")}</Button></div> : null}
  </Panel>;
}

function UsageSection({ data }: { data?: Record<string, unknown> }) {
  const { t, i18n } = useTranslation();
  const totals = asRecord(data?.totals);
  const locale = i18n.resolvedLanguage ?? i18n.language;
  return <Panel className="admin-section">
    <SectionHeader icon={<ChartLineUp weight="duotone" />} title={t("adminConfig.usage.title")} description={t("adminConfig.usage.description", { days: displayNumber(data?.period_days, locale) })} />
    <dl className="admin-metrics"><div><dt>{t("adminConfig.usage.input")}</dt><dd>{displayNumber(totals.total_input, locale)}</dd></div><div><dt>{t("adminConfig.usage.output")}</dt><dd>{displayNumber(totals.total_output, locale)}</dd></div><div><dt>{t("adminConfig.usage.sessions")}</dt><dd>{displayNumber(totals.total_sessions, locale)}</dd></div></dl>
  </Panel>;
}

function SoulSection({ data, canWrite, busy, onSave }: { data?: Record<string, unknown>; canWrite: boolean; busy: boolean; onSave: (content: string) => Promise<boolean> }) {
  const { t } = useTranslation();
  const [content, setContent] = useState("");
  useEffect(() => setContent(stringValue(data?.content)), [data]);
  return <Panel className="admin-section">
    <SectionHeader icon={<Robot weight="duotone" />} title={t("adminConfig.soul.title")} description={t("adminConfig.soul.description")} badge={canWrite ? t("adminConfig.common.editable") : t("adminConfig.common.readOnly")} />
    <label className="hc-field"><span className="hc-field__label">{t("adminConfig.soul.content")}</span><textarea className="admin-soul-editor" value={content} readOnly={!canWrite} onChange={(event) => setContent(event.target.value)} /></label>
    {canWrite ? <div className="admin-section__actions"><Button variant="primary" disabled={busy} onClick={() => void onSave(content)}>{t("adminConfig.soul.save")}</Button></div> : null}
  </Panel>;
}

function ToggleCollection({ title, description, icon, rows, canToggle, busy, onToggle }: {
  title: string;
  description: string;
  icon: React.ReactNode;
  rows: Array<Record<string, unknown>>;
  canToggle: boolean;
  busy: boolean;
  onToggle: (name: string, enabled: boolean) => Promise<boolean>;
}) {
  const { t } = useTranslation();
  return <Panel className="admin-section"><SectionHeader icon={icon} title={title} description={description} badge={canToggle ? t("adminConfig.common.editable") : t("adminConfig.common.readOnly")} />
    {rows.length ? <div className="admin-toggle-list">{rows.map((item) => {
      const name = stringValue(item.name, stringValue(item.id));
      return <Switch key={name} checked={booleanValue(item.enabled)} disabled={!canToggle || busy} onChange={(enabled) => void onToggle(name, enabled)} label={stringValue(item.label, name)} description={stringValue(item.description, stringValue(item.category))} />;
    })}</div> : <EmptySection>{t("adminConfig.collections.empty")}</EmptySection>}
  </Panel>;
}

function McpSection({ data, methods, busy, onAction }: {
  data?: Record<string, unknown>;
  methods: Set<string>;
  busy: boolean;
  onAction: (action: () => Promise<unknown>, message: string) => Promise<boolean>;
}) {
  const { t } = useTranslation();
  const rows = recordsFrom(data, "servers");
  const route = useAdminRoute();
  const csrfToken = useAppStore((state) => state.csrfToken);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [command, setCommand] = useState("");
  const [bearerToken, setBearerToken] = useState("");
  const [validation, setValidation] = useState("");
  const create = async () => {
    if (!route) return;
    if (!name.trim() || Boolean(url.trim()) === Boolean(command.trim())) {
      setValidation(t("adminConfig.mcp.validation"));
      setBearerToken("");
      return;
    }
    setValidation("");
    try {
      const ok = await onAction(() => api.adminCreateMcpServer(route.gatewayId, route.profileName, {
        name: name.trim(),
        ...(url.trim() ? { url: url.trim() } : { command: command.trim() }),
        ...(bearerToken ? { bearerToken } : {}),
        enabled: true,
      }, csrfToken), t("adminConfig.mcp.added"));
      if (ok) { setName(""); setUrl(""); setCommand(""); }
    } finally {
      setBearerToken("");
    }
  };
  return <Panel className="admin-section"><SectionHeader icon={<PlugsConnected weight="duotone" />} title={t("adminConfig.mcp.title")} description={t("adminConfig.mcp.description")} badge={methods.has("mcp.create") ? t("adminConfig.common.editable") : t("adminConfig.common.readOnly")} />
    {rows.length ? <div className="admin-resource-list">{rows.map((item) => {
      const itemName = stringValue(item.name);
      return <div key={itemName} className="admin-resource-row"><div><strong>{itemName}</strong><small>{stringValue(item.url, stringValue(item.command, t("adminConfig.mcp.managedTransport")))}</small></div><Badge tone={booleanValue(item.configured) ? "positive" : "warning"}>{booleanValue(item.configured) ? t("adminConfig.common.configured") : t("adminConfig.common.incomplete")}</Badge><div className="admin-row-actions">
        {methods.has("mcp.toggle") ? <Switch checked={booleanValue(item.enabled)} disabled={busy} onChange={(enabled) => route && void onAction(() => api.adminToggleMcpServer(route.gatewayId, route.profileName, itemName, enabled, csrfToken), t("adminConfig.common.updated", { name: itemName }))} label={t("adminConfig.mcp.activate", { name: itemName })} /> : null}
        {methods.has("mcp.test") ? <Button size="sm" variant="ghost" disabled={busy} onClick={() => route && void onAction(() => api.adminTestMcpServer(route.gatewayId, route.profileName, itemName, csrfToken), t("adminConfig.common.testComplete", { name: itemName }))}>{t("adminConfig.mcp.test")}</Button> : null}
        {methods.has("mcp.delete") ? <Button size="sm" variant="danger" disabled={busy} onClick={() => { if (route && window.confirm(t("adminConfig.mcp.deleteConfirm", { name: itemName }))) void onAction(() => api.adminDeleteMcpServer(route.gatewayId, route.profileName, itemName, csrfToken), t("adminConfig.common.deleted", { name: itemName })); }}>{t("adminConfig.mcp.delete")}</Button> : null}
      </div></div>;
    })}</div> : <EmptySection>{t("adminConfig.mcp.empty")}</EmptySection>}
    {methods.has("mcp.create") ? <div className="admin-create-box"><h3>{t("adminConfig.mcp.addTitle")}</h3><div className="admin-form-grid"><label className="hc-field"><span className="hc-field__label">{t("adminConfig.mcp.name")}</span><input className="hc-input" value={name} onChange={(event) => setName(event.target.value)} /></label><label className="hc-field"><span className="hc-field__label">{t("adminConfig.mcp.url")}</span><input className="hc-input" type="url" value={url} onChange={(event) => { setUrl(event.target.value); if (event.target.value) setCommand(""); }} /></label><label className="hc-field"><span className="hc-field__label">{t("adminConfig.mcp.localCommand")}</span><input className="hc-input" value={command} onChange={(event) => { setCommand(event.target.value); if (event.target.value) setUrl(""); }} /></label><label className="hc-field"><span className="hc-field__label">{t("adminConfig.mcp.bearerToken")}</span><input className="hc-input" type="password" autoComplete="new-password" value={bearerToken} onChange={(event) => setBearerToken(event.target.value)} /></label></div>{validation ? <p className="form-error" role="alert"><WarningCircle /> {validation}</p> : null}<Button variant="primary" disabled={busy} onClick={() => void create()}>{t("adminConfig.mcp.add")}</Button></div> : null}
  </Panel>;
}

function ChannelRow({ item, methods, busy, onAction }: { item: Record<string, unknown>; methods: Set<string>; busy: boolean; onAction: (action: () => Promise<unknown>, message: string) => Promise<boolean> }) {
  const { t } = useTranslation();
  const route = useAdminRoute();
  const csrfToken = useAppStore((state) => state.csrfToken);
  const name = stringValue(item.id, stringValue(item.name));
  const label = stringValue(item.name, name);
  const envRows = Array.isArray(item.env_vars) ? item.env_vars.map(asRecord) : [];
  const [envValues, setEnvValues] = useState<Record<string, string>>({});
  const saveEnv = async () => {
    if (!route) return;
    const payload = Object.fromEntries(Object.entries(envValues).filter(([, value]) => value.length > 0));
    try {
      if (Object.keys(payload).length) await onAction(() => api.adminUpdateChannel(route.gatewayId, route.profileName, name, { env: payload }, csrfToken), t("adminConfig.common.updated", { name: label }));
    } finally {
      setEnvValues({});
    }
  };
  return <div className="admin-channel"><div className="admin-channel__head"><div><strong>{label}</strong><small>{stringValue(item.state, booleanValue(item.configured) ? t("adminConfig.common.configured") : t("adminConfig.channels.requiresConfiguration"))}</small></div>{methods.has("channels.update") ? <Switch checked={booleanValue(item.enabled)} disabled={busy} onChange={(enabled) => route && void onAction(() => api.adminUpdateChannel(route.gatewayId, route.profileName, name, { enabled }, csrfToken), t("adminConfig.common.updated", { name: label }))} label={t("adminConfig.channels.activate", { name: label })} /> : <Badge tone={booleanValue(item.enabled) ? "positive" : "neutral"}>{booleanValue(item.enabled) ? t("adminConfig.common.active") : t("adminConfig.common.inactive")}</Badge>}</div>
    {envRows.length ? <div className="admin-env-list">{envRows.map((field) => { const key = stringValue(field.key); return <div key={key}><label className="hc-field"><span className="hc-field__label">{t("adminConfig.channels.writeOnly", { key })}</span><input className="hc-input" type="password" autoComplete="new-password" placeholder={booleanValue(field.is_set) ? t("adminConfig.channels.configuredReplace") : t("adminConfig.channels.unconfigured")} value={envValues[key] ?? ""} onChange={(event) => setEnvValues((current) => ({ ...current, [key]: event.target.value }))} /></label>{methods.has("channels.update") && booleanValue(field.is_set) ? <Button size="sm" variant="ghost" disabled={busy} onClick={() => { if (route && window.confirm(t("adminConfig.channels.clearConfirm", { key, name: label }))) void onAction(() => api.adminUpdateChannel(route.gatewayId, route.profileName, name, { clearEnv: [key] }, csrfToken), t("adminConfig.common.deleted", { name: key })); }}>{t("adminConfig.channels.clearValue")}</Button> : null}</div>; })}</div> : null}
    <div className="admin-row-actions">{methods.has("channels.update") && envRows.length ? <Button size="sm" variant="primary" disabled={busy || !Object.values(envValues).some(Boolean)} onClick={() => void saveEnv()}>{t("adminConfig.channels.saveCredentials")}</Button> : null}{methods.has("channels.test") ? <Button size="sm" variant="ghost" disabled={busy} onClick={() => route && void onAction(() => api.adminTestChannel(route.gatewayId, route.profileName, name, csrfToken), t("adminConfig.common.testComplete", { name: label }))}>{t("adminConfig.channels.test")}</Button> : null}</div>
  </div>;
}

function ChannelsSection({ data, methods, busy, onAction }: { data?: Record<string, unknown>; methods: Set<string>; busy: boolean; onAction: (action: () => Promise<unknown>, message: string) => Promise<boolean> }) {
  const { t } = useTranslation();
  const rows = recordsFrom(data, "platforms");
  return <Panel className="admin-section"><SectionHeader icon={<PlugsConnected weight="duotone" />} title={t("adminConfig.channels.title")} description={t("adminConfig.channels.description")} badge={methods.has("channels.update") ? t("adminConfig.common.editable") : t("adminConfig.common.readOnly")} />{rows.length ? <div className="admin-resource-list">{rows.map((item) => <ChannelRow key={stringValue(item.id, stringValue(item.name))} item={item} methods={methods} busy={busy} onAction={onAction} />)}</div> : <EmptySection>{t("adminConfig.channels.empty")}</EmptySection>}</Panel>;
}

function SecretRow({ item, canSet, canDelete, busy, onSet, onDelete }: {
  item: Record<string, unknown>;
  canSet: boolean;
  canDelete: boolean;
  busy: boolean;
  onSet: (name: string, value: string) => Promise<boolean>;
  onDelete: (name: string) => Promise<boolean>;
}) {
  const { t } = useTranslation();
  const name = stringValue(item.name);
  const [value, setValue] = useState("");
  const save = async () => {
    try { if (value) await onSet(name, value); } finally { setValue(""); }
  };
  return <div className="admin-secret-row"><div><strong>{name}</strong><small>{stringValue(item.description, t("adminConfig.secrets.managedValue"))}</small></div><Badge tone={booleanValue(item.configured) ? "positive" : "warning"}>{booleanValue(item.configured) ? t("adminConfig.common.configured") : t("adminConfig.channels.unconfigured")}</Badge>{canSet ? <label className="hc-field"><span className="hc-field__label">{t("adminConfig.secrets.newValue", { name })}</span><input className="hc-input" type="password" autoComplete="new-password" value={value} onChange={(event) => setValue(event.target.value)} /></label> : null}<div className="admin-row-actions">{canSet ? <Button size="sm" variant="primary" disabled={busy || !value} onClick={() => void save()}>{t("adminConfig.secrets.save")}</Button> : null}{canDelete && booleanValue(item.configured) ? <Button size="sm" variant="danger" disabled={busy} onClick={() => { if (window.confirm(t("adminConfig.secrets.deleteConfirm", { name }))) void onDelete(name); }}>{t("adminConfig.secrets.delete")}</Button> : null}</div></div>;
}

function SecretsSection({ data, methods, busy, onSet, onDelete }: { data?: Record<string, unknown>; methods: Set<string>; busy: boolean; onSet: (name: string, value: string) => Promise<boolean>; onDelete: (name: string) => Promise<boolean> }) {
  const { t } = useTranslation();
  const rows = recordsFrom(data);
  return <Panel className="admin-section admin-secrets"><SectionHeader icon={<Key weight="duotone" />} title={t("adminConfig.secrets.title")} description={t("adminConfig.secrets.description")} badge={t("adminConfig.common.protected")} />{rows.length ? <div className="admin-resource-list">{rows.map((item) => <SecretRow key={stringValue(item.name)} item={item} canSet={methods.has("secrets.set")} canDelete={methods.has("secrets.delete")} busy={busy} onSet={onSet} onDelete={onDelete} />)}</div> : <EmptySection>{t("adminConfig.secrets.empty")}</EmptySection>}</Panel>;
}

function AgentManagementSection({
  profile,
  gateway,
  canMove,
  canDelete,
  protectedProfile,
  lifecycleBlocked,
  destinations,
  busy,
  onMove,
  onDelete,
}: {
  profile: Profile;
  gateway?: Gateway;
  canMove: boolean;
  canDelete: boolean;
  protectedProfile: boolean;
  lifecycleBlocked: boolean;
  destinations: Gateway[];
  busy: boolean;
  onMove: () => void;
  onDelete: () => void;
}) {
  const { t } = useTranslation();
  return <Panel className="admin-section agent-lifecycle-section">
    <SectionHeader
      icon={<HardDrives weight="duotone" />}
      title={t("adminConfig.management.title")}
      description={t("adminConfig.management.description")}
      badge={protectedProfile ? t("adminConfig.management.protected") : t("adminConfig.management.managed")}
    />
    <dl className="agent-lifecycle-route">
      <div><dt>{t("adminConfig.management.agent")}</dt><dd>{profile.displayName} · <code>{profile.technicalName}</code></dd></div>
      <div><dt>{t("adminConfig.management.currentGateway")}</dt><dd>{gateway?.name ?? profile.gatewayId}</dd></div>
    </dl>
    <div className="agent-lifecycle-disclosure"><ShieldCheck weight="duotone" /><p>{t("adminConfig.management.transferDisclosure")}</p></div>
    {protectedProfile ? <div className="agent-lifecycle-protected" role="status"><ShieldCheck weight="fill" /><p>{t("adminConfig.management.defaultProtected")}</p></div> : null}
    {lifecycleBlocked ? <div className="agent-lifecycle-protected" role="status"><WarningCircle weight="fill" /><p>{t("adminConfig.management.reconcileBlocked")}</p></div> : null}
    <div className="agent-lifecycle-actions">
      <section>
        <span><ArrowsLeftRight weight="duotone" /></span>
        <div><h3>{t("adminConfig.management.moveTitle")}</h3><p>{destinations.length ? t("adminConfig.management.moveDescription") : t("adminConfig.management.noDestination")}</p></div>
        <Button variant="secondary" disabled={!canMove || busy} onClick={onMove}>{t("adminConfig.management.move")}</Button>
      </section>
      <section className="is-danger">
        <span><Trash weight="duotone" /></span>
        <div><h3>{t("adminConfig.management.deleteTitle")}</h3><p>{t("adminConfig.management.deleteDescription")}</p></div>
        <Button variant="danger" disabled={!canDelete || busy} onClick={onDelete}>{t("adminConfig.management.delete")}</Button>
      </section>
    </div>
  </Panel>;
}

function useAdminRoute(): Route | undefined {
  const gatewayId = useAppStore((state) => state.selectedGatewayId);
  const profileId = useAppStore((state) => state.selectedProfileId);
  const profile = useAppStore((state) => state.profiles.find((item) => item.id === profileId));
  return gatewayId && profile?.technicalName ? { gatewayId, profileName: profile.technicalName } : undefined;
}

export function AdminConfigScreen({ header }: { header: React.ReactNode }) {
  const { t } = useTranslation();
  const gateways = useAppStore((state) => state.gateways);
  const profiles = useAppStore((state) => state.profiles);
  const selectedGatewayId = useAppStore((state) => state.selectedGatewayId);
  const selectedProfileId = useAppStore((state) => state.selectedProfileId);
  const selectGateway = useAppStore((state) => state.selectGateway);
  const selectProfile = useAppStore((state) => state.selectProfile);
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const removeSessions = useAppStore((state) => state.removeSessions);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const offline = useAppStore((state) => state.authState === "offline");
  const demoMode = useAppStore((state) => state.demoMode);
  const profile = profiles.find((item) => item.id === selectedProfileId);
  const gateway = gateways.find((item) => item.id === profile?.gatewayId);
  const route = profile && selectedGatewayId ? { gatewayId: selectedGatewayId, profileName: profile.technicalName } : undefined;
  const methodKey = profile?.capabilitySet?.methods.join("\u001f") ?? "";
  const methods = useMemo(() => new Set(profile?.capabilitySet?.methods ?? []), [methodKey]);
  const protectedProfile = profile?.technicalName.toLowerCase() === "default";
  const [blockedLifecycleProfileIds, setBlockedLifecycleProfileIds] = useState<Set<string>>(() => new Set());
  const lifecycleBlocked = Boolean(profile && blockedLifecycleProfileIds.has(profile.id));
  const lifecycleAdvertised = lifecycleIsAdvertised(profile);
  const importGatewayIds = useMemo(() => new Set(
    profiles
      .filter((item) => (
        hasLifecycleCapability(item, "profileImport")
        && hasLifecycleCapability(item, "profileDelete")
        && hasLifecycleCapability(item, "profileTransfer")
      ))
      .map((item) => item.gatewayId),
  ), [profiles]);
  const destinationGateways = useMemo(() => gateways.filter((item) => (
    item.id !== profile?.gatewayId && importGatewayIds.has(item.id)
  )), [gateways, importGatewayIds, profile?.gatewayId]);
  const mutationsBlocked = offline || demoMode;
  const canDeleteProfile = Boolean(
    profile
    && !protectedProfile
    && !lifecycleBlocked
    && !mutationsBlocked
    && hasLifecycleCapability(profile, "profileDelete"),
  );
  const canMoveProfile = Boolean(
    profile
    && !protectedProfile
    && !lifecycleBlocked
    && !mutationsBlocked
    && destinationGateways.length
    && hasLifecycleCapability(profile, "profileDelete")
    && hasLifecycleCapability(profile, "profileExport")
    && hasLifecycleCapability(profile, "profileTransfer"),
  );
  const visibleTabs = useMemo(() => tabDefinitions.filter((tab) => (
    tab.id === "management"
      ? lifecycleAdvertised
      : tab.methods.some((method) => methods.has(method))
  )), [lifecycleAdvertised, methods]);
  const [activeTab, setActiveTab] = useState<AdminTab>("general");
  const [snapshots, setSnapshots] = useState<SnapshotMap>({});
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [lifecycleOutcomeError, setLifecycleOutcomeError] = useState("");
  const [lifecycleDialogError, setLifecycleDialogError] = useState("");
  const [notice, setNotice] = useState("");
  const [lifecycleWarnings, setLifecycleWarnings] = useState<string[]>([]);
  const [moveOpen, setMoveOpen] = useState(false);
  const [moveGatewayId, setMoveGatewayId] = useState("");
  const [moveConfirmation, setMoveConfirmation] = useState("");
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");

  const closeMove = () => {
    if (busy) return;
    setMoveOpen(false);
    setMoveConfirmation("");
    setLifecycleDialogError("");
  };
  const closeDelete = () => {
    if (busy) return;
    setDeleteOpen(false);
    setDeleteConfirmation("");
    setLifecycleDialogError("");
  };
  const moveDialog = useOverlayDialog<HTMLDivElement>({ open: moveOpen, onClose: closeMove, mediaQuery: "(min-width: 0px)" });
  const deleteDialog = useOverlayDialog<HTMLDivElement>({ open: deleteOpen, onClose: closeDelete, mediaQuery: "(min-width: 0px)" });

  useEffect(() => {
    if (!visibleTabs.some((tab) => tab.id === activeTab)) setActiveTab(visibleTabs[0]?.id ?? "general");
  }, [activeTab, visibleTabs]);

  useEffect(() => {
    setSnapshots({});
    setError("");
    setMoveOpen(false);
    setMoveConfirmation("");
    setDeleteOpen(false);
    setDeleteConfirmation("");
    setLifecycleDialogError("");
  }, [selectedGatewayId, selectedProfileId]);

  useEffect(() => {
    if (activeTab === "management") {
      setLoading(false);
      return;
    }
    if (!route || !visibleTabs.length || offline) return;
    let active = true;
    const resources = resourcesByTab[activeTab].filter((resource) => methods.has(readMethods[resource]));
    setLoading(true);
    setError("");
    void Promise.all(resources.map((resource) => readResource(route, resource))).then((results) => {
      if (!active) return;
      setSnapshots((current) => ({ ...current, ...Object.fromEntries(results.map((result) => [result.resource, result.data])) }));
    }).catch((cause: unknown) => {
      if (active) setError(cause instanceof Error ? cause.message : t("adminConfig.errors.read"));
    }).finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [activeTab, methodKey, offline, route?.gatewayId, route?.profileName, visibleTabs.length]);

  const refresh = async (resources: AdminResourceName[]) => {
    if (!route) return;
    const allowed = resources.filter((resource) => methods.has(readMethods[resource]));
    const results = await Promise.all(allowed.map((resource) => readResource(route, resource)));
    setSnapshots((current) => ({ ...current, ...Object.fromEntries(results.map((result) => [result.resource, result.data])) }));
  };

  const perform = async (action: () => Promise<unknown>, message: string, resources = resourcesByTab[activeTab]) => {
    if (offline) return false;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await action();
      await refresh(resources);
      setNotice(message);
      return true;
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : t("adminConfig.errors.rejected"));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const reconcileLifecycle = async (
    source: Profile,
    destinationGatewayId?: string,
  ) => {
    const previousSessionIds = useAppStore.getState().sessions.map((session) => session.id);
    const next = await api.bootstrap();
    const retainedSessionIds = new Set(next.sessions.map((session) => session.id));
    const removedSessionIds = previousSessionIds.filter((sessionId) => !retainedSessionIds.has(sessionId));
    removeSessions(removedSessionIds);
    let localCacheWarning = false;
    try {
      await clearSessionLocalData(removedSessionIds);
    } catch {
      localCacheWarning = true;
    }
    hydrateBootstrap(next);
    const retainedSource = next.profiles.find((item) => item.id === source.id);
    const selected = destinationGatewayId
      ? retainedSource?.gatewayId === destinationGatewayId
        ? retainedSource
        : next.profiles.find((item) => (
          item.gatewayId === destinationGatewayId
          && item.technicalName === source.technicalName
        ))
      : retainedSource
        ?? next.profiles.find((item) => item.gatewayId === source.gatewayId)
        ?? next.profiles[0];
    if (selected) selectProfile(selected.id);
    return localCacheWarning;
  };

  const invalidateSnapshots = async () => {
    try {
      await invalidatePrivateSnapshots();
      return false;
    } catch {
      return true;
    }
  };

  const purgeDeletedProfileSessions = async (source: Profile) => {
    const sessionIds = useAppStore.getState().sessions
      .filter((session) => session.profileId === source.id)
      .map((session) => session.id);
    removeSessions(sessionIds);
    try {
      await clearSessionLocalData(sessionIds);
      return { localCacheWarning: false, sessionIds };
    } catch {
      return { localCacheWarning: true, sessionIds };
    }
  };

  const reconcileAmbiguousLifecycle = async (source: Profile, destinationGatewayId?: string) => {
    let localCacheWarning = await invalidateSnapshots();
    try {
      localCacheWarning = await reconcileLifecycle(source, destinationGatewayId);
      setLifecycleOutcomeError(t("adminConfig.management.outcomeUnknown"));
    } catch {
      setLifecycleOutcomeError(t("adminConfig.management.outcomeUnknownRefresh"));
    }
    setLifecycleWarnings(localCacheWarning ? [t("adminConfig.management.localCacheWarning")] : []);
  };

  const moveAgent = async () => {
    if (!profile || !canMoveProfile || busy || moveConfirmation !== profile.technicalName || !moveGatewayId) return;
    const source = profile;
    setBusy(true);
    setError("");
    setLifecycleOutcomeError("");
    setLifecycleDialogError("");
    setNotice("");
    setLifecycleWarnings([]);
    try {
      const result = await api.moveProfile(source.id, moveGatewayId, moveConfirmation, csrfToken);
      setMoveOpen(false);
      setMoveConfirmation("");
      const preRefreshCacheWarning = await invalidateSnapshots();
      try {
        const localCacheWarning = await reconcileLifecycle(source, moveGatewayId);
        setLifecycleWarnings([
          ...(result?.warnings ?? []),
          ...(localCacheWarning ? [t("adminConfig.management.localCacheWarning")] : []),
        ]);
        setNotice(t("adminConfig.management.moved", { name: source.displayName }));
        setActiveTab("management");
      } catch {
        setLifecycleWarnings(preRefreshCacheWarning ? [t("adminConfig.management.localCacheWarning")] : []);
        setLifecycleOutcomeError(t("adminConfig.management.committedRefreshError", { action: t("adminConfig.management.movedAction") }));
      }
    } catch (cause) {
      if (!(cause instanceof ApiError) || cause.code === "MUTATION_DELIVERY_UNKNOWN") {
        setBlockedLifecycleProfileIds((current) => new Set(current).add(source.id));
        setMoveOpen(false);
        setMoveConfirmation("");
        setLifecycleDialogError("");
        await reconcileAmbiguousLifecycle(source, moveGatewayId);
      } else {
        setLifecycleDialogError(cause instanceof Error ? cause.message : t("adminConfig.errors.rejected"));
      }
    } finally {
      setBusy(false);
    }
  };

  const deleteAgent = async () => {
    if (!profile || !canDeleteProfile || busy || deleteConfirmation !== profile.technicalName) return;
    const source = profile;
    setBusy(true);
    setError("");
    setLifecycleOutcomeError("");
    setLifecycleDialogError("");
    setNotice("");
    setLifecycleWarnings([]);
    try {
      const result = await api.deleteProfile(source.id, deleteConfirmation, csrfToken);
      setDeleteOpen(false);
      setDeleteConfirmation("");
      const preRefreshCache = await purgeDeletedProfileSessions(source);
      try {
        let localCacheWarning = await reconcileLifecycle(source);
        if (preRefreshCache.localCacheWarning) {
          try {
            await clearSessionLocalData(preRefreshCache.sessionIds);
          } catch {
            localCacheWarning = true;
          }
        }
        setLifecycleWarnings([
          ...(result?.warnings ?? []),
          ...(localCacheWarning ? [t("adminConfig.management.localCacheWarning")] : []),
        ]);
        setNotice(t("adminConfig.management.deleted", { name: source.displayName }));
      } catch {
        setLifecycleWarnings(preRefreshCache.localCacheWarning ? [t("adminConfig.management.localCacheWarning")] : []);
        setLifecycleOutcomeError(t("adminConfig.management.committedRefreshError", { action: t("adminConfig.management.deletedAction") }));
      }
    } catch (cause) {
      if (!(cause instanceof ApiError) || cause.code === "MUTATION_DELIVERY_UNKNOWN") {
        setBlockedLifecycleProfileIds((current) => new Set(current).add(source.id));
        setDeleteOpen(false);
        setDeleteConfirmation("");
        setLifecycleDialogError("");
        await reconcileAmbiguousLifecycle(source);
      } else {
        setLifecycleDialogError(cause instanceof Error ? cause.message : t("adminConfig.errors.rejected"));
      }
    } finally {
      setBusy(false);
    }
  };

  const profileOptions = profiles.filter((item) => item.gatewayId === selectedGatewayId);

  return <div className="page-wrap admin-config-page">
    {header}
    <Panel className="admin-route-picker" aria-label={t("adminConfig.route.aria")}><label><span>{t("adminConfig.route.gateway")}</span><select value={selectedGatewayId} onChange={(event) => { setError(""); setLifecycleOutcomeError(""); setNotice(""); setLifecycleWarnings([]); selectGateway(event.target.value); }}>{gateways.map((gateway) => <option key={gateway.id} value={gateway.id}>{gateway.name}</option>)}</select></label><label><span>{t("adminConfig.route.profile")}</span><select value={selectedProfileId} onChange={(event) => { setError(""); setLifecycleOutcomeError(""); setNotice(""); setLifecycleWarnings([]); selectProfile(event.target.value); }}>{profileOptions.map((item) => <option key={item.id} value={item.id}>{item.displayName} · {item.technicalName}</option>)}</select></label><div><span>{t("adminConfig.route.contract")}</span><strong>{profile?.capabilitySet?.version ?? t("adminConfig.route.unverified")}</strong><small>{t("adminConfig.route.exactMethods", { count: methods.size })}</small></div></Panel>
    {offline ? <div className="success-banner success-banner--warning" role="status"><WarningCircle weight="fill" /> {t("adminConfig.offline")}</div> : null}
    {notice ? <div className="success-banner" role="status"><CheckCircle weight="fill" /> {notice}</div> : null}
    {lifecycleWarnings.length ? <div className="agent-lifecycle-warnings" role="status"><WarningCircle weight="fill" /><div><strong>{t("adminConfig.management.warningsTitle")}</strong><ul>{lifecycleWarnings.map((warning, index) => <li key={`${index}-${warning}`}>{warning}</li>)}</ul></div></div> : null}
    {lifecycleOutcomeError || error ? <div className="admin-error" role="alert"><WarningCircle weight="fill" /><span><strong>{t("adminConfig.errors.heading")}</strong>{lifecycleOutcomeError || error}</span></div> : null}
    {!visibleTabs.length ? <Panel className="safety-callout"><ShieldCheck size={24} /><div><strong>{t("adminConfig.unavailable.title")}</strong><p>{t("adminConfig.unavailable.body", { profile: profile?.displayName ?? t("adminConfig.unavailable.selectedProfile") })}</p></div></Panel> : <div className="config-layout"><aside aria-label={t("adminConfig.route.aria")}>{visibleTabs.map((tab) => <button key={tab.id} type="button" className={activeTab === tab.id ? "is-active" : ""} aria-current={activeTab === tab.id ? "page" : undefined} onClick={() => setActiveTab(tab.id)}>{t(`adminConfig.tabs.${tab.id}`)}</button>)}</aside><div className="config-sections" aria-busy={loading || busy}>{loading ? <div className="admin-loading" role="status"><SpinnerGap className="is-spinning" /> {t("adminConfig.loading")}</div> : null}
      {activeTab === "general" ? <>{methods.has("models.list") ? <ModelsSection data={snapshots.models} canWrite={methods.has("models.set") && !offline} busy={busy} onSave={(provider, model, confirmExpensiveModel) => route ? perform(() => api.adminSetModel(route.gatewayId, route.profileName, { provider, model, confirmExpensiveModel }, csrfToken), t("adminConfig.models.updated"), ["models", "config"]) : Promise.resolve(false)} /> : null}{methods.has("config.get") ? <ConfigDocumentSection data={snapshots.config} canWrite={methods.has("config.set") && !offline} busy={busy} onSave={(config) => route ? perform(() => api.adminUpdateConfig(route.gatewayId, route.profileName, config, csrfToken), t("adminConfig.config.applied"), ["config"]) : Promise.resolve(false)} /> : null}{methods.has("usage.get") ? <UsageSection data={snapshots.usage} /> : null}</> : null}
      {activeTab === "identity" && methods.has("soul.get") ? <SoulSection data={snapshots.soul} canWrite={methods.has("soul.set") && !offline} busy={busy} onSave={(content) => route ? perform(() => api.adminUpdateSoul(route.gatewayId, route.profileName, content, csrfToken), t("adminConfig.soul.updated"), ["soul"]) : Promise.resolve(false)} /> : null}
      {activeTab === "tools" ? <>{methods.has("skills.list") ? <ToggleCollection title={t("adminConfig.collections.skillsTitle")} description={t("adminConfig.collections.skillsDescription")} icon={<Wrench weight="duotone" />} rows={recordsFrom(snapshots.skills)} canToggle={methods.has("skills.toggle") && !offline} busy={busy} onToggle={(name, enabled) => route ? perform(() => api.adminToggleSkill(route.gatewayId, route.profileName, name, enabled, csrfToken), t("adminConfig.common.updated", { name }), ["skills"]) : Promise.resolve(false)} /> : null}{methods.has("toolsets.list") ? <ToggleCollection title={t("adminConfig.collections.toolsetsTitle")} description={t("adminConfig.collections.toolsetsDescription")} icon={<Wrench weight="duotone" />} rows={recordsFrom(snapshots.toolsets)} canToggle={methods.has("toolsets.toggle") && !offline} busy={busy} onToggle={(name, enabled) => route ? perform(() => api.adminToggleToolset(route.gatewayId, route.profileName, name, enabled, csrfToken), t("adminConfig.common.updated", { name }), ["toolsets"]) : Promise.resolve(false)} /> : null}</> : null}
      {activeTab === "integrations" ? <>{methods.has("mcp.list") ? <McpSection data={snapshots.mcp} methods={methods} busy={busy || offline} onAction={(action, message) => perform(action, message, ["mcp"])} /> : null}{methods.has("channels.list") ? <ChannelsSection data={snapshots.channels} methods={methods} busy={busy || offline} onAction={(action, message) => perform(action, message, ["channels"])} /> : null}</> : null}
      {activeTab === "secrets" && methods.has("secrets.list") ? <SecretsSection data={snapshots.secrets} methods={methods} busy={busy || offline} onSet={(name, value) => route ? perform(() => api.adminSetSecret(route.gatewayId, route.profileName, name, value, csrfToken), t("adminConfig.secrets.saved", { name }), ["secrets"]) : Promise.resolve(false)} onDelete={(name) => route ? perform(() => api.adminDeleteSecret(route.gatewayId, route.profileName, name, csrfToken), t("adminConfig.common.deleted", { name }), ["secrets"]) : Promise.resolve(false)} /> : null}
      {activeTab === "management" && profile ? <AgentManagementSection profile={profile} gateway={gateway} canMove={canMoveProfile} canDelete={canDeleteProfile} protectedProfile={Boolean(protectedProfile)} lifecycleBlocked={lifecycleBlocked} destinations={destinationGateways} busy={busy} onMove={() => { setMoveGatewayId(destinationGateways[0]?.id ?? ""); setMoveConfirmation(""); setError(""); setLifecycleOutcomeError(""); setLifecycleDialogError(""); setMoveOpen(true); }} onDelete={() => { setDeleteConfirmation(""); setError(""); setLifecycleOutcomeError(""); setLifecycleDialogError(""); setDeleteOpen(true); }} /> : null}
    </div></div>}
    {moveOpen && profile ? <div ref={moveDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="agent-move-title" aria-describedby={`agent-move-description${lifecycleDialogError ? " agent-move-error" : ""}`}>
      <button type="button" className="modal-scrim" tabIndex={-1} aria-hidden="true" onClick={closeMove} />
      <Panel className="form-modal agent-lifecycle-dialog">
        <span className="eyebrow">{t("adminConfig.management.moveEyebrow")}</span>
        <h2 id="agent-move-title">{t("adminConfig.management.moveDialogTitle", { name: profile.displayName })}</h2>
        <p id="agent-move-description">{t("adminConfig.management.moveDialogDescription")}</p>
        {lifecycleDialogError ? <p id="agent-move-error" className="form-error agent-lifecycle-dialog-error" role="alert"><WarningCircle /> {lifecycleDialogError}</p> : null}
        <form onSubmit={(event) => { event.preventDefault(); void moveAgent(); }}>
          <label className="hc-field"><span className="hc-field__label">{t("adminConfig.management.destinationGateway")}</span><select autoFocus value={moveGatewayId} onChange={(event) => { setMoveGatewayId(event.target.value); setLifecycleDialogError(""); }} disabled={busy}>{destinationGateways.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label>
          <div className="agent-lifecycle-disclosure"><ShieldCheck weight="duotone" /><p>{t("adminConfig.management.transferDisclosure")}</p></div>
          <label className="hc-field"><span className="hc-field__label">{t("adminConfig.management.typeToConfirm", { name: profile.technicalName })}</span><input className="hc-input" autoComplete="off" spellCheck={false} value={moveConfirmation} onChange={(event) => { setMoveConfirmation(event.target.value); setLifecycleDialogError(""); }} disabled={busy} /></label>
          <div><Button type="button" variant="ghost" disabled={busy} onClick={closeMove}>{t("adminConfig.management.cancel")}</Button><Button type="submit" variant="primary" disabled={busy || !moveGatewayId || moveConfirmation !== profile.technicalName} leadingIcon={<ArrowsLeftRight />}>{busy ? t("adminConfig.management.moving") : t("adminConfig.management.confirmMove")}</Button></div>
        </form>
      </Panel>
    </div> : null}
    {deleteOpen && profile ? <div ref={deleteDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="agent-delete-title" aria-describedby={`agent-delete-description${lifecycleDialogError ? " agent-delete-error" : ""}`}>
      <button type="button" className="modal-scrim" tabIndex={-1} aria-hidden="true" onClick={closeDelete} />
      <Panel className="form-modal agent-lifecycle-dialog agent-lifecycle-delete-dialog">
        <span className="eyebrow">{t("adminConfig.management.deleteEyebrow")}</span>
        <h2 id="agent-delete-title">{t("adminConfig.management.deleteDialogTitle", { name: profile.displayName })}</h2>
        <p id="agent-delete-description">{t("adminConfig.management.deleteDialogDescription")}</p>
        {lifecycleDialogError ? <p id="agent-delete-error" className="form-error agent-lifecycle-dialog-error" role="alert"><WarningCircle /> {lifecycleDialogError}</p> : null}
        <form onSubmit={(event) => { event.preventDefault(); void deleteAgent(); }}>
          <label className="hc-field"><span className="hc-field__label">{t("adminConfig.management.typeToConfirm", { name: profile.technicalName })}</span><input className="hc-input" autoFocus autoComplete="off" spellCheck={false} value={deleteConfirmation} onChange={(event) => { setDeleteConfirmation(event.target.value); setLifecycleDialogError(""); }} disabled={busy} /></label>
          <div><Button type="button" variant="ghost" disabled={busy} onClick={closeDelete}>{t("adminConfig.management.cancel")}</Button><Button type="submit" variant="danger" disabled={busy || deleteConfirmation !== profile.technicalName} leadingIcon={<Trash />}>{busy ? t("adminConfig.management.deleting") : t("adminConfig.management.confirmDelete")}</Button></div>
        </form>
      </Panel>
    </div> : null}
  </div>;
}
