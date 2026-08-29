import {
  Brain,
  ChartLineUp,
  CheckCircle,
  Key,
  PlugsConnected,
  Robot,
  ShieldCheck,
  SpinnerGap,
  Wrench,
  WarningCircle,
} from "@phosphor-icons/react";
import { Badge, Button, Panel, Switch } from "@hermes-control/ui";
import { useEffect, useMemo, useState } from "react";
import { api, type AdminResourceName, type AdminResourceView } from "../lib/api";
import { useAppStore } from "../store/appStore";

type AdminTab = "general" | "identity" | "tools" | "integrations" | "secrets";
type SnapshotMap = Partial<Record<AdminResourceName, Record<string, unknown>>>;
type Route = { gatewayId: string; profileName: string };

const tabDefinitions: Array<{
  id: AdminTab;
  label: string;
  methods: string[];
}> = [
  { id: "general", label: "General", methods: ["models.list", "config.get", "usage.get"] },
  { id: "identity", label: "Identidad", methods: ["soul.get"] },
  { id: "tools", label: "Herramientas", methods: ["skills.list", "toolsets.list"] },
  { id: "integrations", label: "Integraciones", methods: ["mcp.list", "channels.list"] },
  { id: "secrets", label: "Secretos", methods: ["secrets.list"] },
];

const resourcesByTab: Record<AdminTab, AdminResourceName[]> = {
  general: ["models", "config", "usage"],
  identity: ["soul"],
  tools: ["skills", "toolsets"],
  integrations: ["mcp", "channels"],
  secrets: ["secrets"],
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

function displayNumber(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat("es-MX").format(value) : "—";
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
    <SectionHeader icon={<Brain weight="duotone" />} title="Modelo principal" description="Selección detectada por Hermes para este perfil." badge={canWrite ? "editable" : "solo lectura"} />
    {providers.length ? <form className="admin-form-grid" onSubmit={(event) => { event.preventDefault(); void onSave(provider, model, confirmed); }}>
      <label className="hc-field"><span className="hc-field__label">Proveedor</span><select value={provider} onChange={(event) => changeProvider(event.target.value)} disabled={!canWrite || busy}>{providers.map((item) => <option key={stringValue(item.id)} value={stringValue(item.id)}>{stringValue(item.label, stringValue(item.id))}</option>)}</select></label>
      <label className="hc-field"><span className="hc-field__label">Modelo</span><select value={model} onChange={(event) => setModel(event.target.value)} disabled={!canWrite || busy}>{models.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>
      {canWrite ? <label className="admin-check"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} /><span>Confirmo el cambio si el modelo tiene un costo superior.</span></label> : null}
      {canWrite ? <Button type="submit" variant="primary" disabled={busy || !provider || !model}>Guardar modelo</Button> : null}
    </form> : <EmptySection>Hermes no devolvió opciones de modelo.</EmptySection>}
  </Panel>;
}

function ConfigDocumentSection({ data, canWrite, busy, onSave }: { data?: Record<string, unknown>; canWrite: boolean; busy: boolean; onSave: (config: Record<string, unknown>) => Promise<boolean> }) {
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
      setError("Escribe un objeto JSON válido. Los secretos deben configurarse en su sección write-only.");
    }
  };
  return <Panel className="admin-section">
    <SectionHeader icon={<Robot weight="duotone" />} title="Configuración compatible" description="Documento saneado del perfil. Los campos con forma de secreto son rechazados por la API." badge={canWrite ? "avanzado" : "solo lectura"} />
    <label className="hc-field"><span className="hc-field__label">Configuración JSON</span><textarea className="admin-code-editor" spellCheck={false} value={value} readOnly={!canWrite} onChange={(event) => setValue(event.target.value)} aria-describedby={error ? "config-json-error" : undefined} /></label>
    {error ? <p id="config-json-error" className="form-error" role="alert"><WarningCircle /> {error}</p> : null}
    {canWrite ? <div className="admin-section__actions"><Button variant="primary" disabled={busy} onClick={() => void save()}>Aplicar configuración</Button></div> : null}
  </Panel>;
}

