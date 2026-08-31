import { CaretDown, Check, Checks, Lightning, Microphone, PaperPlaneTilt, Pause, Play, Plus, Question, ShieldWarning, SpeakerHigh, Stop, WarningCircle } from "@phosphor-icons/react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import ReactMarkdown from "react-markdown";
import rehypeSanitize from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { Badge, Button, IconButton } from "@hermes-control/ui";
import { createChatForCurrentContext, respondToApproval, respondToClarification, stopPrompt, submitPrompt, useSessionDraft } from "../hooks";
import { api } from "../lib/api";
import { useOverlayDialog } from "../lib/useOverlayDialog";
import { useAppStore } from "../store/appStore";
import { useScribeDictation } from "../hooks/useScribeDictation";
import { useSpeechPlayback, type LiveSpeechStatus, type SpeechPlaybackStatus } from "../hooks/useSpeechPlayback";
import { usePwaUpdateStore } from "../lib/pwaUpdate";
import type { AgentActivityItem, ApprovalRequest, ChatMessage, ClarificationQuestion, ClarificationRequest, MessageMedia, Profile } from "../types";
import { ProfileAvatar } from "./ProfileAvatar";

const emptyApprovals: ApprovalRequest[] = [];
const emptyClarifications: ClarificationRequest[] = [];
const automationInstructionMaxLines = 10;
const interactionErrorKeys: Record<string, "deliveryUnknown" | "noLongerPending" | "generic"> = {
  "El resultado no está confirmado. Los controles permanecerán bloqueados hasta que Hermes reconcilie la solicitud.": "deliveryUnknown",
  "Hermes no confirmó que la solicitud siga pendiente.": "noLongerPending",
  "No se pudo confirmar la respuesta. Revisa la conexión e inténtalo de nuevo.": "generic",
};

function localizedInteractionError(error: string, namespace: "approvals" | "clarifications", t: TFunction) {
  const key = interactionErrorKeys[error];
  return key ? t(`${namespace}.errors.${key}`) : error;
}
function DeliveryIcon({ delivery }: { delivery?: ChatMessage["delivery"] }) {
  const { t } = useTranslation();
  if (delivery === "ambiguous" || delivery === "failed") return <WarningCircle aria-label={t("chat.delivery.unconfirmed")} />;
  if (delivery === "sent") return <Checks aria-label={t("chat.delivery.delivered")} />;
  return <Check aria-label={t("chat.delivery.sending")} />;
}

