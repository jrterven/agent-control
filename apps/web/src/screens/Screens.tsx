import { zodResolver } from "@hookform/resolvers/zod";
import {
  ArrowRight, CheckCircle, Clock, CloudCheck, Code,
  Database, DownloadSimple, FileText, FolderOpen, Gauge, GearSix, HardDrives,
  Key, Lightning, MagnifyingGlass, Plus, Robot, ShieldCheck,
  PencilSimple, Play, Pulse, SlidersHorizontal, TerminalWindow, Trash, UserCircle, WarningCircle,
} from "@phosphor-icons/react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useMemo, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";
import { Badge, Button, Field, Panel, StatusDot, Switch, cx } from "@hermes-control/ui";
import { api, type ReadinessView } from "../lib/api";
import { clearPrivateCache, clearTranscriptCache, savePreference } from "../lib/db";
import { createAndProvisionGateway } from "../lib/gatewayProvisioning";
import { buildSearchResults } from "../lib/search";
import { useOverlayDialog } from "../lib/useOverlayDialog";
import { useAppStore } from "../store/appStore";
import type { Automation, AutomationRun, Gateway, Profile, SearchResult, ThemePreference } from "../types";
import { BrandMark } from "../components/BrandMark";
import { AdminConfigScreen } from "../components/AdminConfigScreen";

