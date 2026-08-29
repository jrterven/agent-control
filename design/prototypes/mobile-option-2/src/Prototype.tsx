import { useMemo, useState } from "react";
import {
  ActivityLogIcon,
  ChatBubbleIcon,
  CheckIcon,
  ChevronDownIcon,
  Cross2Icon,
  DashboardIcon,
  DotsHorizontalIcon,
  FileTextIcon,
  GearIcon,
  HamburgerMenuIcon,
  LightningBoltIcon,
  Link2Icon,
  MagnifyingGlassIcon,
  PaperPlaneIcon,
  PersonIcon,
  PlusIcon,
  SpeakerLoudIcon,
  TokensIcon,
} from "@radix-ui/react-icons";
import {
  BottomSheet,
  KeyboardTextarea,
  MobileScroll,
  useKeyboard,
  useKeyboardInsets,
} from "./mobile";

type Tab = "chats" | "agents" | "automations" | "more";

const navigation = [
  { id: "chats" as const, label: "Chats", icon: ChatBubbleIcon },
  { id: "agents" as const, label: "Agentes", icon: PersonIcon },
  { id: "automations" as const, label: "Automatizaciones", icon: LightningBoltIcon },
  { id: "more" as const, label: "Más", icon: DotsHorizontalIcon },
];