function AgentActivityDisclosure({ agentName, message }: { agentName: string; message?: ChatMessage }) {
  const { t } = useTranslation();
  const panelId = useId();
  const logRef = useRef<HTMLDivElement>(null);
  const followLatestRef = useRef(true);
  const [expanded, setExpanded] = useState(false);
  const activity = useMemo<AgentActivityItem[]>(() => {
    const entries: AgentActivityItem[] = [{
      id: `${message?.id ?? "active"}:analysis`,
      kind: "analysis",
      status: "running",
      label: t("chat.activity.analyzing"),
    }, ...(message?.activity ?? [])];
    if (message?.content.trim()) {
      entries.push({
        id: `${message.id}:composing`,
        kind: "composing",
        status: "running",
        label: t("chat.activity.composing"),
      });
    }
    return entries;
  }, [message?.activity, message?.content, message?.id, t]);
  const activityKey = activity.map((item) => `${item.id}:${item.status}:${item.summary ?? ""}`).join("|");

  useEffect(() => {
    setExpanded(false);
    followLatestRef.current = true;
  }, [message?.id]);

  useEffect(() => {
    const log = logRef.current;
    if (!expanded || !log || !followLatestRef.current) return;
    log.scrollTop = log.scrollHeight;
  }, [activityKey, expanded]);

  const toggle = () => {
    if (!expanded) followLatestRef.current = true;
    setExpanded((current) => !current);
  };

  return (
    <div className="agent-activity">
      <span className="sr-only" role="status">{t("chat.typing", { agent: agentName })}</span>
      <button
        type="button"
        className="agent-activity__toggle"
        aria-expanded={expanded}
        aria-controls={panelId}
        aria-label={t(expanded ? "chat.activity.hide" : "chat.activity.show", { agent: agentName })}
        onClick={toggle}
      >
        <span>{t("chat.typing", { agent: agentName })}</span>
        <span className="agent-activity__dots" aria-hidden="true"><i /><i /><i /></span>
        <CaretDown className="agent-activity__caret" size={15} weight="bold" aria-hidden="true" />
      </button>
      {expanded ? (
        <div
          id={panelId}
          ref={logRef}
          className="agent-activity__log"
          role="log"
          aria-live="off"
          aria-label={t("chat.activity.label", { agent: agentName })}
          tabIndex={0}
          onScroll={(event) => {
            const log = event.currentTarget;
            followLatestRef.current = log.scrollHeight - log.scrollTop - log.clientHeight <= 16;
          }}
        >
          {activity.map((item) => (
            <div className={`agent-activity__item is-${item.status}`} key={item.id}>
              <span className="agent-activity__marker" aria-hidden="true" />
              <span>
                <strong>{item.label ?? t(item.kind === "delegation" ? "chat.activity.delegation" : "chat.activity.tool")}</strong>
                {item.summary && item.summary !== item.label ? <small>{item.summary}</small> : null}
              </span>
              <span className="sr-only">{t(`chat.toolStatus.${item.status}`)}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function ApprovalCard({ request, offline, canRespond, profileOverride }: { request: ApprovalRequest; offline: boolean; canRespond: boolean; profileOverride?: Profile }) {
  const { t } = useTranslation();
  const headingId = useId();
  const busy = request.state === "submitting";
  const ambiguous = request.state === "ambiguous";
  const blocked = busy || ambiguous || offline || !canRespond;
  return (
    <section className="interaction-card interaction-card--approval" aria-labelledby={headingId}>
      <header>
        <span className="interaction-card__icon"><ShieldWarning size={22} weight="fill" /></span>
        <span>
          <span className="eyebrow">{t("approvals.actionPaused")}</span>
          <h2 id={headingId}>{t("approvals.required")}</h2>
        </span>
        <Badge tone="warning">{t(ambiguous ? "approvals.unconfirmed" : "approvals.waiting")}</Badge>
      </header>
      {request.description ? <p className="interaction-card__description">{request.description}</p> : null}
      {request.command ? <pre className="interaction-card__command"><code>{request.command}</code></pre> : null}
      {request.smartDenied ? <p className="interaction-card__notice">{t("approvals.smartDenied")}</p> : null}
      <div className="interaction-card__actions" aria-label={t("approvals.options")}>
        {request.choices.map((choice) => (
          <Button
            key={choice}
            variant={choice === "deny" ? "danger" : choice === "once" ? "primary" : "secondary"}
            disabled={blocked}
            aria-busy={busy || undefined}
            onClick={() => void respondToApproval(request.sessionId, request.requestId, choice, profileOverride).catch(() => undefined)}
          >
            {busy ? t("approvals.confirming") : t(`approvals.choices.${choice}`)}
          </Button>
        ))}
      </div>
      {offline ? <p className="interaction-card__notice">{t("approvals.reconnect")}</p> : null}
      {!offline && !canRespond ? <p className="interaction-card__notice">{t("approvals.unavailable")}</p> : null}
      {request.error ? <p className="interaction-card__error" role="alert">{localizedInteractionError(request.error, "approvals", t)}</p> : null}
    </section>
  );
}

function readableAnswer(answer: string, omittedLabel: string) {
  try {
    const parsed = JSON.parse(answer) as unknown;
    if (Array.isArray(parsed) && parsed.every((item) => typeof item === "string")) return parsed.join(", ");
  } catch {
    // Plain text is the canonical single-answer representation.
  }
  return answer || omittedLabel;
}

function ClarificationQuestionForm({
  request,
  question,
  index,
  offline,
  canRespond,
  profileOverride,
}: {
  request: ClarificationRequest;
  question: ClarificationQuestion;
  index: number;
  offline: boolean;
  canRespond: boolean;
  profileOverride?: Profile;
}) {
  const { t } = useTranslation();
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
      profileOverride,
    ).catch(() => undefined);
  };

  return (
    <div className={`clarification-question${answered ? " is-answered" : ""}`} aria-labelledby={headingId}>
      <div className="clarification-question__heading">
        <span>{request.batch ? index + 1 : <Question size={18} weight="bold" />}</span>
        <h3 id={headingId}>{question.question}</h3>
        {answered ? <Badge tone="positive">{t("clarifications.answered")}</Badge> : null}
      </div>
      {answered ? <p className="clarification-question__answer"><Check weight="bold" /> {readableAnswer(request.answers[answerKey], t("clarifications.omitted"))}</p> : (
        <>
          {question.choices.length ? (
            <div
              className="clarification-choices"
              role={question.multiSelect ? "group" : "radiogroup"}
              aria-label={t(question.multiSelect ? "clarifications.selectMultiple" : "clarifications.selectOne")}
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
                <span>{t("clarifications.otherAnswer")}</span>
              </button>
            </div>
          ) : (
            <textarea
              className="clarification-answer"
              rows={3}
              value={text}
              disabled={blocked}
              aria-label={t("clarifications.answerAria", { question: question.question })}
              placeholder={t("clarifications.writeAnswer")}
              onChange={(event) => setText(event.target.value)}
            />
          )}
          {question.choices.length && customAnswer ? (
            <textarea
              className="clarification-answer"
              rows={3}
              value={text}
              disabled={blocked}
              aria-label={t("clarifications.otherAnswerAria", { question: question.question })}
              placeholder={t(question.multiSelect ? "clarifications.addOtherOption" : "clarifications.writeOtherAnswer")}
              onChange={(event) => setText(event.target.value)}
            />
          ) : null}
          <div className="clarification-question__actions">
            <Button variant="primary" disabled={blocked || !hasAnswer} aria-busy={busy || undefined} onClick={() => submit(answer)}>
              {busy ? t("clarifications.sending") : request.batch ? t("clarifications.confirmAnswer") : t("clarifications.respond")}
            </Button>
          </div>
        </>
      )}
    </div>
  );
}

function ClarificationCard({ request, offline, canRespond, profileOverride }: { request: ClarificationRequest; offline: boolean; canRespond: boolean; profileOverride?: Profile }) {
  const { t } = useTranslation();
  const headingId = useId();
  return (
    <section className="interaction-card interaction-card--clarification" aria-labelledby={headingId}>
      <header>
        <span className="interaction-card__icon"><Question size={22} weight="fill" /></span>
        <span>
          <span className="eyebrow">{t("clarifications.needsContext")}</span>
          <h2 id={headingId}>{request.batch ? t("clarifications.questions", { count: request.questions.length }) : t("clarifications.oneQuestion")}</h2>
        </span>
        <Badge tone={request.state === "ambiguous" ? "warning" : "info"}>{t(request.state === "ambiguous" ? "clarifications.unconfirmed" : "clarifications.waiting")}</Badge>
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
            profileOverride={profileOverride}
          />
        ))}
      </div>
      {offline ? <p className="interaction-card__notice">{t("clarifications.reconnect")}</p> : null}
      {!offline && !canRespond ? <p className="interaction-card__notice">{t("clarifications.unavailable")}</p> : null}
      {request.error ? <p className="interaction-card__error" role="alert">{localizedInteractionError(request.error, "clarifications", t)}</p> : null}
    </section>
  );
}

export function InteractionCards({ approvals, clarifications, offline, canApprove, canClarify, profileOverride }: {
  approvals: ApprovalRequest[];
  clarifications: ClarificationRequest[];
  offline: boolean;
  canApprove: boolean;
  canClarify: boolean;
  profileOverride?: Profile;
}) {
  const { t } = useTranslation();
  if (!approvals.length && !clarifications.length) return null;
  return (
    <div className="interaction-stack" aria-live="polite" aria-label={t("approvals.waitingAria")}>
      {approvals.map((request) => <ApprovalCard key={request.requestId} request={request} offline={offline} canRespond={canApprove} profileOverride={profileOverride} />)}
      {clarifications.map((request) => <ClarificationCard key={request.requestId} request={request} offline={offline} canRespond={canClarify} profileOverride={profileOverride} />)}
    </div>
  );
}

type MessageSpeech = {
  available: boolean;
  activeMessageId?: string;
  status: SpeechPlaybackStatus;
  rate: number;
  error: boolean;
  speak: (message: ChatMessage) => Promise<void>;
  togglePause: () => void;
  stop: () => void;
  setRate: (rate: number) => void;
};

function SpeechPlayer({ speech }: { speech: MessageSpeech }) {
  const { t } = useTranslation();
  return <div className="speech-player" role="group" aria-label={t("speech.player")}>
    <IconButton
      label={t(speech.status === "playing" ? "speech.pause" : "speech.play")}
      disabled={speech.status === "loading"}
      icon={speech.status === "playing" ? <Pause weight="fill" /> : <Play weight="fill" />}
      onClick={speech.togglePause}
    />
    <IconButton label={t("speech.stop")} icon={<Stop weight="fill" />} onClick={speech.stop} />
    <label><span>{t("speech.speed")}</span><select aria-label={t("speech.speed")} value={speech.rate} onChange={(event) => speech.setRate(Number(event.target.value))}>
      {[0.75, 1, 1.25, 1.5, 2].map((rate) => <option key={rate} value={rate}>{rate}×</option>)}
    </select></label>
    <span className={`speech-player__status speech-player__status--${speech.status}`} role="status">{t(`speech.${speech.error ? "error" : speech.status}`)}</span>
  </div>;
}

function AutomationInstructionMessage({ message }: { message: ChatMessage }) {
  const { t } = useTranslation();
  const contentId = useId();
  const [expanded, setExpanded] = useState(false);

  useEffect(() => setExpanded(false), [message.id, message.sessionId]);

  return (
    <div className={`user-bubble automation-instruction${expanded ? " is-expanded" : ""}`}>
      <span className="message-time">{message.createdAt} <DeliveryIcon delivery={message.delivery} /></span>
      <button
        type="button"
        className="automation-instruction__toggle"
        aria-expanded={expanded}
        aria-controls={contentId}
        aria-label={t(expanded ? "chat.automationInstruction.hide" : "chat.automationInstruction.show")}
        onClick={() => setExpanded((current) => !current)}
      >
        <span className="automation-instruction__identity">
          <Lightning size={20} weight="fill" aria-hidden="true" />
          <span>
            <strong>{t("chat.automationInstruction.title")}</strong>
            <small>{t(expanded ? "chat.automationInstruction.hide" : "chat.automationInstruction.show")}</small>
          </span>
        </span>
        <CaretDown className="automation-instruction__caret" size={17} weight="bold" aria-hidden="true" />
      </button>
      {expanded ? (
        <div className="automation-instruction__frame">
          <div
            id={contentId}
            className="automation-instruction__content"
            role="region"
            aria-label={t("chat.automationInstruction.contentLabel")}
            data-max-lines={automationInstructionMaxLines}
            style={{ "--automation-instruction-lines": automationInstructionMaxLines } as CSSProperties}
            tabIndex={0}
          >
            <p>{message.content}</p>
          </div>
        </div>
      ) : null}
    </div>
  );
}

function Message({ message, profile, agentName, speech, automationInstruction = false }: { message: ChatMessage; profile?: Profile; agentName: string; speech: MessageSpeech; automationInstruction?: boolean }) {
  const { t } = useTranslation();
  if (message.role === "user") {
    return (
      <article className="message message--user" aria-label={t(automationInstruction ? "chat.automationInstruction.title" : "chat.userMessage")}>
        {automationInstruction ? <AutomationInstructionMessage message={message} /> : <div className="user-bubble"><span className="message-time">{message.createdAt} <DeliveryIcon delivery={message.delivery} /></span><p>{message.content}</p></div>}
        {message.delivery === "ambiguous" ? <p className="delivery-warning"><WarningCircle /> {t("chat.deliveryWarning")}</p> : null}
      </article>
    );
  }
  const evidence = !message.content.trim()
    ? [
        ...(message.tools ?? []).map((tool) => ({
          id: `tool:${tool.id}`,
          label: tool.label,
          summary: tool.summary || t(`chat.toolStatus.${tool.status}`),
        })),
        ...(message.activity ?? []).map((item) => ({
          id: `activity:${item.id}`,
          label: item.label || t(item.kind === "delegation" ? "chat.activity.delegation" : "chat.activity.tool"),
          summary: item.summary || t(`chat.toolStatus.${item.status}`),
        })),
      ]
    : [];
  return (
    <article className="message message--assistant" aria-label={t("chat.assistantResponse", { agent: agentName })}>
      <div className="assistant-avatar"><ProfileAvatar profile={profile} /></div>
      <div className="assistant-content">
        <header><strong>{agentName}</strong><time>{message.createdAt}</time>{message.streaming ? <Badge tone="info">{t("chat.streaming")}</Badge> : null}</header>
        <div className="markdown-body">
          {message.content.trim() ? (
            <ReactMarkdown remarkPlugins={[remarkGfm]} rehypePlugins={[rehypeSanitize]}>{message.content}</ReactMarkdown>
          ) : evidence.length ? (
            <div className="message-evidence" role="note" aria-label={t("chat.activity.label", { agent: agentName })}>
              <strong>{message.tools?.length
                ? t("chat.toolsCount", { count: message.tools.length })
                : t("chat.activity.label", { agent: agentName })}</strong>
              <ul>
                {evidence.map((item) => <li key={item.id}><span>{item.label}</span><small>{item.summary}</small></li>)}
              </ul>
            </div>
          ) : " "}
          {message.streaming ? <span className="stream-caret" aria-hidden="true" /> : null}
        </div>
        {message.media?.length ? (
          <div className="message-media">
            {message.media.map((media, index) => (
              <VoiceNote
                key={media.id}
                media={media}
                sessionId={message.sessionId}
                number={message.media && message.media.length > 1 ? index + 1 : undefined}
              />
            ))}
          </div>
        ) : null}
        {speech.available && !message.streaming && message.content.trim() ? <div className="message-speech">
          <IconButton className="message-speech__button" label={t("speech.readResponse")} selected={speech.activeMessageId === message.id} icon={<SpeakerHigh weight="fill" />} onClick={() => void speech.speak(message)} />
          {speech.activeMessageId === message.id ? <SpeechPlayer speech={speech} /> : null}
        </div> : null}
      </div>
    </article>
  );
}

function VoiceNote({
  media,
  sessionId,
  number,
}: {
  media: MessageMedia;
  sessionId: string;
  number?: number;
}) {
  const { t } = useTranslation();
  const label = number
    ? t("chat.voiceNoteNumber", { number })
    : t("chat.voiceNote");
  return (
    <section className="voice-note" aria-label={label}>
      <div className="voice-note__label"><SpeakerHigh size={18} weight="fill" /> <span>{label}</span></div>
      <audio controls preload="metadata" aria-label={t("chat.playVoiceNote")}>
        <source src={api.sessionMediaUrl(sessionId, media.id)} type={media.mediaType} />
        {t("chat.audioUnsupported")}
      </audio>
    </section>
  );
}

export function insertTranscriptAtSelection(value: string, transcript: string, selectionStart: number, selectionEnd: number) {
  const start = Math.max(0, Math.min(selectionStart, value.length));
  const end = Math.max(start, Math.min(selectionEnd, value.length));
  const text = transcript.trim();
  if (!text) return { value, caret: start };
  const before = value.slice(0, start);
  const after = value.slice(end);
  const prefix = before && !/\s$/.test(before) && !/^[,.;:!?)]/.test(text) ? " " : "";
  const suffix = after && !/^\s/.test(after) && !/^[,.;:!?)]/.test(after) ? " " : "";
  const insertion = `${prefix}${text}${suffix}`;
  return { value: `${before}${insertion}${after}`, caret: before.length + insertion.length };
}