export function PageHeader({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) {
  return <header className="page-header"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{action}</header>;
}

const loginSchema = z.object({ username: z.string().min(1, "Escribe tu usuario"), password: z.string().min(12, "Usa al menos 12 caracteres") });
type LoginValues = z.infer<typeof loginSchema>;

export function LoginScreen() {
  const setAuth = useAppStore((state) => state.setAuth);
  const [serverError, setServerError] = useState("");
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<LoginValues>({ resolver: zodResolver(loginSchema), defaultValues: { username: "", password: "" } });
  const onSubmit = handleSubmit(async (values) => {
    setServerError("");
    try {
      const user = await api.login(values.username, values.password);
      setAuth("authenticated", user.name, user.csrfToken, false);
    } catch {
      setServerError("No pudimos iniciar sesión. Revisa las credenciales o la conexión.");
    }
  });
  return (
    <main className="login-screen">
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-brand"><BrandMark size="lg" label="Agent Control" /><div><strong>Agent</strong><span>Control</span></div></div>
        <span className="eyebrow">Acceso protegido</span>
        <h1 id="login-title">Tus agentes. Un solo lugar.</h1>
        <p>Conversa, organiza y automatiza tu infraestructura de agentes desde una interfaz segura, estés donde estés.</p>
        <form onSubmit={onSubmit} noValidate>
          <Field label="Usuario" autoComplete="username" error={errors.username?.message} {...register("username")} />
          <Field label="Contraseña" type="password" autoComplete="current-password" error={errors.password?.message} {...register("password")} />
          {serverError ? <p className="form-error" role="alert"><WarningCircle /> {serverError}</p> : null}
          <Button type="submit" variant="primary" disabled={isSubmitting}>{isSubmitting ? "Comprobando…" : "Entrar a Agent Control"}</Button>
        </form>
        <footer><ShieldCheck size={18} /> La sesión se guarda en una cookie segura. Las credenciales nunca se almacenan en este dispositivo.</footer>
      </section>
    </main>
  );
}

const administrativeWriteMethods = new Set([
  "session.delete", "cron.create", "cron.update", "cron.delete", "cron.trigger",
  "models.set", "config.set", "soul.set", "memory.provider.set", "memory.reset",
  "skills.toggle", "toolsets.toggle", "mcp.create", "mcp.delete", "mcp.toggle",
  "channels.update", "secrets.set", "secrets.delete",
]);

function profileWritePolicy(profile: Profile) {
  if (!profile.mutable) return "Solo lectura";
  if (profile.capabilitySet?.methods.some((method) => administrativeWriteMethods.has(method))) {
    return "Conversación y administración";
  }
  return "Conversación completa";
}

export function AgentsScreen() {
  const selectedProfileId = useAppStore((state) => state.selectedProfileId);
  const selectProfile = useAppStore((state) => state.selectProfile);
  const profiles = useAppStore((state) => state.profiles);
  return (
    <div className="page-wrap">
      <PageHeader eyebrow="Perfiles de Hermes" title="Agentes" description="Perfiles descubiertos en la infraestructura Hermes existente. Control no crea ni reemplaza su runtime." />
      <div className="agent-grid">
        {profiles.map((profile) => (
          <Panel key={profile.id} className={cx("agent-card", profile.id === selectedProfileId && "is-selected")}>
            <header><span className="agent-card__avatar"><Robot weight="duotone" /></span><span><strong>{profile.displayName}</strong><small>{profile.technicalName}</small></span><Badge tone={profile.status === "ready" ? "positive" : "warning"}>{profile.status === "ready" ? "Disponible" : profile.status === "busy" ? "Trabajando" : "Sin configurar"}</Badge></header>
            <dl><div><dt>Modelo</dt><dd>{profile.model}</dd></div><div><dt>Funciones disponibles</dt><dd>{profileWritePolicy(profile)}</dd></div></dl>
            <div className="agent-card__actions"><Button variant={profile.id === selectedProfileId ? "primary" : "secondary"} onClick={() => selectProfile(profile.id)}>{profile.id === selectedProfileId ? "Activo" : "Usar agente"}</Button>{profile.capabilities?.config ? <Link to="/config" className="hc-button hc-button--ghost hc-button--md">Ver configuración</Link> : null}</div>
          </Panel>
        ))}
      </div>
      <Panel className="safety-callout"><ShieldCheck size={24} /><div><strong>Experiencia completa, permisos seguros</strong><p>Newton, Jarvis y <code>control-dev</code> pueden crear y reanudar chats, usar herramientas, detener ejecuciones y responder aprobaciones o preguntas cuando Hermes las anuncie. Los cambios destructivos y de infraestructura permanecen aislados en <code>control-dev</code>.</p></div></Panel>
    </div>
  );
}

const automationSchema = z.object({
  name: z.string().trim().min(3, "Escribe un nombre descriptivo"),
  schedule: z.string().trim().refine((value) => value.split(/\s+/).length === 5, "Usa una expresión cron de 5 campos"),
  timezone: z.string().trim().min(3, "Usa una zona IANA, por ejemplo America/Mexico_City"),
  prompt: z.string().trim().min(3, "Describe la tarea que ejecutará Hermes"),
  profileId: z.string().min(1, "Selecciona el perfil aislado"),
});
type AutomationValues = z.infer<typeof automationSchema>;

const emptyAutomationValues: AutomationValues = {
  name: "",
  schedule: "30 8 * * FRI",
  timezone: "America/Mexico_City",
  prompt: "",
  profileId: "",
};

const automationTemplates = [
  {
    id: "morning-brief",
    label: "Resumen matutino",
    name: "Resumen matutino",
    schedule: "30 8 * * MON-FRI",
    prompt: "Prepara un resumen de prioridades, riesgos y próximos pasos para hoy.",
  },
  {
    id: "weekly-review",
    label: "Revisión semanal",
    name: "Revisión semanal",
    schedule: "0 16 * * FRI",
    prompt: "Resume los avances de la semana, decisiones pendientes y riesgos abiertos.",
  },
  {
    id: "daily-monitor",
    label: "Monitoreo diario",
    name: "Monitoreo diario",
    schedule: "0 18 * * *",
    prompt: "Revisa el estado del sistema y reporta únicamente cambios o anomalías relevantes.",
  },
] as const;

export function describeCron(schedule: string, timezone: string): string {
  const [minute, hour, dayOfMonth, month, dayOfWeek] = schedule.trim().split(/\s+/);
  if (![minute, hour, dayOfMonth, month, dayOfWeek].every(Boolean)) return "Expresión cron incompleta";
  const time = /^\d+$/.test(hour) && /^\d+$/.test(minute)
    ? `${String(Number(hour)).padStart(2, "0")}:${String(Number(minute)).padStart(2, "0")}`
    : `${minute} ${hour}`;
  const zone = timezone || "UTC";
  if (dayOfMonth === "*" && month === "*" && dayOfWeek === "MON-FRI") return `De lunes a viernes a las ${time} (${zone})`;
  if (dayOfMonth === "*" && month === "*" && dayOfWeek === "*") return `Todos los días a las ${time} (${zone})`;
  if (dayOfMonth === "*" && month === "*" && /^(MON|TUE|WED|THU|FRI|SAT|SUN)$/.test(dayOfWeek)) {
    const days: Record<string, string> = { MON: "lunes", TUE: "martes", WED: "miércoles", THU: "jueves", FRI: "viernes", SAT: "sábado", SUN: "domingo" };
    return `Cada ${days[dayOfWeek]} a las ${time} (${zone})`;
  }
  return `Cron avanzado: ${schedule.trim()} (${zone})`;
}

function uiAutomation(raw: Automation, profileId: string, previous?: Automation): Automation {
  const nextRuns = raw.nextRuns ?? (raw.nextRun ? [raw.nextRun] : []);
  return {
    ...previous,
    ...raw,
    profileId,
    nextRuns,
    nextRun: nextRuns[0] ?? "Por calcular",
    lastStatus: previous?.lastStatus ?? raw.lastStatus ?? "idle",
  };
}

export function AutomationsScreen() {
  const storeItems = useAppStore((state) => state.automations);
  const profiles = useAppStore((state) => state.profiles);
  const [items, setItems] = useState(storeItems);
  const [editorOpen, setEditorOpen] = useState(false);
  const [advancedEditor, setAdvancedEditor] = useState(false);
  const [editing, setEditing] = useState<Automation | null>(null);
  const [submitError, setSubmitError] = useState("");
  const [actionId, setActionId] = useState("");
  const [runs, setRuns] = useState<Record<string, AutomationRun[]>>({});
  const [runsRefreshVersion, setRunsRefreshVersion] = useState(0);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const demoMode = useAppStore((state) => state.demoMode);
  const offline = useAppStore((state) => state.authState === "offline");
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const selectSession = useAppStore((state) => state.selectSession);
  const navigate = useNavigate();
  const eligibleProfiles = profiles.filter((profile) => profile.mutable && profile.capabilities?.cronCreate);
  useEffect(() => setItems(storeItems), [storeItems]);
  useEffect(() => {
    if (demoMode || items.length === 0) return;
    let active = true;
    let timer: number | undefined;
    let refreshing = false;
    const refresh = async () => {
      if (!active || refreshing) return;
      refreshing = true;
      try {
        const rows = await Promise.all(items.map(async (automation) => [automation.id, await api.automationRuns(automation.id)] as const));
        if (!active) return;
        const next = Object.fromEntries(rows);
        setRuns(next);
        const stillRunning = rows.some(([, automationRuns]) => automationRuns.some((run) => ["accepted", "queued", "running"].includes(run.status)));
        if (stillRunning) timer = window.setTimeout(() => { void refresh(); }, 2_000);
      } catch {
        if (active) timer = window.setTimeout(() => { void refresh(); }, 5_000);
      } finally {
        refreshing = false;
      }
    };
    const onAutomationEvent = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => { void refresh(); }, 200);
    };
    void refresh();
    window.addEventListener("hermes-control:automation-event", onAutomationEvent);
    return () => {
      active = false;
      window.clearTimeout(timer);
      window.removeEventListener("hermes-control:automation-event", onAutomationEvent);
    };
  }, [demoMode, items, runsRefreshVersion]);
  const closeEditor = () => { setEditorOpen(false); setEditing(null); setSubmitError(""); reset(emptyAutomationValues); };
  const editorDialog = useOverlayDialog<HTMLDivElement>({ open: editorOpen, onClose: closeEditor, mediaQuery: "(min-width: 0px)" });
  const { register, handleSubmit, reset, setValue, watch, formState: { errors, isSubmitting } } = useForm<AutomationValues>({ resolver: zodResolver(automationSchema), defaultValues: emptyAutomationValues });
  const watchedSchedule = watch("schedule");
  const watchedTimezone = watch("timezone");
  const openCreate = () => {
    setEditing(null);
    setAdvancedEditor(false);
    setSubmitError("");
    reset({ ...emptyAutomationValues, profileId: eligibleProfiles[0]?.id ?? "" });
    setEditorOpen(true);
  };
  const openEdit = (automation: Automation) => {
    setEditing(automation);
    setAdvancedEditor(true);
    setSubmitError("");
    reset({ name: automation.name, schedule: automation.schedule, timezone: automation.timezone, prompt: automation.prompt ?? "", profileId: automation.profileId });
    setEditorOpen(true);
  };
  const toggle = async (automation: Automation) => {
    if (actionId) return;
    setActionId(automation.id);
    setItems((current) => current.map((item) => item.id === automation.id ? { ...item, enabled: !item.enabled } : item));
    try {
      if (!demoMode) {
        const raw = await api.setAutomationEnabled(automation.id, !automation.enabled, csrfToken);
        setItems((current) => current.map((item) => item.id === automation.id ? uiAutomation(raw, automation.profileId, automation) : item));
      }
    } catch {
      setItems((current) => current.map((item) => item.id === automation.id ? automation : item));
    } finally {
      setActionId("");
    }
  };
  const save = handleSubmit(async (values) => {
    const profile = profiles.find((item) => item.id === values.profileId);
    const requiredCapability = editing ? profile?.capabilities?.cronUpdate : profile?.capabilities?.cronCreate;
    if (!profile?.mutable || !requiredCapability) {
      setSubmitError("Hermes no confirmó esta operación cron para el perfil seleccionado.");
      return;
    }
    setSubmitError("");
    try {
      if (editing) {
        const raw = await api.updateAutomation(editing.id, { name: values.name, schedule: values.schedule, timezone: values.timezone, prompt: values.prompt }, csrfToken);
        setItems((current) => current.map((item) => item.id === editing.id ? uiAutomation(raw, profile.id, editing) : item));
      } else {
        const raw = await api.createAutomation({
          gatewayId: profile.gatewayId,
          profileName: profile.technicalName,
          name: values.name,
          schedule: values.schedule,
          timezone: values.timezone,
          prompt: values.prompt,
          enabled: false,
        }, csrfToken);
        setItems((current) => [uiAutomation(raw, profile.id), ...current]);
      }
      closeEditor();
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : "No se pudo guardar la automatización.");
    }
  });
  const trigger = async (automation: Automation) => {
    if (actionId) return;
    setActionId(automation.id);
    try {
      await api.triggerAutomation(automation.id, csrfToken);
      const [nextRuns, bootstrap] = await Promise.all([api.automationRuns(automation.id), api.bootstrap()]);
      setRuns((current) => ({ ...current, [automation.id]: nextRuns }));
      // The immediate response is commonly queued/running. Restart the bounded
      // polling loop even when no realtime event can yet be routed to a Control
      // session (for example, before the cron run creates its session link).
      setRunsRefreshVersion((version) => version + 1);
      hydrateBootstrap(bootstrap);
      setItems((current) => current.map((item) => item.id === automation.id ? { ...item, lastStatus: nextRuns[0]?.status === "completed" ? "success" : nextRuns[0]?.status === "failed" ? "failed" : "idle" } : item));
    } finally { setActionId(""); }
  };
  const remove = async (automation: Automation) => {
    if (!window.confirm(`Eliminar la automatización “${automation.name}” también en Hermes?`)) return;
    setActionId(automation.id);
    try {
      await api.deleteAutomation(automation.id, csrfToken);
      setItems((current) => current.filter((item) => item.id !== automation.id));
    } finally { setActionId(""); }
  };
  return (
    <div className="page-wrap">
      <PageHeader eyebrow="Ejecuciones programadas" title="Automatizaciones" description="Programa tareas aisladas y revisa cada ejecución sin mezclarlas con tus chats." action={<Button variant="primary" leadingIcon={<Plus />} onClick={openCreate} disabled={!eligibleProfiles.length || offline}>Nueva automatización</Button>} />
      {!eligibleProfiles.length ? <Panel className="safety-callout"><ShieldCheck size={24} /><div><strong>Cron no está disponible</strong><p>El editor permanecerá cerrado hasta que Hermes anuncie la capacidad para algún perfil.</p></div></Panel> : null}
      <div className="next-runs"><span className="eyebrow">Próximas cinco ejecuciones</span><div>{items.filter((item) => item.enabled).flatMap((item) => (item.nextRuns?.length ? item.nextRuns : [item.nextRun]).map((run) => ({ item, run }))).slice(0, 5).map(({ item, run }, index) => <span key={`${item.id}-${run}-${index}`}><Clock /><strong>{run || "Por calcular"}</strong><small>{item.name}</small></span>)}</div></div>
      <div className="automation-list">
        {items.map((automation) => {
          const profile = profiles.find((item) => item.id === automation.profileId);
          const canUpdate = !offline && profile?.mutable === true && Boolean(profile.capabilities?.cronUpdate);
          const canTrigger = !offline && profile?.mutable === true && Boolean(profile.capabilities?.cronTrigger);
          const canDelete = !offline && profile?.mutable === true && Boolean(profile.capabilities?.cronDelete);
          const latestRun = runs[automation.id]?.[0];
          const runLabel = latestRun?.status === "completed" ? "Última ejecución correcta" : latestRun?.status === "failed" ? "Última ejecución falló" : latestRun ? `Ejecución ${latestRun.status}` : "Sin ejecuciones";
          return <Panel key={automation.id} className="automation-row"><span className="automation-row__icon"><Lightning weight="duotone" /></span><div><strong>{automation.name}</strong><p>{describeCron(automation.schedule, automation.timezone)}</p><span><Badge>{profile?.displayName ?? automation.profileName ?? "Perfil"}</Badge><Badge tone={latestRun?.status === "completed" ? "positive" : latestRun?.status === "failed" ? "warning" : "neutral"}>{runLabel}</Badge></span><details className="cron-details"><summary>Ver cron</summary><code>{automation.schedule}</code></details></div><div className="automation-row__end">{canUpdate ? <Switch checked={automation.enabled} disabled={actionId === automation.id} onChange={() => void toggle(automation)} label={automation.enabled ? "Activa" : "Pausada"} /> : null}<span className="automation-actions">{latestRun?.sessionLinkId ? <Button size="sm" variant="ghost" onClick={() => { selectSession(latestRun.sessionLinkId!); void navigate({ to: "/chats" }); }}>Abrir sesión</Button> : null}{canTrigger ? <Button size="sm" variant="ghost" leadingIcon={<Play />} disabled={actionId === automation.id} onClick={() => void trigger(automation)}>Ejecutar</Button> : null}{canUpdate ? <Button size="sm" variant="ghost" leadingIcon={<PencilSimple />} onClick={() => openEdit(automation)}>Editar</Button> : null}{canDelete ? <Button size="sm" variant="ghost" leadingIcon={<Trash />} disabled={actionId === automation.id} onClick={() => void remove(automation)}>Eliminar</Button> : null}</span></div></Panel>;
        })}
      </div>
      {editorOpen ? <div ref={editorDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="automation-editor-title"><button className="modal-scrim" aria-label="Cerrar editor" onClick={closeEditor} /><Panel className="form-modal automation-editor"><span className="eyebrow">Editor {advancedEditor ? "avanzado" : "simple"}</span><h2 id="automation-editor-title">{editing ? "Editar automatización" : "Nueva automatización"}</h2><p>{editing ? "Los cambios se aplicarán en Hermes después de validarlos." : "Se creará pausada para validar sus próximas ejecuciones antes de activarla."}</p><div className="editor-mode" role="group" aria-label="Modo del editor"><button type="button" className={!advancedEditor ? "is-active" : ""} aria-pressed={!advancedEditor} onClick={() => setAdvancedEditor(false)}>Simple</button><button type="button" className={advancedEditor ? "is-active" : ""} aria-pressed={advancedEditor} onClick={() => setAdvancedEditor(true)}>Cron avanzado</button></div><form onSubmit={save}>{!editing ? <fieldset className="automation-templates"><legend>Empezar con una plantilla</legend><div>{automationTemplates.map((template) => <button key={template.id} type="button" onClick={() => { setValue("name", template.name, { shouldValidate: true }); setValue("schedule", template.schedule, { shouldValidate: true }); setValue("prompt", template.prompt, { shouldValidate: true }); }}>{template.label}</button>)}</div></fieldset> : null}<Field label="Nombre" error={errors.name?.message} {...register("name")} /><label className="hc-field"><span>Perfil</span><select {...register("profileId")} disabled={Boolean(editing)}>{eligibleProfiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.displayName} · {profile.technicalName}</option>)}</select>{errors.profileId?.message ? <small className="hc-field__error">{errors.profileId.message}</small> : null}</label>{advancedEditor ? <Field label="Expresión cron · 5 campos" placeholder="30 8 * * FRI" error={errors.schedule?.message} {...register("schedule")} /> : <label className="hc-field"><span>Frecuencia</span><select value={watchedSchedule} onChange={(event) => setValue("schedule", event.target.value, { shouldValidate: true })}><option value="30 8 * * MON-FRI">Lunes a viernes · 08:30</option><option value="0 9 * * *">Todos los días · 09:00</option><option value="0 16 * * FRI">Cada viernes · 16:00</option>{!automationTemplates.some((template) => template.schedule === watchedSchedule) && watchedSchedule !== "0 9 * * *" ? <option value={watchedSchedule}>Horario personalizado actual</option> : null}</select>{errors.schedule?.message ? <small className="hc-field__error">{errors.schedule.message}</small> : null}</label>}<Field label="Zona horaria IANA" error={errors.timezone?.message} {...register("timezone")} /><output className="schedule-explanation" aria-live="polite"><Clock weight="duotone" /><span><strong>Así se ejecutará</strong>{describeCron(watchedSchedule, watchedTimezone)}</span></output><label className="hc-field"><span>Prompt</span><textarea rows={5} {...register("prompt")} />{errors.prompt?.message ? <small className="hc-field__error">{errors.prompt.message}</small> : null}</label>{submitError ? <p className="form-error" role="alert"><WarningCircle /> {submitError}</p> : null}<div><Button type="button" variant="ghost" onClick={closeEditor}>Cancelar</Button><Button type="submit" variant="primary" disabled={isSubmitting}>{isSubmitting ? "Guardando…" : editing ? "Guardar cambios" : "Crear pausada"}</Button></div></form></Panel></div> : null}
    </div>
  );
}