function UsageSection({ data }: { data?: Record<string, unknown> }) {
  const totals = asRecord(data?.totals);
  return <Panel className="admin-section">
    <SectionHeader icon={<ChartLineUp weight="duotone" />} title="Uso y contexto" description={`Ventana de ${displayNumber(data?.period_days)} días informada por Hermes.`} />
    <dl className="admin-metrics"><div><dt>Entrada</dt><dd>{displayNumber(totals.total_input)}</dd></div><div><dt>Salida</dt><dd>{displayNumber(totals.total_output)}</dd></div><div><dt>Sesiones</dt><dd>{displayNumber(totals.total_sessions)}</dd></div></dl>
  </Panel>;
}

function SoulSection({ data, canWrite, busy, onSave }: { data?: Record<string, unknown>; canWrite: boolean; busy: boolean; onSave: (content: string) => Promise<boolean> }) {
  const [content, setContent] = useState("");
  useEffect(() => setContent(stringValue(data?.content)), [data]);
  return <Panel className="admin-section">
    <SectionHeader icon={<Robot weight="duotone" />} title="SOUL" description="Identidad e instrucciones oficiales del perfil, gestionadas en Hermes." badge={canWrite ? "editable" : "solo lectura"} />
    <label className="hc-field"><span className="hc-field__label">Contenido de SOUL</span><textarea className="admin-soul-editor" value={content} readOnly={!canWrite} onChange={(event) => setContent(event.target.value)} /></label>
    {canWrite ? <div className="admin-section__actions"><Button variant="primary" disabled={busy} onClick={() => void onSave(content)}>Guardar SOUL</Button></div> : null}
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
  return <Panel className="admin-section"><SectionHeader icon={icon} title={title} description={description} badge={canToggle ? "editable" : "solo lectura"} />
    {rows.length ? <div className="admin-toggle-list">{rows.map((item) => {
      const name = stringValue(item.name, stringValue(item.id));
      return <Switch key={name} checked={booleanValue(item.enabled)} disabled={!canToggle || busy} onChange={(enabled) => void onToggle(name, enabled)} label={stringValue(item.label, name)} description={stringValue(item.description, stringValue(item.category))} />;
    })}</div> : <EmptySection>No hay elementos anunciados para este perfil.</EmptySection>}
  </Panel>;
}

