import { CaretDown, Check, Checks, PaperPlaneTilt, Question, ShieldWarning, Stop, WarningCircle, Wrench } from "@phosphor-icons/react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { Badge, Button, IconButton } from "@hermes-control/ui";
import { respondToApproval, respondToClarification, stopPrompt, submitPrompt, useSessionDraft } from "../hooks";
import { useAppStore } from "../store/appStore";
import type { ApprovalChoice, ApprovalRequest, ChatMessage, ClarificationQuestion, ClarificationRequest } from "../types";
import { BrandMark } from "./BrandMark";

const emptyApprovals: ApprovalRequest[] = [];
const emptyClarifications: ClarificationRequest[] = [];
const approvalLabels: Record<ApprovalChoice, string> = {
  once: "Permitir una vez",
  session: "Durante esta sesión",
  always: "Permitir siempre",
  deny: "Rechazar",
};

function DeliveryIcon({ delivery }: { delivery?: ChatMessage["delivery"] }) {
  if (delivery === "ambiguous" || delivery === "failed") return <WarningCircle aria-label="Entrega sin confirmar" />;
  if (delivery === "sent") return <Checks aria-label="Entregado" />;
  return <Check aria-label="Enviando" />;
}

function ToolCards({ tools }: { tools: NonNullable<ChatMessage["tools"]> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="tool-cards">
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>
        <span><Wrench size={17} /> Herramientas · {tools.length}</span><CaretDown size={17} className={open ? "is-open" : ""} />
      </button>
      {open ? <div className="tool-cards__list">{tools.map((tool) => <div key={tool.id}><span className={`tool-state tool-state--${tool.status}`} /><span><strong>{tool.label}</strong><small>{tool.summary}{tool.durationMs ? ` · ${(tool.durationMs / 1000).toFixed(1)} s` : ""}</small></span><Badge tone={tool.status === "completed" ? "positive" : tool.status === "failed" ? "warning" : "info"}>{tool.status === "completed" ? "Listo" : tool.status === "failed" ? "Error" : "En curso"}</Badge></div>)}</div> : null}
    </div>
  );
}

function ApprovalCard({ request, offline, canRespond }: { request: ApprovalRequest; offline: boolean; canRespond: boolean }) {
  const headingId = useId();
  const busy = request.state === "submitting";
  const ambiguous = request.state === "ambiguous";
  const blocked = busy || ambiguous || offline || !canRespond;
  return (
    <section className="interaction-card interaction-card--approval" aria-labelledby={headingId}>
      <header>
        <span className="interaction-card__icon"><ShieldWarning size={22} weight="fill" /></span>
        <span>
          <span className="eyebrow">Acción detenida</span>
          <h2 id={headingId}>Aprobación requerida</h2>
        </span>
        <Badge tone="warning">{ambiguous ? "Sin confirmar" : "Esperando"}</Badge>
      </header>
      {request.description ? <p className="interaction-card__description">{request.description}</p> : null}
      {request.command ? <pre className="interaction-card__command"><code>{request.command}</code></pre> : null}
      {request.smartDenied ? <p className="interaction-card__notice">La revisión de seguridad bloqueó esta acción. Solo puedes permitirla una vez o rechazarla.</p> : null}
      <div className="interaction-card__actions" aria-label="Opciones de aprobación">
        {request.choices.map((choice) => (
          <Button
            key={choice}
            variant={choice === "deny" ? "danger" : choice === "once" ? "primary" : "secondary"}
            disabled={blocked}
            aria-busy={busy || undefined}
            onClick={() => void respondToApproval(request.sessionId, request.requestId, choice).catch(() => undefined)}
          >
            {busy ? "Confirmando…" : approvalLabels[choice]}
          </Button>
        ))}
      </div>
      {offline ? <p className="interaction-card__notice">Recupera la conexión para responder de forma segura.</p> : null}
      {!offline && !canRespond ? <p className="interaction-card__notice">La respuesta requiere un perfil habilitado por el backend y la capacidad approval.respond verificada.</p> : null}
      {request.error ? <p className="interaction-card__error" role="alert">{request.error}</p> : null}
    </section>
  );
}

function readableAnswer(answer: string) {
  try {
    const parsed = JSON.parse(answer) as unknown;
    if (Array.isArray(parsed) && parsed.every((item) => typeof item === "string")) return parsed.join(", ");
  } catch {
    // Plain text is the canonical single-answer representation.
  }
  return answer || "Omitida";
}