const gatewaySchema = z.object({
  name: z.string().min(2, "Escribe un nombre"),
  restUrl: z.string().url("Usa una URL HTTP válida").refine((value) => value.startsWith("http://") || value.startsWith("https://"), "La URL debe comenzar con http:// o https://"),
  wsUrl: z.string().url("Usa una URL WebSocket válida").refine((value) => value.startsWith("ws://") || value.startsWith("wss://"), "La URL debe comenzar con ws:// o wss://"),
  apiUrl: z.string().url("Usa una URL válida").refine((value) => value.startsWith("http://") || value.startsWith("https://"), "La URL debe comenzar con http:// o https://").optional().or(z.literal("")),
  dashboardToken: z.string().min(12, "La credencial debe tener al menos 12 caracteres").optional().or(z.literal("")),
  apiKey: z.string().min(12, "La credencial debe tener al menos 12 caracteres").optional().or(z.literal("")),
  trustedSourceSha: z.string().regex(/^[0-9a-fA-F]{40}$/, "Usa el SHA Git exacto de 40 caracteres hexadecimales").or(z.literal("")),
});
type GatewayValues = z.infer<typeof gatewaySchema>;
const gatewayTrustSchema = z.object({
  trustedSourceSha: z.string().regex(/^[0-9a-fA-F]{40}$/, "Usa el SHA Git exacto de 40 caracteres hexadecimales"),
});
type GatewayTrustValues = z.infer<typeof gatewayTrustSchema>;