function McpSection({ data, methods, busy, onAction }: {
  data?: Record<string, unknown>;
  methods: Set<string>;
  busy: boolean;
  onAction: (action: () => Promise<unknown>, message: string) => Promise<boolean>;
}) {
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
      setValidation("Indica un nombre y exactamente una conexión: URL o comando.");
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
      }, csrfToken), "Servidor MCP añadido.");
      if (ok) { setName(""); setUrl(""); setCommand(""); }
    } finally {
      setBearerToken("");
    }
  };
  return <Panel className="admin-section"><SectionHeader icon={<PlugsConnected weight="duotone" />} title="Servidores MCP" description="Conexiones detectadas para el perfil. Los tokens solo se escriben y se vacían al terminar." badge={methods.has("mcp.create") ? "editable" : "solo lectura"} />
    {rows.length ? <div className="admin-resource-list">{rows.map((item) => {
      const itemName = stringValue(item.name);
      return <div key={itemName} className="admin-resource-row"><div><strong>{itemName}</strong><small>{stringValue(item.url, stringValue(item.command, "Transporte administrado"))}</small></div><Badge tone={booleanValue(item.configured) ? "positive" : "warning"}>{booleanValue(item.configured) ? "configurado" : "incompleto"}</Badge><div className="admin-row-actions">
        {methods.has("mcp.toggle") ? <Switch checked={booleanValue(item.enabled)} disabled={busy} onChange={(enabled) => route && void onAction(() => api.adminToggleMcpServer(route.gatewayId, route.profileName, itemName, enabled, csrfToken), `${itemName} actualizado.`)} label={`Activar ${itemName}`} /> : null}
        {methods.has("mcp.test") ? <Button size="sm" variant="ghost" disabled={busy} onClick={() => route && void onAction(() => api.adminTestMcpServer(route.gatewayId, route.profileName, itemName, csrfToken), `Prueba de ${itemName} completada.`)}>Probar</Button> : null}
        {methods.has("mcp.delete") ? <Button size="sm" variant="danger" disabled={busy} onClick={() => { if (route && window.confirm(`¿Eliminar el servidor MCP ${itemName}?`)) void onAction(() => api.adminDeleteMcpServer(route.gatewayId, route.profileName, itemName, csrfToken), `${itemName} eliminado.`); }}>Eliminar</Button> : null}
      </div></div>;
    })}</div> : <EmptySection>No hay servidores MCP configurados.</EmptySection>}
    {methods.has("mcp.create") ? <div className="admin-create-box"><h3>Añadir servidor</h3><div className="admin-form-grid"><label className="hc-field"><span className="hc-field__label">Nombre</span><input className="hc-input" value={name} onChange={(event) => setName(event.target.value)} /></label><label className="hc-field"><span className="hc-field__label">URL</span><input className="hc-input" type="url" value={url} onChange={(event) => { setUrl(event.target.value); if (event.target.value) setCommand(""); }} /></label><label className="hc-field"><span className="hc-field__label">Comando local</span><input className="hc-input" value={command} onChange={(event) => { setCommand(event.target.value); if (event.target.value) setUrl(""); }} /></label><label className="hc-field"><span className="hc-field__label">Bearer token (solo escritura)</span><input className="hc-input" type="password" autoComplete="new-password" value={bearerToken} onChange={(event) => setBearerToken(event.target.value)} /></label></div>{validation ? <p className="form-error" role="alert"><WarningCircle /> {validation}</p> : null}<Button variant="primary" disabled={busy} onClick={() => void create()}>Añadir MCP</Button></div> : null}
  </Panel>;
}

function ChannelRow({ item, methods, busy, onAction }: { item: Record<string, unknown>; methods: Set<string>; busy: boolean; onAction: (action: () => Promise<unknown>, message: string) => Promise<boolean> }) {
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
      if (Object.keys(payload).length) await onAction(() => api.adminUpdateChannel(route.gatewayId, route.profileName, name, { env: payload }, csrfToken), `${label} actualizado.`);
    } finally {
      setEnvValues({});
    }
  };
  return <div className="admin-channel"><div className="admin-channel__head"><div><strong>{label}</strong><small>{stringValue(item.state, booleanValue(item.configured) ? "configurado" : "requiere configuración")}</small></div>{methods.has("channels.update") ? <Switch checked={booleanValue(item.enabled)} disabled={busy} onChange={(enabled) => route && void onAction(() => api.adminUpdateChannel(route.gatewayId, route.profileName, name, { enabled }, csrfToken), `${label} actualizado.`)} label={`Activar ${label}`} /> : <Badge tone={booleanValue(item.enabled) ? "positive" : "neutral"}>{booleanValue(item.enabled) ? "activo" : "inactivo"}</Badge>}</div>
    {envRows.length ? <div className="admin-env-list">{envRows.map((field) => { const key = stringValue(field.key); return <div key={key}><label className="hc-field"><span className="hc-field__label">{key} (solo escritura)</span><input className="hc-input" type="password" autoComplete="new-password" placeholder={booleanValue(field.is_set) ? "Configurado · escribe para reemplazar" : "Sin configurar"} value={envValues[key] ?? ""} onChange={(event) => setEnvValues((current) => ({ ...current, [key]: event.target.value }))} /></label>{methods.has("channels.update") && booleanValue(field.is_set) ? <Button size="sm" variant="ghost" disabled={busy} onClick={() => { if (route && window.confirm(`¿Borrar ${key} de ${label}?`)) void onAction(() => api.adminUpdateChannel(route.gatewayId, route.profileName, name, { clearEnv: [key] }, csrfToken), `${key} eliminado.`); }}>Borrar valor</Button> : null}</div>; })}</div> : null}
    <div className="admin-row-actions">{methods.has("channels.update") && envRows.length ? <Button size="sm" variant="primary" disabled={busy || !Object.values(envValues).some(Boolean)} onClick={() => void saveEnv()}>Guardar credenciales</Button> : null}{methods.has("channels.test") ? <Button size="sm" variant="ghost" disabled={busy} onClick={() => route && void onAction(() => api.adminTestChannel(route.gatewayId, route.profileName, name, csrfToken), `Prueba de ${label} completada.`)}>Probar canal</Button> : null}</div>
  </div>;
}