function Composer({ agentName, sessionId, canInterrupt, offline = false, speechAvailable, liveSpeechEnabled, liveSpeechStatus, onLiveSpeechChange }: { agentName: string; sessionId: string; canInterrupt: boolean; offline?: boolean; speechAvailable: boolean; liveSpeechEnabled: boolean; liveSpeechStatus: LiveSpeechStatus; onLiveSpeechChange: (enabled: boolean) => void }) {
  const { t } = useTranslation();
  const [value, setValue] = useState("");
  const [dictationConsent, setDictationConsent] = useState(false);
  const [consentOpen, setConsentOpen] = useState(false);
  const streamingMessageId = useAppStore((state) => state.streamingBySession[sessionId]);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const authState = useAppStore((state) => state.authState);
  const dictationConfigured = useAppStore((state) => state.features?.dictation.available === true);
  const draft = useSessionDraft(sessionId);
  const setUpdateBlocker = usePwaUpdateStore((state) => state.setBlocker);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const dictationSelectionRef = useRef<{ start: number; end: number } | null>(null);
  const insertCommitted = (transcript: string) => {
    setValue((current) => {
      const textarea = textareaRef.current;
      const selection = dictationSelectionRef.current ?? {
        start: textarea?.selectionStart ?? current.length,
        end: textarea?.selectionEnd ?? current.length,
      };
      const result = insertTranscriptAtSelection(
        current,
        transcript,
        selection.start,
        selection.end,
      );
      // Keep subsequent confirmed VAD segments flowing from the same point.
      // Provisional hypotheses never update this anchor or the saved draft.
      dictationSelectionRef.current = { start: result.caret, end: result.caret };
      draft.save(result.value);
      window.requestAnimationFrame(() => {
        const activeTextarea = textareaRef.current;
        if (!activeTextarea) return;
        activeTextarea.focus();
        activeTextarea.setSelectionRange(result.caret, result.caret);
      });
      return result.value;
    });
  };
  const dictation = useScribeDictation({
    enabled: dictationConfigured && authState === "authenticated" && !offline && !streamingMessageId,
    sessionId,
    csrfToken,
    onCommitted: insertCommitted,
  });
  const consentDialog = useOverlayDialog<HTMLDivElement>({
    open: consentOpen,
    onClose: () => setConsentOpen(false),
    mediaQuery: "(min-width: 0px)",
  });

  const captureDictationSelection = () => {
    const textarea = textareaRef.current;
    dictationSelectionRef.current = {
      start: textarea?.selectionStart ?? value.length,
      end: textarea?.selectionEnd ?? value.length,
    };
  };

  const beginDictation = () => {
    captureDictationSelection();
    if (!dictationConsent) {
      setConsentOpen(true);
      return;
    }
    void dictation.start();
  };

  const acceptDictation = () => {
    setDictationConsent(true);
    setConsentOpen(false);
    // This remains inside the explicit consent button gesture. The hook asks
    // for a fresh token and microphone only now, never when opening the modal.
    void dictation.start();
  };

  useEffect(() => {
    if (authState !== "authenticated") {
      setConsentOpen(false);
      setDictationConsent(false);
    } else if (!dictation.available) {
      setConsentOpen(false);
    }
  }, [authState, dictation.available]);

  useEffect(() => {
    setUpdateBlocker("dictation", dictation.active);
    return () => setUpdateBlocker("dictation", false);
  }, [dictation.active, setUpdateBlocker]);

  useEffect(() => {
    setUpdateBlocker("draft", Boolean(value.trim()));
    return () => setUpdateBlocker("draft", false);
  }, [setUpdateBlocker, value]);

  useEffect(() => {
    let active = true;
    dictationSelectionRef.current = null;
    setValue("");
    void draft.load().then((loaded) => { if (active) setValue(loaded); });
    return () => { active = false; };
  }, [sessionId]); // eslint-disable-line react-hooks/exhaustive-deps

  const previewSelection = dictationSelectionRef.current ?? { start: value.length, end: value.length };
  const displayedValue = dictation.partial
    ? insertTranscriptAtSelection(
      value,
      dictation.partial,
      previewSelection.start,
      previewSelection.end,
    ).value
    : value;

  useEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    const styles = window.getComputedStyle(textarea);
    const fontSize = Number.parseFloat(styles.fontSize) || 15;
    const rawLineHeight = Number.parseFloat(styles.lineHeight);
    const lineHeight = Number.isFinite(rawLineHeight)
      ? rawLineHeight <= 4 ? rawLineHeight * fontSize : rawLineHeight
      : fontSize * 1.5;
    const verticalPadding = (Number.parseFloat(styles.paddingTop) || 0) + (Number.parseFloat(styles.paddingBottom) || 0);
    const maxHeight = Math.ceil((lineHeight * 6) + verticalPadding);
    textarea.style.height = "auto";
    textarea.style.height = `${Math.min(textarea.scrollHeight, maxHeight)}px`;
    textarea.style.overflowY = textarea.scrollHeight > maxHeight ? "auto" : "hidden";
  }, [displayedValue]);

  const onSubmit = async () => {
    const next = value.trim();
    if (!next || streamingMessageId || offline || dictation.active) return;
    // submitPrompt marks the session as streaming synchronously before its
    // first await. Start it before clearing the draft so an update queued for
    // a safe moment cannot slip into the hand-off between typing and sending.
    const submission = submitPrompt(next);
    setValue("");
    await draft.clear();
    await submission;
  };

  return (
    <div className="composer-wrap">
      {speechAvailable ? <label className="live-speech-toggle">
        <input type="checkbox" checked={liveSpeechEnabled} onChange={(event) => onLiveSpeechChange(event.target.checked)} />
        <span aria-hidden="true" />
        <SpeakerHigh weight="fill" />
        <strong>{t("speech.autoRead")}</strong>
        {liveSpeechStatus !== "idle" ? <small role="status">{t(`speech.live.${liveSpeechStatus}`)}</small> : null}
      </label> : null}
      <div className="composer">
        <textarea
          ref={textareaRef}
          rows={1}
          value={displayedValue}
          readOnly={dictation.active}
          aria-label={t("chat.messagePlaceholder", { agent: agentName })}
          placeholder={t("chat.messagePlaceholder", { agent: agentName })}
          onChange={(event) => { setValue(event.target.value); draft.save(event.target.value); }}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); void onSubmit(); }
          }}
        />
        {dictation.active || dictation.issue ? <div className={`dictation-state${dictation.issue ? " dictation-state--error" : ""}`} role={dictation.issue ? "alert" : "status"} aria-live={dictation.issue ? "assertive" : "polite"}>
          <span>{dictation.issue ? t(`dictation.${dictation.issue}`) : dictation.phase === "connecting" ? t("dictation.connecting") : dictation.phase === "stopping" ? t("dictation.stopping") : t("dictation.listening")}</span>
          {dictation.partial ? <em className="dictation-state__announcement">{t("dictation.provisional", { text: dictation.partial })}</em> : null}
          {!dictation.issue ? <small>{t("dictation.disclosure")}</small> : null}
        </div> : null}
        <div className="composer__actions">
          <span />
          {offline ? <Badge tone="warning">{t("chat.offlineDraft")}</Badge> : streamingMessageId ? (canInterrupt ? <Button variant="danger" size="sm" leadingIcon={<Stop weight="fill" />} onClick={() => void stopPrompt()}>{t("chat.stop")}</Button> : <Badge tone="info">{t("chat.running")}</Badge>) : <>
            {dictation.available ? <IconButton className="dictation-button" selected={dictation.active} label={t(dictation.active ? "dictation.stop" : "dictation.start")} icon={dictation.active ? <Stop size={20} weight="fill" /> : <Microphone size={21} weight="fill" />} onClick={() => { if (dictation.active) dictation.stop(); else beginDictation(); }} /> : null}
            <IconButton className="send-button" label={t("chat.sendMessage")} disabled={!value.trim() || dictation.active} icon={<PaperPlaneTilt size={22} weight="fill" />} onClick={() => void onSubmit()} />
          </>}
        </div>
      </div>
      <p className="composer-note">{t(offline ? "chat.offlineDraftNote" : "chat.disclaimer")}</p>
      {consentOpen ? <div ref={consentDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="dictation-consent-title" aria-describedby="dictation-consent-description">
        <button className="modal-scrim" aria-label={t("dictation.consentClose")} onClick={() => setConsentOpen(false)} />
        <div className="hc-panel form-modal dictation-consent">
          <span className="eyebrow">ElevenLabs · Scribe v2 Realtime</span>
          <h2 id="dictation-consent-title">{t("dictation.consentTitle")}</h2>
          <p id="dictation-consent-description">{t("dictation.consentDescription")}</p>
          <ul className="dictation-consent__points">
            <li><Microphone aria-hidden="true" /><span>{t("dictation.consentAudio")}</span></li>
            <li><WarningCircle aria-hidden="true" /><span>{t("dictation.consentRetention")}</span></li>
            <li><Check aria-hidden="true" /><span>{t("dictation.consentDraft")}</span></li>
          </ul>
          <div className="dictation-consent__actions">
            <Button type="button" variant="ghost" onClick={() => setConsentOpen(false)}>{t("dictation.consentCancel")}</Button>
            <Button type="button" variant="primary" leadingIcon={<Microphone />} onClick={acceptDictation}>{t("dictation.consentAccept")}</Button>
          </div>
        </div>
      </div> : null}
    </div>
  );
}