function ClarificationQuestionForm({
  request,
  question,
  index,
  offline,
  canRespond,
}: {
  request: ClarificationRequest;
  question: ClarificationQuestion;
  index: number;
  offline: boolean;
  canRespond: boolean;
}) {
  const headingId = useId();
  const [text, setText] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [customAnswer, setCustomAnswer] = useState(false);
  const answerKey = question.questionId ?? "single";
  const answered = request.batch && Object.prototype.hasOwnProperty.call(request.answers, answerKey);
  const busy = request.state === "submitting" && request.submittingQuestionId === answerKey;
  const blocked = request.state === "submitting" || request.state === "ambiguous" || offline || !canRespond || answered;
  const customText = text.trim();
  const answer: string | string[] = question.choices.length
    ? customAnswer
      ? question.multiSelect ? [...selected, customText].filter(Boolean) : customText
      : question.multiSelect ? selected : selected[0] ?? ""
    : text.trim();
  const hasAnswer = customAnswer
    ? Boolean(customText)
    : Array.isArray(answer) ? answer.length > 0 : Boolean(answer);

  const toggleChoice = (choice: string) => {
    if (question.multiSelect) {
      setSelected((current) => current.includes(choice)
        ? current.filter((item) => item !== choice)
        : [...current, choice]);
    } else {
      setSelected([choice]);
      setCustomAnswer(false);
    }
  };
  const toggleCustomAnswer = () => {
    if (question.multiSelect) {
      setCustomAnswer((current) => !current);
    } else {
      setSelected([]);
      setCustomAnswer(true);
    }
  };
  const submit = (value: string | string[]) => {
    if (blocked) return;
    void respondToClarification(
      request.sessionId,
      request.requestId,
      value,
      request.batch ? question.questionId : undefined,
    ).catch(() => undefined);
  };

  return (
    <div className={`clarification-question${answered ? " is-answered" : ""}`} aria-labelledby={headingId}>
      <div className="clarification-question__heading">
        <span>{request.batch ? index + 1 : <Question size={18} weight="bold" />}</span>
        <h3 id={headingId}>{question.question}</h3>
        {answered ? <Badge tone="positive">Respondida</Badge> : null}
      </div>
      {answered ? <p className="clarification-question__answer"><Check weight="bold" /> {readableAnswer(request.answers[answerKey])}</p> : (
        <>
          {question.choices.length ? (
            <div
              className="clarification-choices"
              role={question.multiSelect ? "group" : "radiogroup"}
              aria-label={question.multiSelect ? "Selecciona una o más opciones" : "Selecciona una opción"}
            >
              {question.choices.map((choice) => {
                const active = selected.includes(choice);
                return (
                  <button
                    key={choice}
                    type="button"
                    className={active ? "is-selected" : ""}
                    role={question.multiSelect ? undefined : "radio"}
                    aria-checked={question.multiSelect ? undefined : active}
                    aria-pressed={question.multiSelect ? active : undefined}
                    disabled={blocked}
                    onClick={() => toggleChoice(choice)}
                  >
                    <span className="clarification-choice__mark">{active ? <Check weight="bold" /> : null}</span>
                    <span>{choice}</span>
                  </button>
                );
              })}
              <button
                type="button"
                className={customAnswer ? "is-selected" : ""}
                role={question.multiSelect ? undefined : "radio"}
                aria-checked={question.multiSelect ? undefined : customAnswer}
                aria-pressed={question.multiSelect ? customAnswer : undefined}
                disabled={blocked}
                onClick={toggleCustomAnswer}
              >
                <span className="clarification-choice__mark">{customAnswer ? <Check weight="bold" /> : null}</span>
                <span>Otra respuesta</span>
              </button>
            </div>
          ) : (
            <textarea
              className="clarification-answer"
              rows={3}
              value={text}
              disabled={blocked}
              aria-label={`Respuesta: ${question.question}`}
              placeholder="Escribe una respuesta"
              onChange={(event) => setText(event.target.value)}
            />
          )}
          {question.choices.length && customAnswer ? (
            <textarea
              className="clarification-answer"
              rows={3}
              value={text}
              disabled={blocked}
              aria-label={`Otra respuesta: ${question.question}`}
              placeholder={question.multiSelect ? "Añade otra opción" : "Escribe otra respuesta"}
              onChange={(event) => setText(event.target.value)}
            />
          ) : null}
          <div className="clarification-question__actions">
            <Button variant="primary" disabled={blocked || !hasAnswer} aria-busy={busy || undefined} onClick={() => submit(answer)}>
              {busy ? "Enviando…" : request.batch ? "Confirmar respuesta" : "Responder"}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}

function ClarificationCard({ request, offline, canRespond }: { request: ClarificationRequest; offline: boolean; canRespond: boolean }) {
  const headingId = useId();
  return (
    <section className="interaction-card interaction-card--clarification" aria-labelledby={headingId}>
      <header>
        <span className="interaction-card__icon"><Question size={22} weight="fill" /></span>
        <span>
          <span className="eyebrow">Hermes necesita contexto</span>
          <h2 id={headingId}>{request.batch ? `${request.questions.length} preguntas` : "Una pregunta antes de continuar"}</h2>
        </span>
        <Badge tone={request.state === "ambiguous" ? "warning" : "info"}>{request.state === "ambiguous" ? "Sin confirmar" : "Esperando"}</Badge>
      </header>
      <div className="clarification-list">
        {request.questions.map((question, index) => (
          <ClarificationQuestionForm
            key={question.questionId ?? "single"}
            request={request}
            question={question}
            index={index}
            offline={offline}
            canRespond={canRespond}
          />
        ))}
      </div>
      {offline ? <p className="interaction-card__notice">Recupera la conexión para responder.</p> : null}
      {!offline && !canRespond ? <p className="interaction-card__notice">La respuesta requiere un perfil habilitado por el backend y la capacidad clarify.respond verificada.</p> : null}
      {request.error ? <p className="interaction-card__error" role="alert">{request.error}</p> : null}
    </section>
  );
}

function InteractionCards({ approvals, clarifications, offline, canApprove, canClarify }: {
  approvals: ApprovalRequest[];
  clarifications: ClarificationRequest[];
  offline: boolean;
  canApprove: boolean;
  canClarify: boolean;
}) {
  if (!approvals.length && !clarifications.length) return null;
  return (
    <div className="interaction-stack" aria-live="polite" aria-label="Hermes espera tu respuesta">
      {approvals.map((request) => <ApprovalCard key={request.requestId} request={request} offline={offline} canRespond={canApprove} />)}
      {clarifications.map((request) => <ClarificationCard key={request.requestId} request={request} offline={offline} canRespond={canClarify} />)}
    </div>
  );
}

function Message({ message, agentName }: { message: ChatMessage; agentName: string }) {
  if (message.role === "user") {
    return (
      <article className="message message--user" aria-label="Tu mensaje">
        <div className="user-bubble"><span className="message-time">{message.createdAt} <DeliveryIcon delivery={message.delivery} /></span><p>{message.content}</p></div>
        {message.delivery === "ambiguous" ? <p className="delivery-warning"><WarningCircle /> No se confirmó la entrega; no se reenviará automáticamente.</p> : null}
      </article>
    );
  }
  return (
    <article className="message message--assistant" aria-label={`Respuesta de ${agentName}`}>
      <div className="assistant-avatar"><BrandMark size="sm" /></div>
      <div className="assistant-content">
        <header><strong>{agentName}</strong><time>{message.createdAt}</time>{message.streaming ? <Badge tone="info">En curso</Badge> : null}</header>
        <div className="markdown-body"><ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>{message.content || " "}</ReactMarkdown>{message.streaming ? <span className="stream-caret" aria-hidden="true" /> : null}</div>
        {message.tools?.length ? <ToolCards tools={message.tools} /> : null}
      </div>
    </article>
  );
}

function Composer({ agentName, sessionId, canInterrupt, offline = false }: { agentName: string; sessionId: string; canInterrupt: boolean; offline?: boolean }) {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const streamingMessageId = useAppStore((state) => state.streamingBySession[sessionId]);
  const draft = useSessionDraft(sessionId);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    let active = true;
    setValue("");
    void draft.load().then((loaded) => { if (active) setValue(loaded); });
    return () => { active = false; };
  }, [sessionId]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, 132)}px`;
  }, [value]);

  const onSubmit = async () => {
    const next = value.trim();
    if (!next || streamingMessageId || offline) return;
    setValue("");
    await draft.clear();
    await submitPrompt(next);
  };

  return (
    <div className="composer-wrap">
      <div className="composer">
        <textarea
          ref={textareaRef}
          rows={1}
          value={value}
          aria-label={t("messagePlaceholder", { agent: agentName })}
          placeholder={t("messagePlaceholder", { agent: agentName })}
          onChange={(event) => { setValue(event.target.value); draft.save(event.target.value); }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void onSubmit(); }
          }}
        />
        <div className="composer__actions">
          <span />
          {offline ? <Badge tone="warning">Borrador offline</Badge> : streamingMessageId ? (canInterrupt ? <Button variant="danger" size="sm" leadingIcon={<Stop weight="fill" />} onClick={() => void stopPrompt()}>Detener</Button> : <Badge tone="info">En ejecución</Badge>) : <IconButton className="send-button" label="Enviar mensaje" disabled={!value.trim()} icon={<PaperPlaneTilt size={22} weight="fill" />} onClick={() => void onSubmit()} />}
        </div>
      </div>
      <p className="composer-note">{offline ? "El borrador queda en este dispositivo y no se enviará al recuperar la conexión." : "Hermes puede cometer errores. Verifica información importante."}</p>
    </div>
  );
}

export function ChatView() {
  const sessionId = useAppStore((state) => state.selectedSessionId);
  const profileId = useAppStore((state) => state.selectedProfileId);
  const streamingMessageId = useAppStore((state) => state.streamingBySession[sessionId]);
  const messages = useAppStore((state) => state.messages);
  const approvals = useAppStore((state) => state.approvalsBySession[sessionId] ?? emptyApprovals);
  const clarifications = useAppStore((state) => state.clarificationsBySession[sessionId] ?? emptyClarifications);
  const authState = useAppStore((state) => state.authState);
  const demoMode = useAppStore((state) => state.demoMode);
  const profiles = useAppStore((state) => state.profiles);
  const sessions = useAppStore((state) => state.sessions);
  const profile = profiles.find((item) => item.id === profileId) ?? profiles[0];
  // An empty selection is meaningful: the active profile has no conversation
  // in the selected workspace. Never leak another profile's first session into
  // that state, even as a visual fallback.
  const session = sessions.find((item) => item.id === sessionId);
  const visibleMessages = useMemo(() => messages.filter((message) => message.sessionId === sessionId), [messages, sessionId]);
  const offline = authState === "offline";
  const canMutate = demoMode || (authState === "authenticated" && profile?.mutable === true);
  const canPrompt = canMutate && Boolean(profile?.capabilities?.prompts);
  const canInterrupt = canMutate && Boolean(profile?.capabilities?.interrupt);
  const canMutateInteractions = authState === "authenticated" && profile?.mutable === true;
  const canApprove = canMutateInteractions
    && profile?.capabilities?.approvals === true
    && profile.capabilitySet?.methods.includes("approval.respond") === true;
  const canClarify = canMutateInteractions
    && profile?.capabilities?.clarifications === true
    && profile.capabilitySet?.methods.includes("clarify.respond") === true;
  const waitingForResponse = approvals.length > 0 || clarifications.length > 0;
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const viewport = scrollRef.current;
    if (viewport) viewport.scrollTo({ top: viewport.scrollHeight, behavior: streamingMessageId ? "auto" : "smooth" });
  }, [approvals, clarifications, messages, streamingMessageId]);

  return (
    <section className="conversation" aria-labelledby="conversation-title">
      <div className="conversation__title"><div><span className="eyebrow">Conversación</span><h1 id="conversation-title">{session?.title ?? "Nueva conversación"}</h1></div>{session ? <Badge>{session.storedSessionId}</Badge> : null}</div>
      <div className="message-scroll" ref={scrollRef}>
        <div className="date-divider"><span>28 de agosto de 2026</span></div>
        <div className="message-list">
          {visibleMessages.length ? visibleMessages.map((message) => <Message key={message.id} message={message} agentName={profile?.displayName ?? "Agente"} />) : <div className="empty-chat"><BrandMark size="lg" label="Agent Control" /><h2>{session ? `Inicia una conversación con ${profile?.displayName ?? "tu agente"}` : profile?.mutable ? `Crea un chat con ${profile.displayName}` : `${profile?.displayName ?? "Este agente"} está en modo solo lectura`}</h2><p>{session ? "El contexto de esta sesión permanecerá aislado del resto de agentes." : profile?.mutable ? "Usa “Nuevo chat” para iniciar una conversación dentro de este workspace." : "La protección actual no permite crear conversaciones ni enviar mensajes. Selecciona el entorno de pruebas para escribir."}</p></div>}
          <InteractionCards approvals={approvals} clarifications={clarifications} offline={offline} canApprove={canApprove} canClarify={canClarify} />
          {streamingMessageId ? waitingForResponse
            ? <p className="typing-state typing-state--waiting" role="status"><WarningCircle /><span>{profile?.displayName ?? "Hermes"} espera tu respuesta</span></p>
            : <p className="typing-state" role="status"><span>{profile?.displayName ?? "Hermes"} está escribiendo</span><i /><i /><i /></p>
            : null}
        </div>
      </div>
      {session && (canPrompt || offline) ? <Composer agentName={profile?.displayName ?? "Hermes"} sessionId={sessionId} canInterrupt={canInterrupt} offline={offline} /> : session ? <div className="composer-unavailable"><ShieldNotice /> {profile?.mutable ? "Este perfil no anunció envío de mensajes." : "Este perfil está protegido en modo solo lectura."}</div> : profile && !profile.mutable ? <div className="composer-unavailable"><ShieldNotice /> Para escribir, selecciona el entorno de pruebas.</div> : null}
    </section>
  );
}

function ShieldNotice() {
  return <WarningCircle aria-hidden="true" />;
}