function ChannelsSection({ data, methods, busy, onAction }: { data?: Record<string, unknown>; methods: Set<string>; busy: boolean; onAction: (action: () => Promise<unknown>, message: string) => Promise<boolean> }) {
  const rows = recordsFrom(data, "platforms");
  return <Panel className="admin-section"><SectionHeader icon={<PlugsConnected weight="duotone" />} title="Canales" description="Canales y credenciales administrados por Hermes." badge={methods.has("channels.update") ? "editable" : "solo lectura"} />{rows.length ? <div className="admin-resource-list">{rows.map((item) => <ChannelRow key={stringValue(item.id, stringValue(item.name))} item={item} methods={methods} busy={busy} onAction={onAction} />)}</div> : <EmptySection>No hay canales anunciados.</EmptySection>}</Panel>;
}

function SecretRow({ item, canSet, canDelete, busy, onSet, onDelete }: {
  item: Record<string, unknown>;
  canSet: boolean;
  canDelete: boolean;
  busy: boolean;
  onSet: (name: string, value: string) => Promise<boolean>;
  onDelete: (name: string) => Promise<boolean>;
}) {
  const name = stringValue(item.name);
  const [value, setValue] = useState("");
  const save = async () => {
    try { if (value) await onSet(name, value); } finally { setValue(""); }
  };
  return <div className="admin-secret-row"><div><strong>{name}</strong><small>{stringValue(item.description, "Valor administrado por Hermes")}</small></div><Badge tone={booleanValue(item.configured) ? "positive" : "warning"}>{booleanValue(item.configured) ? "configurado" : "sin configurar"}</Badge>{canSet ? <label className="hc-field"><span className="hc-field__label">Nuevo valor para {name}</span><input className="hc-input" type="password" autoComplete="new-password" value={value} onChange={(event) => setValue(event.target.value)} /></label> : null}<div className="admin-row-actions">{canSet ? <Button size="sm" variant="primary" disabled={busy || !value} onClick={() => void save()}>Guardar valor</Button> : null}{canDelete && booleanValue(item.configured) ? <Button size="sm" variant="danger" disabled={busy} onClick={() => { if (window.confirm(`¿Eliminar ${name}? Hermes dejará de tener acceso a este valor.`)) void onDelete(name); }}>Eliminar</Button> : null}</div></div>;
}

function SecretsSection({ data, methods, busy, onSet, onDelete }: { data?: Record<string, unknown>; methods: Set<string>; busy: boolean; onSet: (name: string, value: string) => Promise<boolean>; onDelete: (name: string) => Promise<boolean> }) {
  const rows = recordsFrom(data);
  return <Panel className="admin-section admin-secrets"><SectionHeader icon={<Key weight="duotone" />} title="Secretos write-only" description="La interfaz solo conoce si un valor está configurado. Nunca recibe ni muestra su contenido." badge="protegido" />{rows.length ? <div className="admin-resource-list">{rows.map((item) => <SecretRow key={stringValue(item.name)} item={item} canSet={methods.has("secrets.set")} canDelete={methods.has("secrets.delete")} busy={busy} onSet={onSet} onDelete={onDelete} />)}</div> : <EmptySection>Hermes no anunció secretos configurables.</EmptySection>}</Panel>;
}

function useAdminRoute(): Route | undefined {
  const gatewayId = useAppStore((state) => state.selectedGatewayId);
  const profileId = useAppStore((state) => state.selectedProfileId);
  const profile = useAppStore((state) => state.profiles.find((item) => item.id === profileId));
  return gatewayId && profile?.technicalName ? { gatewayId, profileName: profile.technicalName } : undefined;
}

