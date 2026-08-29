import type { Automation, ChatMessage, Gateway, Profile, SearchResult, SessionSummary, Workspace } from "./types";

export const gateways: Gateway[] = [
  {
    id: "gateway-home",
    name: "gx10-58f9",
    location: "Tailscale · casa",
    status: "connected",
    latencyMs: 24,
    version: "0.20.5",
    sha: "791e2ae",
    capabilities: { realtime: true, sessions: true, prompts: true, interrupt: true, cron: true, cronCreate: true, cronUpdate: true, cronDelete: true, cronTrigger: true, profiles: true, config: true, memory: true },
  },
  {
    id: "gateway-mock",
    name: "Mock local",
    location: "Desarrollo sin conexión",
    status: "connected",
    latencyMs: 3,
    version: "mock-1",
    sha: "deterministic",
    capabilities: { realtime: true, sessions: true, prompts: true, interrupt: true, cron: true, cronCreate: true, cronUpdate: true, cronDelete: true, cronTrigger: true, profiles: true, config: true, memory: true },
  },
];

export const profiles: Profile[] = [
  { id: "profile-newton", gatewayId: "gateway-home", technicalName: "default", displayName: "Newton", model: "gpt-5.6-sol", status: "ready", mutable: false, capabilities: gateways[0].capabilities },
  { id: "profile-jarvis", gatewayId: "gateway-home", technicalName: "jarvis", displayName: "Jarvis", model: "gpt-5.6-sol", status: "ready", mutable: false, capabilities: gateways[0].capabilities },
  { id: "profile-control-dev", gatewayId: "gateway-home", technicalName: "control-dev", displayName: "Control Dev", model: "sin configurar", status: "offline", mutable: true, capabilities: gateways[0].capabilities },
];

export const workspaces: Workspace[] = [
  { id: "workspace-papers", name: "Revisión semanal de papers", description: "Literatura, benchmarks y recomendaciones", sessionCount: 4, updatedAt: "2026-08-28T10:14:00Z" },
  { id: "workspace-platform", name: "Agent Control", description: "Arquitectura, seguridad y lanzamiento", sessionCount: 7, updatedAt: "2026-08-27T18:40:00Z" },
  { id: "workspace-personal", name: "Personal", description: "Consultas y recordatorios", sessionCount: 2, updatedAt: "2026-08-26T08:10:00Z" },
];

export const sessions: SessionSummary[] = [
  { id: "session-papers", storedSessionId: "papers-a0f3", runtimeSessionId: "7fe09b2a", workspaceId: "workspace-papers", profileId: "profile-newton", title: "Memoria de agentes · agosto", preview: "Comparativa de enfoques y benchmarks…", updatedAt: "10:14", unread: true },
  { id: "session-evals", storedSessionId: "evals-9112", workspaceId: "workspace-papers", profileId: "profile-jarvis", title: "Evaluaciones de navegación", preview: "Resultados sobre Mind2Web y WebArena…", updatedAt: "Ayer" },
  { id: "session-architecture", storedSessionId: "architecture-26ab", runtimeSessionId: "48dbe23c", workspaceId: "workspace-platform", profileId: "profile-newton", title: "Diseño de reconexión", preview: "Replay, epoch y deduplicación de eventos…", updatedAt: "Ayer" },
  { id: "session-security", storedSessionId: "security-88d1", workspaceId: "workspace-platform", profileId: "profile-jarvis", title: "Threat model", preview: "SSRF, secretos write-only y auditoría…", updatedAt: "Lun" },
];

export const initialMessages: ChatMessage[] = [
  {
    id: "message-user-1",
    sessionId: "session-papers",
    role: "user",
    content: "Compara los papers más recientes sobre memoria de agentes publicados en agosto 2026. Enfócate en métodos, benchmarks y resultados clave. Incluye fortalezas, limitaciones y una recomendación práctica.",
    createdAt: "10:14",
    delivery: "sent",
  },
  {
    id: "message-assistant-1",
    sessionId: "session-papers",
    role: "assistant",
    content: "Entendido. Analizaré la literatura reciente de agosto 2026 sobre memoria de agentes.\n\n## Comparativa de memoria de agentes — Agosto 2026\n\nResumen de los enfoques más recientes y su rendimiento en benchmarks comunes.\n\n- **Enfoques principales:** memorias episódicas, jerárquicas y vectoriales con recuperación híbrida.\n- **Benchmarks clave:** MemoryBench v2, AgentMem-Long, Needle-in-a-Haystack (1M), Mind2Web.\n- **Hallazgos:** las arquitecturas híbridas lideran en retención a largo plazo y recuperación precisa.\n- **Recomendación:** usar memoria jerárquica + recuperación vectorial con reescritura selectiva.",
    createdAt: "10:14",
    tools: [
      { id: "tool-search", name: "web_search", label: "Búsqueda académica", status: "completed", durationMs: 1830, summary: "18 fuentes revisadas" },
      { id: "tool-read", name: "paper_reader", label: "Lectura de papers", status: "completed", durationMs: 4270, summary: "6 papers comparados" },
    ],
  },
];

export const automations: Automation[] = [
  { id: "cron-weekly", name: "Resumen semanal de papers", schedule: "Cada viernes · 08:30", timezone: "America/Mexico_City", profileId: "profile-control-dev", enabled: true, nextRun: "Vie 4 sep · 08:30", lastStatus: "success" },
  { id: "cron-health", name: "Revisión de gateways", schedule: "Cada 6 horas", timezone: "America/Mexico_City", profileId: "profile-control-dev", enabled: true, nextRun: "Hoy · 14:00", lastStatus: "success" },
  { id: "cron-memory", name: "Mantenimiento de memoria", schedule: "Domingos · 03:00", timezone: "America/Mexico_City", profileId: "profile-control-dev", enabled: false, nextRun: "Pausada", lastStatus: "idle" },
];

export const searchResults: SearchResult[] = [
  { id: "sr1", kind: "message", title: "Comparativa de memoria de agentes", excerpt: "Las arquitecturas híbridas lideran en retención a largo plazo…", meta: "Newton · Hoy" },
  { id: "sr2", kind: "session", title: "Diseño de reconexión", excerpt: "Replay, epoch y deduplicación de eventos…", meta: "Agent Control · Ayer" },
  { id: "sr3", kind: "workspace", title: "Revisión semanal de papers", excerpt: "4 sesiones · Literatura, benchmarks y recomendaciones", meta: "Workspace" },
  { id: "sr4", kind: "automation", title: "Resumen semanal de papers", excerpt: "Cada viernes a las 08:30", meta: "Control Dev · Activa" },
  ...Array.from({ length: 28 }, (_, index) => ({
    id: `generated-${index}`,
    kind: (index % 2 === 0 ? "message" : "session") as "message" | "session",
    title: index % 2 === 0 ? `Hallazgo de memoria ${index + 1}` : `Sesión de investigación ${index + 1}`,
    excerpt: "Coincidencia contextual en el historial de Hermes, recuperada sin duplicar el contenido.",
    meta: index % 3 === 0 ? "Newton · Agosto" : "Jarvis · Agosto",
  })),
];
