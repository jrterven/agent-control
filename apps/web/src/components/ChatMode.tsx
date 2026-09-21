import { BookOpen, Brain, Camera, EyeSlash, PaperPlaneTilt, Paperclip, Waveform, X } from "@phosphor-icons/react";
import { useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { IconButton } from "@hermes-control/ui";
import type { ChatMode, Profile } from "../types";
import { useAppStore } from "../store/appStore";
import "./ChatMode.css";

export const CHAT_MODES: ChatMode[] = ["memory_read_write", "memory_read_only", "temporary"];
const icons = { memory_read_write: Brain, memory_read_only: BookOpen, temporary: EyeSlash };

export function supportedChatModes(profile?: Profile): ChatMode[] {
  return CHAT_MODES.filter((mode) => mode === "memory_read_write" || profile?.capabilitySet?.features.includes(`session.mode.${mode}`));
}

export function ChatModeIndicator({ mode = "memory_read_write" }: { mode?: ChatMode }) {
  const { t } = useTranslation();
  const id = useId();
  const [open, setOpen] = useState(false);
  const Icon = icons[mode];
  return <span className="chat-mode-indicator" onMouseEnter={() => setOpen(true)} onMouseLeave={() => setOpen(false)}>
    <button type="button" aria-label={t(`chatModes.${mode}`)} aria-describedby={open ? id : undefined} aria-expanded={open} onClick={() => setOpen(true)} onFocus={() => setOpen(true)} onBlur={() => setOpen(false)} onKeyDown={(event) => { if (event.key === "Escape") setOpen(false); }}><Icon size={22} aria-hidden="true" /></button>
    {open ? <span id={id} role="tooltip" className="chat-mode-tooltip"><strong>{t(`chatModes.${mode}`)}</strong><span>{t(`chatModes.descriptions.${mode}`)}</span></span> : null}
  </span>;
}

export type ChatStart = { mode: ChatMode; text: string; files: File[]; action?: "voice" | "camera" };

export function NewChatSetup({ profile, disabled, voiceAvailable, onStart }: { profile: Profile; disabled: boolean; voiceAvailable: boolean; onStart: (start: ChatStart) => Promise<void> }) {
  const { t } = useTranslation();
  const group = useId();
  const [mode, setMode] = useState<ChatMode>("memory_read_write");
  const [text, setText] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const sending = useRef(false);
  const input = useRef<HTMLInputElement>(null);
  const demoMode = useAppStore((state) => state.demoMode);
  const supported = demoMode ? CHAT_MODES : supportedChatModes(profile);
  const start = async (action?: ChatStart["action"]) => {
    if (sending.current || disabled || !supported.includes(mode) || (!action && !text.trim() && !files.length)) return;
    sending.current = true; setBusy(true); setError("");
    try { await onStart({ mode, text: text.trim(), files, action }); }
    catch { setError(t("chatModes.createError")); }
    finally { sending.current = false; setBusy(false); }
  };
  return <div className="new-chat-setup">
    <fieldset className="chat-mode-picker" disabled={busy || disabled}>
      <legend>{t("chatModes.label")}</legend>
      {CHAT_MODES.map((value) => {
        const Icon = icons[value]; const available = supported.includes(value);
        return <label key={value} className={`chat-mode-option${value === mode ? " is-selected" : ""}${available ? "" : " is-unavailable"}`}>
          <input type="radio" name={group} value={value} checked={mode === value} disabled={!available} onChange={() => setMode(value)} aria-describedby={`${group}-${value}`} />
          <Icon size={24} aria-hidden="true" /><strong>{t(`chatModes.${value}`)}</strong>
          <span id={`${group}-${value}`}>{t(`chatModes.descriptions.${value}`)}{!available ? <small>{t("chatModes.unavailable")}</small> : null}</span>
        </label>;
      })}
    </fieldset>
    <form className="composer-wrap new-chat-composer" onSubmit={(event) => { event.preventDefault(); void start(); }}>
      <div className="composer">
        <textarea aria-label={t("chat.messagePlaceholder", { agent: profile.displayName })} placeholder={t("chat.messagePlaceholder", { agent: profile.displayName })} value={text} disabled={disabled || busy} rows={3} onChange={(event) => setText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void start(); } }} />
        {files.length ? <div className="composer-attachments">{files.map((file, index) => <span className="composer-attachment" key={`${file.name}-${index}`}><span>{file.name}</span><IconButton label={t("chat.attachments.remove", { name: file.name })} icon={<X />} disabled={busy} onClick={() => setFiles((current) => current.filter((_, i) => i !== index))} /></span>)}</div> : null}
        <div className="composer__actions">
          <input ref={input} type="file" multiple hidden onChange={(event) => {
            const next = [...files, ...Array.from(event.target.files ?? [])];
            event.target.value = "";
            if (next.length > 5 || next.some((file) => !file.size || file.size > 8 * 1024 * 1024) || next.reduce((sum, file) => sum + file.size, 0) > 12 * 1024 * 1024) { setError(t("chat.attachments.errors.tooMuchTotal")); return; }
            setError(""); setFiles(next);
          }} />
          <IconButton label={t("chatModes.files")} icon={<Paperclip size={21} />} disabled={disabled || busy} onClick={() => input.current?.click()} />
          <IconButton label={t("chatModes.camera")} icon={<Camera size={21} />} disabled={disabled || busy || !!text.trim() || !!files.length} onClick={() => void start("camera")} />
          {voiceAvailable ? <IconButton label={t("liveVoice.start")} icon={<Waveform size={21} />} disabled={disabled || busy || !!text.trim() || !!files.length} onClick={() => void start("voice")} /> : null}
          <IconButton className="send-button" label={t("chat.sendMessage")} icon={<PaperPlaneTilt size={22} weight="fill" />} disabled={disabled || busy || (!text.trim() && !files.length)} onClick={() => void start()} />
        </div>
      </div>
      {error ? <p role="alert" className="composer-attachment-error">{error}</p> : null}
    </form>
  </div>;
}