export function AdminConfigScreen({ header }: { header: React.ReactNode }) {
  const gateways = useAppStore((state) => state.gateways);
  const profiles = useAppStore((state) => state.profiles);
  const selectedGatewayId = useAppStore((state) => state.selectedGatewayId);
  const selectedProfileId = useAppStore((state) => state.selectedProfileId);
  const selectGateway = useAppStore((state) => state.selectGateway);
  const selectProfile = useAppStore((state) => state.selectProfile);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const offline = useAppStore((state) => state.authState === "offline");
  const profile = profiles.find((item) => item.id === selectedProfileId);
  const route = profile && selectedGatewayId ? { gatewayId: selectedGatewayId, profileName: profile.technicalName } : undefined;
  const methodKey = profile?.capabilitySet?.methods.join("\u001f") ?? "";
  const methods = useMemo(() => new Set(profile?.capabilitySet?.methods ?? []), [methodKey]);
  const visibleTabs = useMemo(() => tabDefinitions.filter((tab) => tab.methods.some((method) => methods.has(method))), [methods]);
  const [activeTab, setActiveTab] = useState<AdminTab>("general");
  const [snapshots, setSnapshots] = useState<SnapshotMap>({});
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    if (!visibleTabs.some((tab) => tab.id === activeTab)) setActiveTab(visibleTabs[0]?.id ?? "general");
  }, [activeTab, visibleTabs]);

  useEffect(() => {
    setSnapshots({});
    setNotice("");
    setError("");
  }, [selectedGatewayId, selectedProfileId]);

  useEffect(() => {
    if (!route || !visibleTabs.length || offline) return;
    let active = true;
    const resources = resourcesByTab[activeTab].filter((resource) => methods.has(readMethods[resource]));
    setLoading(true);
    setError("");
    void Promise.all(resources.map((resource) => readResource(route, resource))).then((results) => {
      if (!active) return;
      setSnapshots((current) => ({ ...current, ...Object.fromEntries(results.map((result) => [result.resource, result.data])) }));
    }).catch((cause: unknown) => {
      if (active) setError(cause instanceof Error ? cause.message : "No se pudo leer la configuración de Hermes.");
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
      setError(cause instanceof Error ? cause.message : "Hermes rechazó la operación.");
      return false;
    } finally {
      setBusy(false);
    }
  };

  const profileOptions = profiles.filter((item) => item.gatewayId === selectedGatewayId);

  return <div className="page-wrap admin-config-page">
    {header}
    <Panel className="admin-route-picker" aria-label="Destino de configuración"><label><span>Gateway</span><select value={selectedGatewayId} onChange={(event) => selectGateway(event.target.value)}>{gateways.map((gateway) => <option key={gateway.id} value={gateway.id}>{gateway.name}</option>)}</select></label><label><span>Perfil Hermes</span><select value={selectedProfileId} onChange={(event) => selectProfile(event.target.value)}>{profileOptions.map((item) => <option key={item.id} value={item.id}>{item.displayName} · {item.technicalName}</option>)}</select></label><div><span>Contrato</span><strong>{profile?.capabilitySet?.version ?? "sin verificar"}</strong><small>{methods.size} métodos exactos</small></div></Panel>
    {offline ? <div className="success-banner success-banner--warning" role="status"><WarningCircle weight="fill" /> Administración bloqueada sin conexión. Los datos visibles pueden estar desactualizados.</div> : null}
    {notice ? <div className="success-banner" role="status"><CheckCircle weight="fill" /> {notice}</div> : null}
    {error ? <div className="admin-error" role="alert"><WarningCircle weight="fill" /><span><strong>No se completó la operación</strong>{error}</span></div> : null}
    {!visibleTabs.length ? <Panel className="safety-callout"><ShieldCheck size={24} /><div><strong>Sin funciones administrativas verificadas</strong><p>El perfil {profile?.displayName ?? "seleccionado"} no anunció métodos exactos de administración. Agent Control mantiene ocultos todos los controles.</p></div></Panel> : <div className="config-layout"><aside aria-label="Secciones de configuración">{visibleTabs.map((tab) => <button key={tab.id} type="button" className={activeTab === tab.id ? "is-active" : ""} aria-current={activeTab === tab.id ? "page" : undefined} onClick={() => setActiveTab(tab.id)}>{tab.label}</button>)}</aside><div className="config-sections" aria-busy={loading || busy}>{loading ? <div className="admin-loading" role="status"><SpinnerGap className="is-spinning" /> Consultando Hermes…</div> : null}
      {activeTab === "general" ? <>{methods.has("models.list") ? <ModelsSection data={snapshots.models} canWrite={methods.has("models.set") && !offline} busy={busy} onSave={(provider, model, confirmExpensiveModel) => route ? perform(() => api.adminSetModel(route.gatewayId, route.profileName, { provider, model, confirmExpensiveModel }, csrfToken), "Modelo actualizado.", ["models", "config"]) : Promise.resolve(false)} /> : null}{methods.has("config.get") ? <ConfigDocumentSection data={snapshots.config} canWrite={methods.has("config.set") && !offline} busy={busy} onSave={(config) => route ? perform(() => api.adminUpdateConfig(route.gatewayId, route.profileName, config, csrfToken), "Configuración aplicada.", ["config"]) : Promise.resolve(false)} /> : null}{methods.has("usage.get") ? <UsageSection data={snapshots.usage} /> : null}</> : null}
      {activeTab === "identity" && methods.has("soul.get") ? <SoulSection data={snapshots.soul} canWrite={methods.has("soul.set") && !offline} busy={busy} onSave={(content) => route ? perform(() => api.adminUpdateSoul(route.gatewayId, route.profileName, content, csrfToken), "SOUL actualizado.", ["soul"]) : Promise.resolve(false)} /> : null}
      {activeTab === "tools" ? <>{methods.has("skills.list") ? <ToggleCollection title="Skills" description="Habilidades descubiertas en el perfil." icon={<Wrench weight="duotone" />} rows={recordsFrom(snapshots.skills)} canToggle={methods.has("skills.toggle") && !offline} busy={busy} onToggle={(name, enabled) => route ? perform(() => api.adminToggleSkill(route.gatewayId, route.profileName, name, enabled, csrfToken), `${name} actualizado.`, ["skills"]) : Promise.resolve(false)} /> : null}{methods.has("toolsets.list") ? <ToggleCollection title="Toolsets" description="Conjuntos de herramientas administrados por Hermes." icon={<Wrench weight="duotone" />} rows={recordsFrom(snapshots.toolsets)} canToggle={methods.has("toolsets.toggle") && !offline} busy={busy} onToggle={(name, enabled) => route ? perform(() => api.adminToggleToolset(route.gatewayId, route.profileName, name, enabled, csrfToken), `${name} actualizado.`, ["toolsets"]) : Promise.resolve(false)} /> : null}</> : null}
      {activeTab === "integrations" ? <>{methods.has("mcp.list") ? <McpSection data={snapshots.mcp} methods={methods} busy={busy || offline} onAction={(action, message) => perform(action, message, ["mcp"])} /> : null}{methods.has("channels.list") ? <ChannelsSection data={snapshots.channels} methods={methods} busy={busy || offline} onAction={(action, message) => perform(action, message, ["channels"])} /> : null}</> : null}
      {activeTab === "secrets" && methods.has("secrets.list") ? <SecretsSection data={snapshots.secrets} methods={methods} busy={busy || offline} onSet={(name, value) => route ? perform(() => api.adminSetSecret(route.gatewayId, route.profileName, name, value, csrfToken), `${name} guardado sin exponer su valor.`, ["secrets"]) : Promise.resolve(false)} onDelete={(name) => route ? perform(() => api.adminDeleteSecret(route.gatewayId, route.profileName, name, csrfToken), `${name} eliminado.`, ["secrets"]) : Promise.resolve(false)} /> : null}
    </div></div>}
  </div>;
}