const emptyGatewayValues: GatewayValues = {
  name: "",
  restUrl: "",
  wsUrl: "",
  apiUrl: "",
  dashboardToken: "",
  apiKey: "",
  trustedSourceSha: "",
};

export function GatewaysScreen() {
  const [adding, setAdding] = useState(false);
  const [trustGateway, setTrustGateway] = useState<Gateway | null>(null);
  const [trustBusy, setTrustBusy] = useState(false);
  const [trustError, setTrustError] = useState("");
  const [saved, setSaved] = useState<"connected" | "degraded" | null>(null);
  const [submitError, setSubmitError] = useState("");
  const gateways = useAppStore((state) => state.gateways);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const selectGateway = useAppStore((state) => state.selectGateway);
  const setConnection = useAppStore((state) => state.setConnection);
  const offline = useAppStore((state) => state.authState === "offline");
  const { register, handleSubmit, reset, resetField, formState: { errors, isSubmitting } } = useForm<GatewayValues>({
    resolver: zodResolver(gatewaySchema),
    defaultValues: emptyGatewayValues,
  });
  const {
    register: registerTrust,
    handleSubmit: handleTrustSubmit,
    reset: resetTrust,
    resetField: resetTrustField,
    formState: { errors: trustErrors },
  } = useForm<GatewayTrustValues>({
    resolver: zodResolver(gatewayTrustSchema),
    defaultValues: { trustedSourceSha: "" },
  });
  const openForm = () => {
    reset(emptyGatewayValues);
    setSubmitError("");
    setAdding(true);
  };
  const closeForm = () => {
    reset(emptyGatewayValues);
    setSubmitError("");
    setAdding(false);
  };
  const openTrustForm = (gateway: Gateway) => {
    resetTrust({ trustedSourceSha: "" });
    setTrustError("");
    setTrustGateway(gateway);
  };
  const closeTrustForm = () => {
    resetTrust({ trustedSourceSha: "" });
    setTrustError("");
    setTrustGateway(null);
  };
  const gatewayDialog = useOverlayDialog<HTMLDivElement>({ open: adding, onClose: closeForm, mediaQuery: "(min-width: 0px)" });
  const trustDialog = useOverlayDialog<HTMLDivElement>({ open: Boolean(trustGateway), onClose: closeTrustForm, mediaQuery: "(min-width: 0px)" });
  const submit = handleSubmit(async (values) => {
    let completed = false;
    setSubmitError("");
    try {
      const result = await createAndProvisionGateway({
        name: values.name,
        restUrl: values.restUrl,
        wsUrl: values.wsUrl,
        apiUrl: values.apiUrl || undefined,
        dashboardToken: values.dashboardToken || undefined,
        apiKey: values.apiKey || undefined,
        trustedSourceSha: values.trustedSourceSha ? values.trustedSourceSha.toLowerCase() : undefined,
        connectionMode: "private",
      }, csrfToken);
      hydrateBootstrap(result.bootstrap);
      selectGateway(result.gatewayId);
      setConnection(result.degraded ? "degraded" : "connected");
      setSaved(result.degraded ? "degraded" : "connected");
      completed = true;
    } catch {
      setConnection("degraded");
      setSubmitError("No se pudo guardar el gateway. Las credenciales se descartaron; revisa la conexión e inténtalo de nuevo.");
    } finally {
      // React Hook Form must not retain write-only credentials after any
      // attempt, including validation-safe server failures.
      resetField("dashboardToken", { defaultValue: "" });
      resetField("apiKey", { defaultValue: "" });
      resetField("trustedSourceSha", { defaultValue: "" });
      if (completed) {
        reset(emptyGatewayValues);
        setAdding(false);
      }
    }
  });

  const applyTrust = async (trustedSourceSha: string | null) => {
    if (!trustGateway) return;
    let updated = false;
    let probeFailed = false;
    setTrustBusy(true);
    setTrustError("");
    try {
      await api.updateGateway(trustGateway.id, { trustedSourceSha }, csrfToken);
      updated = true;
      try {
        // Profile refresh performs the real read-only probes and persists the
        // resulting exact CapabilitySet used by every subsequent UI gate.
        await api.refreshProfiles(trustGateway.id, csrfToken);
      } catch {
        probeFailed = true;
      }
      const bootstrap = await api.bootstrap();
      hydrateBootstrap(bootstrap);
      setSaved(probeFailed ? "degraded" : "connected");
      closeTrustForm();
    } catch {
      setTrustError(updated
        ? "La confianza se actualizó, pero no se pudo refrescar el estado. Recarga la aplicación antes de operar."
        : "No se pudo actualizar la confianza contractual. El gateway conserva su estado anterior.");
    } finally {
      // The operator assertion is write-only just like the gateway secrets.
      resetTrustField("trustedSourceSha", { defaultValue: "" });
      setTrustBusy(false);
    }
  };
  const saveTrust = handleTrustSubmit(async (values) => {
    await applyTrust(values.trustedSourceSha.toLowerCase());
  });

  return (
    <div className="page-wrap">
      <PageHeader eyebrow="Infraestructura" title="Gateways" description="Conecta varios entornos sin revelar sus credenciales al resto de la interfaz." action={<Button variant="primary" leadingIcon={<Plus />} onClick={openForm} disabled={offline}>Añadir gateway</Button>} />
      {saved === "connected" ? <div className="success-banner" role="status"><CheckCircle weight="fill" /> Gateway sincronizado. Los valores write-only no se conservaron en el navegador.</div> : null}
      {saved === "degraded" ? <div className="success-banner success-banner--warning" role="status"><WarningCircle weight="fill" /> Gateway guardado, pero el probe de Hermes no respondió por completo. Revisa el diagnóstico antes de operar.</div> : null}
      <div className="gateway-grid">{gateways.map((gateway) => <Panel key={gateway.id} className="gateway-card"><header><span><HardDrives weight="duotone" /></span><div><strong>{gateway.name}</strong><small>{gateway.location}</small></div><Badge tone={gateway.status === "connected" ? "positive" : "warning"}>{gateway.status === "connected" ? "Conectado" : "Degradado"}</Badge></header><div className="gateway-metrics"><span><strong>{gateway.latencyMs ?? "—"} ms</strong><small>latencia</small></span><span><strong>{gateway.version}</strong><small>Hermes</small></span><span><strong>{Object.values(gateway.capabilities).filter(Boolean).length}</strong><small>capacidades</small></span></div><p className="gateway-contract"><ShieldCheck weight="duotone" /><span><strong>{gateway.hasTrustedSourceSha ? "SHA confiable configurado" : "Solo lectura por defecto"}</strong><small>{gateway.hasTrustedSourceSha ? "El diagnóstico confirma por separado si el contrato coincide" : "Añade un SHA auditado para permitir la verificación contractual"}</small></span></p><footer><code>{gateway.sha ?? "SHA no reportado"}</code><span>{gateway.envManaged ? <small>Confianza gestionada por el backend</small> : <Button size="sm" variant="ghost" leadingIcon={<PencilSimple />} onClick={() => openTrustForm(gateway)} disabled={offline}>{gateway.hasTrustedSourceSha ? "Editar confianza" : "Configurar confianza"}</Button>}<Link to="/diagnostics">Diagnóstico <ArrowRight /></Link></span></footer></Panel>)}</div>
      {adding ? <div ref={gatewayDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="gateway-form-title"><button className="modal-scrim" aria-label="Cerrar formulario" onClick={closeForm} /><Panel className="form-modal"><span className="eyebrow">Conexión privada</span><h2 id="gateway-form-title">Añadir gateway</h2><p>Las URLs, credenciales y cualquier SHA confiable se envían solo al backend. Los campos write-only se vacían inmediatamente.</p><form onSubmit={submit}><Field label="Nombre" placeholder="Servidor privado" error={errors.name?.message} {...register("name")} /><Field label="REST dashboard" type="url" placeholder="URL REST privada" error={errors.restUrl?.message} {...register("restUrl")} /><Field label="WebSocket dashboard" type="url" placeholder="URL WebSocket privada" error={errors.wsUrl?.message} {...register("wsUrl")} /><Field label="API fallback (opcional)" type="url" placeholder="URL API privada" error={errors.apiUrl?.message} {...register("apiUrl")} /><Field label="Token dashboard (solo escritura)" type="password" autoComplete="new-password" error={errors.dashboardToken?.message} {...register("dashboardToken")} /><Field label="API key fallback (solo escritura)" type="password" autoComplete="new-password" error={errors.apiKey?.message} {...register("apiKey")} /><Field label="SHA fuente confiable (solo escritura, opcional)" type="password" autoComplete="new-password" placeholder="40 caracteres hexadecimales" error={errors.trustedSourceSha?.message} {...register("trustedSourceSha")} /><p className="form-hint"><ShieldCheck /> Déjalo vacío para crear el gateway en modo seguro de solo lectura. No se confía en el SHA que Hermes anuncie por sí mismo.</p>{submitError ? <p className="form-error" role="alert"><WarningCircle /> {submitError}</p> : null}<div><Button type="button" variant="ghost" onClick={closeForm}>Cancelar</Button><Button type="submit" variant="primary" disabled={isSubmitting}>Guardar cifrado</Button></div></form></Panel></div> : null}
      {trustGateway ? <div ref={trustDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="gateway-trust-title"><button className="modal-scrim" aria-label="Cerrar confianza" onClick={closeTrustForm} /><Panel className="form-modal"><span className="eyebrow">Contrato Hermes</span><h2 id="gateway-trust-title">Confianza de {trustGateway.name}</h2><p>Introduce solo un SHA Git que hayas verificado fuera de Agent Control. El backend lo usa para comparar el contrato auditado y nunca lo devuelve al navegador.</p><div className="form-hint" role="status"><ShieldCheck /><span><strong>{trustGateway.hasTrustedSourceSha ? "Hay un SHA confiable configurado" : "Gateway en solo lectura"}</strong><br />El valor actual permanece oculto; escribir uno nuevo lo reemplaza.</span></div><form onSubmit={saveTrust}><Field label="Nuevo SHA confiable (solo escritura)" type="password" autoComplete="new-password" placeholder="40 caracteres hexadecimales" error={trustErrors.trustedSourceSha?.message} {...registerTrust("trustedSourceSha")} />{trustError ? <p className="form-error" role="alert"><WarningCircle /> {trustError}</p> : null}<div><Button type="button" variant="ghost" onClick={closeTrustForm} disabled={trustBusy}>Cancelar</Button>{trustGateway.hasTrustedSourceSha ? <Button type="button" variant="danger" onClick={() => void applyTrust(null)} disabled={trustBusy}>Volver a solo lectura</Button> : null}<Button type="submit" variant="primary" disabled={trustBusy}>{trustBusy ? "Verificando…" : "Guardar y comprobar"}</Button></div></form></Panel></div> : null}
    </div>
  );
}

export function ConfigScreen() {
  return <AdminConfigScreen header={<PageHeader eyebrow="Administración" title="Configuración de Hermes" description="Interfaz sobre la infraestructura existente: cada control llama al contrato oficial verificado del perfil seleccionado." />} />;
}

export function diagnosticIsOperational(
  connection: string,
  gatewayStatus: Gateway["status"] | undefined,
  readiness: Pick<ReadinessView, "status" | "upstream"> | null,
) {
  return connection === "connected"
    && gatewayStatus === "connected"
    && readiness?.status === "ready"
    && readiness.upstream === "online";
}

export function DiagnosticsScreen() {
  const gatewayId = useAppStore((state) => state.selectedGatewayId);
  const profileId = useAppStore((state) => state.selectedProfileId);
  const connection = useAppStore((state) => state.connection);
  const gateway = useAppStore((state) => state.gateways.find((item) => item.id === gatewayId));
  const profile = useAppStore((state) => state.profiles.find((item) => item.id === profileId));
  const [readiness, setReadiness] = useState<ReadinessView | null>(null);
  useEffect(() => {
    let active = true;
    const refresh = () => { void api.readiness().then((next) => { if (active) setReadiness(next); }).catch(() => { if (active) setReadiness({ status: "not_ready", database: "unavailable", upstream: "unknown", time: new Date().toISOString() }); }); };
    refresh();
    const interval = window.setInterval(refresh, 15_000);
    return () => { active = false; window.clearInterval(interval); };
  }, []);
  const capabilityLabels: Array<[keyof NonNullable<typeof gateway>["capabilities"], string]> = [["realtime", "Realtime RPC"], ["sessions", "Sesiones persistentes"], ["interrupt", "Interrupción"], ["cron", "Cron"], ["profiles", "Perfiles"], ["config", "Configuración"], ["memory", "Memoria"]];
  const capabilities = profile?.capabilities ?? gateway?.capabilities;
  const healthy = diagnosticIsOperational(connection, gateway?.status, readiness);
  const exportReport = () => {
    const payload = {
      generatedAt: new Date().toISOString(),
      connection,
      readiness,
      gateway: gateway ? { id: gateway.id, name: gateway.name, status: gateway.status, version: gateway.version, sha: gateway.sha, capabilities: gateway.capabilities } : null,
      profile: profile ? { id: profile.id, displayName: profile.displayName, technicalName: profile.technicalName, status: profile.status, mutable: profile.mutable, capabilities: profile.capabilities } : null,
    };
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `hermes-control-diagnostico-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };
  return <div className="page-wrap"><PageHeader eyebrow="Estado técnico" title="Diagnóstico" description="Comprueba salud, compatibilidad y transporte sin revelar valores sensibles." action={<Button leadingIcon={<DownloadSimple />} onClick={exportReport}>Exportar reporte saneado</Button>} /><div className="health-hero"><span className="health-hero__icon"><CloudCheck weight="duotone" /></span><div><span className="eyebrow">Estado global</span><h2>{healthy ? "Conexión operativa" : "Conexión degradada"}</h2><p>{gateway?.name ?? "Sin gateway seleccionado"} · {profile?.displayName ?? "sin perfil"}</p></div><Badge tone={healthy ? "positive" : "warning"}>{healthy ? "Operativo" : "Revisar"}</Badge></div><div className="diagnostic-grid"><Panel><header><Pulse /><strong>Conectividad</strong></header><dl><div><dt>Control API</dt><dd><StatusDot tone={readiness?.status === "ready" ? "positive" : "warning"} /> {readiness?.status === "ready" ? "Listo" : readiness ? "No disponible" : "Comprobando"}</dd></div><div><dt>Base local</dt><dd><StatusDot tone={readiness?.database === "ready" ? "positive" : "warning"} /> {readiness?.database ?? "comprobando"}</dd></div><div><dt>Hermes (último probe)</dt><dd><StatusDot tone={readiness?.upstream === "online" ? "positive" : "warning"} /> {readiness?.upstream ?? "comprobando"}</dd></div><div><dt>Gateway</dt><dd><StatusDot tone={gateway?.status === "connected" ? "positive" : "warning"} /> {gateway?.status ?? "desconocido"}</dd></div><div><dt>Realtime</dt><dd><StatusDot tone={connection === "connected" ? "positive" : "warning"} /> {connection}</dd></div></dl></Panel><Panel><header><Code /><strong>Compatibilidad</strong></header><dl><div><dt>Versión detectada</dt><dd>{gateway?.version ?? "desconocida"}</dd></div><div><dt>SHA</dt><dd><code>{gateway?.sha ?? "desconocido"}</code></dd></div><div><dt>Contrato</dt><dd>{capabilities?.realtime ? "dashboard-jsonrpc" : "sin verificar"}</dd></div></dl></Panel></div><Panel className="capability-table"><header><Gauge /><strong>Matriz de capacidades</strong></header><div>{capabilityLabels.map(([key, label]) => <span key={key}>{capabilities?.[key] ? <CheckCircle weight="fill" /> : <WarningCircle />}<strong>{label}</strong><small>{capabilities?.[key] ? "Verificado" : "No anunciado"}</small></span>)}</div></Panel><Panel className="log-preview"><header><TerminalWindow /><strong>Datos técnicos saneados</strong><Badge>sin secretos</Badge></header><pre><code>control={readiness?.status ?? "checking"}{"\n"}database={readiness?.database ?? "checking"}{"\n"}upstream={readiness?.upstream ?? "checking"}{"\n"}gateway={gateway?.name ?? "none"}{"\n"}profile={profile?.technicalName ?? "none"}{"\n"}version={gateway?.version ?? "unknown"}{"\n"}sha={gateway?.sha ?? "unknown"}</code></pre></Panel></div>;
}

export function SearchScreen() {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | SearchResult["kind"]>("all");
  const [remoteResults, setRemoteResults] = useState<SearchResult[]>([]);
  const [partial, setPartial] = useState(false);
  const [loading, setLoading] = useState(false);
  const [searchError, setSearchError] = useState("");
  const viewportRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const authState = useAppStore((state) => state.authState);
  const demoMode = useAppStore((state) => state.demoMode);
  const sessions = useAppStore((state) => state.sessions);
  const workspaces = useAppStore((state) => state.workspaces);
  const automations = useAppStore((state) => state.automations);
  const messages = useAppStore((state) => state.messages);
  const profiles = useAppStore((state) => state.profiles);
  const selectSession = useAppStore((state) => state.selectSession);
  const selectWorkspace = useAppStore((state) => state.selectWorkspace);
  const allResults = useMemo(() => buildSearchResults({ sessions, workspaces, automations, messages, profiles }), [automations, messages, profiles, sessions, workspaces]);
  const localResults = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase("es");
    return allResults.filter((result) => (filter === "all" || result.kind === filter) && (!needle || `${result.title} ${result.excerpt} ${result.meta}`.toLocaleLowerCase("es").includes(needle)));
  }, [allResults, filter, query]);
  const useLocalSearch = demoMode || authState !== "authenticated";
  useEffect(() => {
    const normalized = query.trim();
    if (useLocalSearch || normalized.length < 2) {
      setRemoteResults([]);
      setPartial(false);
      setLoading(false);
      setSearchError("");
      return undefined;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setLoading(true);
      setSearchError("");
      void api.search(normalized, filter, 100, controller.signal)
        .then((response) => {
          setRemoteResults(response.items);
          setPartial(response.partial);
        })
        .catch((error) => {
          if (controller.signal.aborted) return;
          setRemoteResults([]);
          setPartial(false);
          setSearchError(error instanceof Error ? error.message : "No se pudo consultar Hermes.");
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [filter, query, useLocalSearch]);
  const results = useLocalSearch ? localResults : remoteResults;
  const virtualizer = useVirtualizer({ count: results.length, getScrollElement: () => viewportRef.current, estimateSize: () => 84, overscan: 6 });
  const openResult = (result: SearchResult) => {
    if (result.kind === "automation") { void navigate({ to: "/automations" }); return; }
    if (result.kind === "workspace" && result.targetId) selectWorkspace(result.targetId);
    else if (result.targetId) selectSession(result.targetId);
    void navigate({ to: "/chats" });
  };
  const filters: Array<[typeof filter, string]> = [["all", "Todo"], ["message", "Mensajes"], ["session", "Sesiones"], ["workspace", "Workspaces"], ["automation", "Automatizaciones"]];
  const emptyCopy = query.trim().length < 2
    ? "Escribe al menos dos caracteres para buscar en Hermes."
    : loading
      ? "Consultando el índice de Hermes…"
      : searchError || "No hay coincidencias.";
  return <div className="page-wrap search-page"><PageHeader eyebrow="Historial" title="Búsqueda global" description="Consulta el índice de mensajes de Hermes y la organización local de Control." /><label className="search-box"><MagnifyingGlass /><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Buscar por tema, agente o fecha…" /><kbd>⌘ K</kbd></label><div className="search-filters">{filters.map(([value, label]) => <button key={value} type="button" aria-pressed={filter === value} className={filter === value ? "is-active" : ""} onClick={() => setFilter(value)}>{label}</button>)}</div>{partial ? <p className="form-warning" role="status"><WarningCircle /> Resultados parciales: una conexión Hermes no respondió.</p> : null}<div className="virtual-results" ref={viewportRef} aria-busy={loading}><div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>{virtualizer.getVirtualItems().map((row) => { const result = results[row.index]; return <button key={result.id} type="button" className="search-result" style={{ transform: `translateY(${row.start}px)`, height: row.size }} onClick={() => openResult(result)}><span className="search-result__icon">{result.kind === "automation" ? <Lightning /> : result.kind === "workspace" ? <FolderOpen /> : <FileText />}</span><span><strong>{result.title}</strong><small>{result.excerpt}</small></span><span className="search-result__meta">{result.meta}<ArrowRight /></span></button>; })}</div>{results.length === 0 ? <p className="empty-state" role="status">{emptyCopy}</p> : null}</div></div>;
}

export function SettingsScreen() {
  const theme = useAppStore((state) => state.theme);
  const setTheme = useAppStore((state) => state.setTheme);
  const cacheEnabled = useAppStore((state) => state.offlineCacheEnabled);
  const setCacheEnabled = useAppStore((state) => state.setOfflineCacheEnabled);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const userName = useAppStore((state) => state.userName);
  const resetPrivateState = useAppStore((state) => state.resetPrivateState);
  const [loggingOut, setLoggingOut] = useState(false);
  const setThemePreference = (next: ThemePreference) => { setTheme(next); void savePreference("theme", next); };
  const changeCache = (enabled: boolean) => {
    setCacheEnabled(enabled);
    void savePreference("offline-cache", String(enabled));
    if (!enabled) void clearTranscriptCache();
  };
  const logout = async () => {
    setLoggingOut(true);
    try { await api.logout(csrfToken); } catch { /* Local credentials are cleared even if the API is unreachable. */ }
    await clearPrivateCache().catch(() => undefined);
    resetPrivateState();
  };
  return <div className="page-wrap"><PageHeader eyebrow="Preferencias" title="Ajustes" description="Personaliza esta instalación sin guardar secretos en el dispositivo." /><div className="settings-layout"><Panel className="settings-section"><header><SlidersHorizontal /><div><strong>Apariencia</strong><p>El tema oscuro coincide con la experiencia móvil aprobada.</p></div></header><div className="theme-grid">{(["dark", "light", "auto"] as ThemePreference[]).map((option) => <button type="button" key={option} className={theme === option ? "is-active" : ""} onClick={() => setThemePreference(option)}><span className={`theme-preview theme-preview--${option}`}><i /><i /><i /></span><strong>{option === "dark" ? "Oscuro" : option === "light" ? "Claro" : "Automático"}</strong></button>)}</div></Panel><Panel className="settings-section"><header><Database /><div><strong>Disponibilidad sin conexión</strong><p>Solo shell, borradores y una caché cifrada opcional; nunca se reenvían mensajes en segundo plano.</p></div></header><Switch checked={cacheEnabled} onChange={changeCache} label="Caché cifrada del último workspace" description="Máximo 200 elementos, 10 MB y 7 días; sin adjuntos." /><Button variant="ghost" onClick={() => void clearPrivateCache()}>Borrar datos locales</Button></Panel><Panel className="settings-section"><header><UserCircle /><div><strong>Sesión</strong><p>{userName} · autenticación por cookie HttpOnly</p></div></header><Button variant="danger" disabled={loggingOut} onClick={() => void logout()}>{loggingOut ? "Cerrando…" : "Cerrar sesión en este dispositivo"}</Button></Panel></div></div>;
}

const moreItems = [
  { to: "/search", title: "Búsqueda global", description: "Sesiones, mensajes y automatizaciones", icon: MagnifyingGlass },
  { to: "/gateways", title: "Gateways", description: "Conexiones y capacidades", icon: HardDrives },
  { to: "/config", title: "Configuración", description: "Modelos, memoria, tools y MCP", icon: GearSix },
  { to: "/diagnostics", title: "Diagnóstico", description: "Salud, versiones y replay", icon: Pulse },
  { to: "/admin", title: "Seguridad", description: "Acceso, auditoría y respaldos", icon: ShieldCheck },
  { to: "/settings", title: "Preferencias", description: "Tema y datos offline", icon: SlidersHorizontal },
] as const;

export function MoreScreen() {
  return <div className="page-wrap"><PageHeader eyebrow="Centro de control" title="Más" description="Administración y herramientas avanzadas, fuera del flujo principal de chat." /><div className="more-grid">{moreItems.map(({ to, title, description, icon: Icon }) => <Link key={to} to={to}><span><Icon weight="duotone" /></span><div><strong>{title}</strong><small>{description}</small></div><ArrowRight /></Link>)}</div><Panel className="about-panel"><BrandMark size="md" /><div><strong>Agent Control</strong><p>Interfaz segura y móvil para tu infraestructura de agentes.</p></div><Badge>v0.1.0</Badge></Panel></div>;
}

export function AdminScreen() {
  const offline = useAppStore((state) => state.authState === "offline");
  const [auditEvents, setAuditEvents] = useState<Awaited<ReturnType<typeof api.audit>>>([]);
  const [auditOpen, setAuditOpen] = useState(false);
  const [auditBusy, setAuditBusy] = useState(false);
  const [auditError, setAuditError] = useState("");

  const toggleAudit = async () => {
    if (auditOpen) {
      setAuditOpen(false);
      return;
    }
    setAuditOpen(true);
    if (auditEvents.length || offline) return;
    setAuditBusy(true);
    setAuditError("");
    try {
      setAuditEvents(await api.audit());
    } catch {
      setAuditError("No se pudo cargar la auditoría saneada.");
    } finally {
      setAuditBusy(false);
    }
  };

  return (
    <div className="page-wrap">
      <PageHeader eyebrow="Administración" title="Seguridad y operación" description="Controles locales para acceso, auditoría, respaldo y recuperación." />
      <div className="admin-grid">
        <Panel><ShieldCheck weight="duotone" /><h2>Conversaciones protegidas</h2><p>Cookies HttpOnly, SameSite estricto, CSRF y validación de origen.</p><a href="/chats" className="hc-button hc-button--ghost hc-button--md">Abrir conversaciones</a></Panel>
        <Panel><Key weight="duotone" /><h2>Vault cifrado</h2><p>AES-GCM con una clave maestra que permanece fuera de la base de datos.</p><Badge tone="positive">Configurado en backend</Badge></Panel>
        <Panel><Database weight="duotone" /><h2>Backup y restore</h2><p>SQLite y clave maestra se respaldan por canales separados.</p><code className="runbook-reference">docs/operations/backup-restore.md</code></Panel>
        <Panel><FileText weight="duotone" /><h2>Auditoría</h2><p>Acciones sensibles registradas con datos técnicos saneados.</p><Button variant="ghost" onClick={() => void toggleAudit()} disabled={offline}>{auditOpen ? "Cerrar eventos" : "Abrir eventos"}</Button></Panel>
      </div>
      {auditOpen ? (
        <Panel className="audit-panel" aria-live="polite">
          <header><div><span className="eyebrow">Registro local de Control</span><h2>Eventos recientes</h2></div><Badge>{auditEvents.length}</Badge></header>
          {auditBusy ? <p>Cargando eventos…</p> : auditError ? <p className="form-error" role="alert"><WarningCircle /> {auditError}</p> : auditEvents.length ? (
            <ol>{auditEvents.map((event) => <li key={event.id}><span><strong>{event.action}</strong><small>{event.targetType ?? "control"}{event.targetId ? ` · ${event.targetId}` : ""}</small></span><span><Badge tone={event.outcome === "success" ? "positive" : "warning"}>{event.outcome}</Badge><time dateTime={event.createdAt}>{new Date(event.createdAt).toLocaleString()}</time></span></li>)}</ol>
          ) : <p>No hay eventos registrados.</p>}
        </Panel>
      ) : null}
    </div>
  );
}