export default function Prototype() {
  const [activeTab, setActiveTab] = useState<Tab>("chats");
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [contextOpen, setContextOpen] = useState(false);
  const [toolsOpen, setToolsOpen] = useState(false);
  const [composer, setComposer] = useState("");
  const [sentMessage, setSentMessage] = useState<string | null>(null);
  const keyboard = useKeyboard();
  const { bottomInset, isKeyboardVisible } = useKeyboardInsets();

  const composerBottom = useMemo(
    () => bottomInset + (isKeyboardVisible ? 10 : 30),
    [bottomInset, isKeyboardVisible],
  );

  const sendMessage = () => {
    const next = composer.trim();
    if (!next) return;
    setSentMessage(next);
    setComposer("");
    keyboard.hide();
  };

  return (
    <div className="hermes-app" data-testid="hermes-prototype">
      <MobileScroll className="hermes-scroll">
        <main className="chat-feed" aria-label="Conversación con Newton">
          <div className="conversation-heading">
            <button className="workspace-button" type="button">
              <span>Revisión semanal de papers</span>
              <ChevronDownIcon aria-hidden="true" />
            </button>
            <div className="date-divider" aria-label="28 de agosto de 2026">
              <span>28 de agosto de 2026</span>
            </div>
          </div>

          <article className="user-message" aria-label="Mensaje enviado a las 10:14">
            <div className="message-meta">
              <span>10:14</span>
              <CheckIcon aria-hidden="true" />
            </div>
            <p>
              Compara los papers más recientes sobre memoria de agentes publicados en agosto 2026. Enfócate en
              métodos, benchmarks y resultados clave. Incluye fortalezas, limitaciones y una recomendación práctica.
            </p>
          </article>

          <article className="assistant-message">
            <header className="assistant-author">
              <span className="agent-avatar" aria-hidden="true"><TokensIcon /></span>
              <strong>Newton</strong>
              <time>10:14</time>
            </header>
            <div className="assistant-copy">
              <p>Entendido. Analizaré la literatura reciente de agosto 2026 sobre memoria de agentes.</p>
              <h1>Comparativa de memoria de agentes — Agosto 2026</h1>
              <p>Resumen de los enfoques más recientes y su rendimiento en benchmarks comunes.</p>
              <ul>
                <li><strong>Enfoques principales:</strong> memorias episódicas, jerárquicas y vectoriales con recuperación híbrida.</li>
                <li><strong>Benchmarks clave:</strong> MemoryBench v2, AgentMem-Long, Needle-in-a-Haystack (1M), Mind2Web.</li>
                <li><strong>Hallazgos:</strong> las arquitecturas híbridas lideran en retención a largo plazo y recuperación precisa.</li>
                <li><strong>Recomendación:</strong> usar memoria jerárquica + recuperación vectorial con reescritura selectiva.</li>
              </ul>
            </div>

            <button
              className="tools-toggle"
              type="button"
              aria-expanded={toolsOpen}
              onClick={() => setToolsOpen((open) => !open)}
            >
              <span>Herramientas · 2</span>
              <ChevronDownIcon aria-hidden="true" />
            </button>
            {toolsOpen ? (
              <div className="tool-list" aria-label="Herramientas utilizadas">
                <div><MagnifyingGlassIcon aria-hidden="true" /><span><strong>Buscar papers</strong><small>12 resultados revisados</small></span><CheckIcon /></div>
                <div><FileTextIcon aria-hidden="true" /><span><strong>Sintetizar hallazgos</strong><small>Resumen estructurado</small></span><CheckIcon /></div>
              </div>
            ) : null}
            <p className="typing-state"><span>Newton está escribiendo</span><DotsHorizontalIcon aria-hidden="true" /></p>
          </article>

          {sentMessage ? (
            <article className="user-message user-message-new" aria-label="Mensaje recién enviado">
              <div className="message-meta"><span>Ahora</span><CheckIcon aria-hidden="true" /></div>
              <p>{sentMessage}</p>
            </article>
          ) : null}
        </main>
      </MobileScroll>

      <header className="top-bar">
        <button className="icon-button menu-button" type="button" aria-label="Abrir navegación" onClick={() => setDrawerOpen(true)}>
          <HamburgerMenuIcon />
          <span className="notification-dot" aria-hidden="true" />
        </button>
        <button className="agent-switcher" type="button" aria-label="Cambiar agente">
          <span className="agent-avatar"><TokensIcon /></span>
          <span className="agent-title"><strong>Newton</strong><small><i aria-hidden="true" />Conectado</small></span>
          <ChevronDownIcon aria-hidden="true" />
        </button>
        <button className="icon-button" type="button" aria-label="Abrir actividad" onClick={() => setContextOpen(true)}>
          <ActivityLogIcon />
        </button>
      </header>

      <aside className="edge-handle" aria-hidden="true"><ChevronDownIcon /></aside>

      <section className="composer" style={{ bottom: composerBottom }} aria-label="Componer mensaje">
        <KeyboardTextarea
          value={composer}
          onChange={(event) => setComposer(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              sendMessage();
            }
          }}
          placeholder="Mensaje a Newton…"
          aria-label="Mensaje a Newton"
          rows={1}
        />
        <button type="button" className="composer-action" aria-label="Adjuntar archivo"><Link2Icon /></button>
        <button type="button" className="composer-action microphone" aria-label="Dictar mensaje"><SpeakerLoudIcon /></button>
        <button type="button" className="send-button" aria-label="Enviar mensaje" onClick={sendMessage}>
          <PaperPlaneIcon />
        </button>
      </section>

      {!isKeyboardVisible ? (
        <nav className="bottom-nav" style={{ paddingBottom: bottomInset }} aria-label="Navegación principal">
          {navigation.map(({ id, label, icon: Icon }) => (
            <button key={id} type="button" className={activeTab === id ? "active" : ""} onClick={() => setActiveTab(id)}>
              <Icon aria-hidden="true" />
              <span>{label}</span>
            </button>
          ))}
        </nav>
      ) : null}

      <BottomSheet open={drawerOpen} onOpenChange={setDrawerOpen} title="Navegación" description="Tus agentes y espacios" snap={0.78}>
        <div className="sheet-stack">
          <button className="sheet-search" type="button"><MagnifyingGlassIcon /><span>Buscar en Hermes Control</span></button>
          <section>
            <h2>Agentes</h2>
            <button className="sheet-row selected" type="button"><TokensIcon /><span><strong>Newton</strong><small>Perfil default · conectado</small></span><CheckIcon /></button>
            <button className="sheet-row" type="button"><TokensIcon /><span><strong>Jarvis</strong><small>Perfil activo · conectado</small></span></button>
            <button className="sheet-row" type="button"><PlusIcon /><span><strong>control-dev</strong><small>Entorno seguro de pruebas</small></span></button>
          </section>
          <section>
            <h2>Workspaces</h2>
            <button className="sheet-row selected" type="button"><DashboardIcon /><span><strong>Revisión semanal de papers</strong><small>3 sesiones</small></span></button>
            <button className="sheet-row" type="button"><FileTextIcon /><span><strong>Investigación personal</strong><small>8 sesiones</small></span></button>
          </section>
        </div>
      </BottomSheet>

      <BottomSheet open={contextOpen} onOpenChange={setContextOpen} title="Actividad" description="Estado de esta sesión" snap={0.68}>
        <div className="context-grid">
          <article><small>Conexión</small><strong className="healthy">Estable</strong><span>32 ms · replay al día</span></article>
          <article><small>Contexto</small><strong>38%</strong><span>76k de 200k tokens</span></article>
          <article><small>Herramientas</small><strong>2 activas</strong><span>Sin aprobaciones pendientes</span></article>
          <article><small>Sesión</small><strong>Persistente</strong><span>Newton · default</span></article>
        </div>
        <button className="danger-action" type="button"><Cross2Icon /> Detener ejecución</button>
        <button className="settings-action" type="button"><GearIcon /> Configuración de la sesión</button>
      </BottomSheet>
    </div>
  );
}