export function ChatView() {
  const { t } = useTranslation();
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
  const visibleMessages = useMemo(() => messages.filter((message) => (
    message.sessionId === sessionId
    && (
      message.role !== "assistant"
      || message.streaming === true
      || Boolean(message.content.trim())
      || Boolean(message.media?.length)
      || Boolean(message.tools?.length)
      || Boolean(message.activity?.length)
    )
  )), [messages, sessionId]);
  const firstUserMessageId = useMemo(() => visibleMessages.find((message) => message.role === "user")?.id, [visibleMessages]);
  const globalSpeechAvailable = useAppStore((state) => state.features?.speech?.available === true);
  // Profiles from pre-voice cached bootstraps do not include ``speech``;
  // retain the global behavior until a fresh profile-aware bootstrap arrives.
  const speechAvailable = profile?.speech?.available ?? globalSpeechAvailable;
  const canUseSpeech = speechAvailable && authState === "authenticated";
  const csrfToken = useAppStore((state) => state.csrfToken);
  const streamingMessage = visibleMessages.find((message) => message.id === streamingMessageId);
  const speech = useSpeechPlayback({
    available: canUseSpeech,
    sessionId,
    historySessionId: profile?.speech ? sessionId : undefined,
    csrfToken,
    streamingMessage,
  });
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
  const [creatingSession, setCreatingSession] = useState(false);
  const canCreateSession = !session && (
    demoMode
    || (
      authState === "authenticated"
      && profile?.mutable === true
      && profile.capabilities?.sessions === true
    )
  );

  const createChat = async () => {
    if (!canCreateSession || creatingSession) return;
    setCreatingSession(true);
    try {
      await createChatForCurrentContext();
    } finally {
      setCreatingSession(false);
    }
  };

  useEffect(() => {
    const viewport = scrollRef.current;
    if (viewport) viewport.scrollTo({ top: viewport.scrollHeight, behavior: streamingMessageId ? "auto" : "smooth" });
  }, [approvals, clarifications, messages, streamingMessageId]);

  return (
    <section className="conversation" aria-labelledby="conversation-title">
      <div className="conversation__title"><div><span className="eyebrow">{t("chat.conversation")}</span><h1 id="conversation-title">{session?.title ?? t("chat.newConversation")}</h1></div>{session ? <Badge>{session.storedSessionId}</Badge> : null}</div>
      <div className="message-scroll" ref={scrollRef}>
        <div className="date-divider"><span>{t("chat.fixedDate")}</span></div>
        <div className="message-list">
          {visibleMessages.length ? visibleMessages.map((message) => <Message key={message.id} message={message} profile={profile} agentName={profile?.displayName ?? t("chat.agent")} automationInstruction={session?.automationGenerated === true && message.id === firstUserMessageId} speech={{ available: canUseSpeech, activeMessageId: speech.activeMessageId, status: speech.status, rate: speech.rate, error: speech.error, speak: speech.speak, togglePause: speech.togglePause, stop: speech.stop, setRate: speech.setRate }} />) : <div className="empty-chat"><ProfileAvatar profile={profile} size="lg" /><h2>{session ? t("chat.startWithAgent", { agent: profile?.displayName ?? t("chat.yourAgent") }) : profile?.mutable ? t("chat.createWithAgent", { agent: profile.displayName }) : t("chat.readOnlyAgent", { agent: profile?.displayName ?? t("chat.thisAgent") })}</h2><p>{t(session ? "chat.sessionIsolation" : profile?.mutable ? "chat.startInWorkspace" : "chat.readOnlyDescription")}</p>{canCreateSession ? <Button className="empty-chat__action" variant="primary" leadingIcon={<Plus size={19} />} disabled={creatingSession} aria-busy={creatingSession || undefined} onClick={() => void createChat().catch(() => undefined)}>{t(creatingSession ? "chat.creating" : "chat.newChat")}</Button> : null}</div>}
          <InteractionCards approvals={approvals} clarifications={clarifications} offline={offline} canApprove={canApprove} canClarify={canClarify} />
          {streamingMessageId ? waitingForResponse
            ? <p className="typing-state typing-state--waiting" role="status"><WarningCircle /><span>{t("chat.waitingForResponse", { agent: profile?.displayName ?? "Hermes" })}</span></p>
            : <AgentActivityDisclosure agentName={profile?.displayName ?? "Hermes"} message={streamingMessage} />
            : null}
        </div>
      </div>
      {session && (canPrompt || offline) ? <Composer agentName={profile?.displayName ?? "Hermes"} sessionId={sessionId} canInterrupt={canInterrupt} offline={offline} speechAvailable={canUseSpeech} liveSpeechEnabled={speech.liveEnabled} liveSpeechStatus={speech.liveStatus} onLiveSpeechChange={speech.setLiveEnabled} /> : session ? <div className="composer-unavailable"><ShieldNotice /> {t(profile?.mutable ? "chat.promptUnavailable" : "chat.profileReadOnly")}</div> : profile && !profile.mutable ? <div className="composer-unavailable"><ShieldNotice /> {t("chat.chooseTestEnvironment")}</div> : null}
    </section>
  );
}

function ShieldNotice() {
  return <WarningCircle aria-hidden="true" />;
}
