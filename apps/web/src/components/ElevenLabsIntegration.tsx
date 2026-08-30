import { CheckCircle, Key, Microphone, Pause, Play, SpeakerHigh, Trash, WarningCircle } from "@phosphor-icons/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Badge, Button, Field, Panel } from "@hermes-control/ui";
import { api, type ElevenLabsIntegrationView, type ElevenLabsTtsModelId, type ElevenLabsVoice } from "../lib/api";
import { useAppStore } from "../store/appStore";

type Action = "load" | "save" | "test" | "voice" | "delete" | "";
type PreviewState = "idle" | "loading" | "playing" | "paused" | "error";

export function ElevenLabsIntegration() {
  const { t } = useTranslation();
  const csrfToken = useAppStore((state) => state.csrfToken);
  const offline = useAppStore((state) => state.authState === "offline");
  const demoMode = useAppStore((state) => state.demoMode);
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const [view, setView] = useState<ElevenLabsIntegrationView | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [voices, setVoices] = useState<ElevenLabsVoice[]>([]);
  const [voiceId, setVoiceId] = useState("");
  const [ttsModelId, setTtsModelId] = useState<ElevenLabsTtsModelId>("eleven_flash_v2_5");
  const [voicesLoading, setVoicesLoading] = useState(false);
  const [action, setAction] = useState<Action>("");
  const [previewState, setPreviewState] = useState<PreviewState>("idle");
  const [previewVoiceId, setPreviewVoiceId] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const previewAudioRef = useRef<HTMLAudioElement | null>(null);
  const blocked = offline || demoMode || Boolean(action);

  const releasePreview = useCallback(() => {
    const audio = previewAudioRef.current;
    if (!audio) return;
    audio.onplaying = null;
    audio.onwaiting = null;
    audio.onpause = null;
    audio.onended = null;
    audio.onerror = null;
    audio.pause();
    audio.removeAttribute("src");
    audio.load();
    previewAudioRef.current = null;
  }, []);

  const stopPreview = useCallback(() => {
    releasePreview();
    setPreviewState("idle");
    setPreviewVoiceId("");
  }, [releasePreview]);

  useEffect(() => () => releasePreview(), [releasePreview]);

  useEffect(() => {
    setTtsModelId(view?.ttsModelId ?? "eleven_flash_v2_5");
  }, [view?.ttsModelId]);

  useEffect(() => {
    if (previewVoiceId && previewVoiceId !== voiceId) stopPreview();
  }, [previewVoiceId, stopPreview, voiceId]);

  useEffect(() => {
    if (offline || demoMode) return;
    let active = true;
    setAction("load");
    setError("");
    void api.elevenLabsIntegration()
      .then((integration) => { if (active) setView(integration); })
      .catch(() => { if (active) setError(t("integrations.loadError")); })
      .finally(() => { if (active) setAction(""); });
    return () => { active = false; };
  }, [demoMode, offline, t]);

  useEffect(() => {
    if (!view?.configured || offline || demoMode) {
      setVoices([]);
      setVoiceId("");
      return;
    }
    let active = true;
    setVoicesLoading(true);
    void api.elevenLabsVoices()
      .then(({ items }) => {
        if (!active) return;
        setVoices(items);
        setVoiceId(view.voiceId ?? "");
      })
      .catch(() => { if (active) setError(t("integrations.voicesError")); })
      .finally(() => { if (active) setVoicesLoading(false); });
    return () => { active = false; };
  }, [demoMode, offline, t, view?.configured]);

  const refreshFeatures = async () => {
    const bootstrap = await api.bootstrap();
    hydrateBootstrap(bootstrap);
  };

  const save = async () => {
    const submittedKey = apiKey.trim();
    if (!submittedKey || blocked) return;
    stopPreview();
    setAction("save");
    setNotice("");
    setError("");
    // This write-only value leaves component memory immediately. It is never
    // copied into app state, browser storage, logs, or a readable API response.
    setApiKey("");
    try {
      const integration = await api.saveElevenLabsKey(submittedKey, csrfToken);
      setView(integration);
      setNotice(t("integrations.saved"));
      void refreshFeatures().catch(() => undefined);
    } catch {
      setError(t("integrations.saveError"));
    } finally {
      setAction("");
    }
  };

  const test = async () => {
    if (!view?.configured || blocked) return;
    setAction("test");
    setNotice("");
    setError("");
    try {
      await api.testElevenLabsIntegration(csrfToken);
      setNotice(t("integrations.testPassed"));
    } catch {
      setError(t("integrations.testFailed"));
    } finally {
      setAction("");
    }
  };

  const saveVoice = async () => {
    if (!voiceId || blocked) return;
    stopPreview();
    setAction("voice");
    setNotice("");
    setError("");
    try {
      const integration = await api.saveElevenLabsVoice(voiceId, ttsModelId, csrfToken);
      setView(integration);
      setNotice(t("integrations.voiceSaved", {
        voice: integration.voiceName ?? voiceId,
        model: t(`integrations.models.${integration.ttsModelId ?? ttsModelId}`),
      }));
      await refreshFeatures();
    } catch {
      setError(t("integrations.voiceSaveError"));
    } finally {
      setAction("");
    }
  };

  const previewVoice = async () => {
    const selected = voices.find((voice) => voice.id === voiceId);
    if (!selected?.previewAvailable || blocked) return;
    setError("");

    const current = previewAudioRef.current;
    if (current && previewVoiceId === voiceId) {
      if (previewState === "playing" || previewState === "loading") {
        current.pause();
        setPreviewState("paused");
        return;
      }
      if (previewState === "paused") {
        try {
          await current.play();
        } catch {
          setPreviewState("error");
          setError(t("integrations.previewError"));
        }
        return;
      }
    }

    releasePreview();
    const audio = new Audio(api.elevenLabsVoicePreviewUrl(voiceId));
    audio.preload = "none";
    previewAudioRef.current = audio;
    setPreviewVoiceId(voiceId);
    setPreviewState("loading");
    audio.onplaying = () => setPreviewState("playing");
    audio.onwaiting = () => setPreviewState("loading");
    audio.onpause = () => {
      if (!audio.ended) setPreviewState("paused");
    };
    audio.onended = stopPreview;
    audio.onerror = () => {
      releasePreview();
      setPreviewState("error");
      setError(t("integrations.previewError"));
    };
    try {
      await audio.play();
    } catch {
      releasePreview();
      setPreviewState("error");
      setError(t("integrations.previewError"));
    }
  };

  const remove = async () => {
    if (!view?.configured || blocked) return;
    stopPreview();
    setAction("delete");
    setNotice("");
    setError("");
    setConfirmDelete(false);
    try {
      await api.deleteElevenLabsKey(csrfToken);
      setView({ configured: false, provider: "elevenlabs", modelId: "scribe_v2_realtime" });
      setNotice(t("integrations.deleted"));
      void refreshFeatures().catch(() => undefined);
    } catch {
      setError(t("integrations.deleteError"));
    } finally {
      setAction("");
    }
  };

  return (
    <Panel className="settings-section integration-settings" aria-labelledby="elevenlabs-integration-title">
      <header>
        <Microphone />
        <div>
          <strong id="elevenlabs-integration-title">{t("integrations.title")}</strong>
          <p>{t("integrations.description")}</p>
        </div>
        <Badge tone={view?.configured ? "positive" : "neutral"}>
          {action === "load" ? t("integrations.loading") : view?.configured ? t("integrations.configured") : t("integrations.notConfigured")}
        </Badge>
      </header>
      <div className="integration-settings__provider">
        <span><Key weight="duotone" /></span>
        <div><strong>ElevenLabs</strong><small>Scribe v2 Realtime · {t("integrations.autoLanguage")}</small></div>
        {view?.configured ? <CheckCircle weight="fill" aria-label={t("integrations.configured")} /> : null}
      </div>
      <Field
        label={view?.configured ? t("integrations.replaceKey") : t("integrations.apiKey")}
        type="password"
        value={apiKey}
        autoComplete="off"
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
        disabled={blocked}
        placeholder={view?.configured ? t("integrations.keyConfiguredPlaceholder") : t("integrations.keyPlaceholder")}
        hint={t("integrations.writeOnlyHint")}
        onChange={(event) => setApiKey(event.target.value)}
      />
      <p className="integration-settings__privacy"><WarningCircle /> {t("integrations.audioDisclosure")}</p>
      {view?.configured ? <div className="integration-settings__voice">
        <div className="integration-settings__voice-heading"><SpeakerHigh weight="fill" /><span><strong>{t("integrations.voice")}</strong><small>{t("integrations.voiceHint")}</small></span></div>
        <label className="integration-settings__model">
          <span>{t("integrations.model")}</span>
          <select
            aria-label={t("integrations.model")}
            value={ttsModelId}
            disabled={blocked}
            onChange={(event) => setTtsModelId(event.target.value as ElevenLabsTtsModelId)}
          >
            <option value="eleven_flash_v2_5">{t("integrations.models.eleven_flash_v2_5")}</option>
            <option value="eleven_multilingual_v2">{t("integrations.models.eleven_multilingual_v2")}</option>
          </select>
          <small>{t(`integrations.modelHints.${ttsModelId}`)}</small>
        </label>
        <div className="integration-settings__voice-controls">
          <select
            aria-label={t("integrations.voice")}
            value={voiceId}
            disabled={blocked || voicesLoading}
            onChange={(event) => setVoiceId(event.target.value)}
          >
            <option value="">{voicesLoading ? t("integrations.loadingVoices") : t("integrations.chooseVoice")}</option>
            {voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}{voice.category ? ` · ${voice.category}` : ""}</option>)}
          </select>
          <Button
            variant="secondary"
            leadingIcon={previewVoiceId === voiceId && (previewState === "playing" || previewState === "loading") ? <Pause /> : <Play />}
            disabled={blocked || voicesLoading || !voiceId || !voices.find((voice) => voice.id === voiceId)?.previewAvailable}
            onClick={() => void previewVoice()}
          >
            {previewVoiceId === voiceId && previewState === "loading"
              ? t("integrations.loadingPreview")
              : previewVoiceId === voiceId && previewState === "playing"
                ? t("integrations.pausePreview")
                : previewVoiceId === voiceId && previewState === "paused"
                  ? t("integrations.resumePreview")
                  : t("integrations.previewVoice")}
          </Button>
          <Button variant="secondary" disabled={blocked || voicesLoading || !voiceId || (voiceId === view.voiceId && ttsModelId === (view.ttsModelId ?? "eleven_flash_v2_5"))} onClick={() => void saveVoice()}>{action === "voice" ? t("integrations.savingVoice") : t("integrations.saveVoice")}</Button>
        </div>
        {voiceId && !voicesLoading && !voices.find((voice) => voice.id === voiceId)?.previewAvailable ? <small className="form-hint">{t("integrations.previewUnavailable")}</small> : null}
        {view.voiceName ? <small className="integration-settings__voice-current"><CheckCircle weight="fill" /> {t("integrations.currentVoice", { voice: view.voiceName, model: t(`integrations.models.${view.ttsModelId ?? "eleven_flash_v2_5"}`) })}</small> : <small className="form-hint">{t("integrations.voiceRequired")}</small>}
      </div> : null}
      {!view?.configured && action !== "load" ? <p className="form-hint"><Microphone /> {t("integrations.nativeFallback")}</p> : null}
      {notice ? <p className="integration-settings__notice" role="status" aria-live="polite"><CheckCircle weight="fill" /> {notice}</p> : null}
      {error ? <p className="form-error" role="alert"><WarningCircle weight="fill" /> {error}</p> : null}
      {offline ? <p className="form-warning" role="status"><WarningCircle /> {t("integrations.offline")}</p> : null}
      <div className="integration-settings__actions">
        <Button variant="primary" leadingIcon={<Key />} disabled={blocked || !apiKey.trim()} onClick={() => void save()}>
          {action === "save" ? t("integrations.saving") : view?.configured ? t("integrations.replace") : t("integrations.save")}
        </Button>
        {view?.configured ? <Button variant="secondary" disabled={blocked} onClick={() => void test()}>{action === "test" ? t("integrations.testing") : t("integrations.test")}</Button> : null}
        {view?.configured && !confirmDelete ? <Button variant="ghost" leadingIcon={<Trash />} disabled={blocked} onClick={() => setConfirmDelete(true)}>{t("integrations.delete")}</Button> : null}
      </div>
      {confirmDelete ? <div className="integration-settings__confirm" role="group" aria-label={t("integrations.deleteConfirm")}><p>{t("integrations.deleteConfirm")}</p><div><Button variant="ghost" disabled={blocked} onClick={() => setConfirmDelete(false)}>{t("integrations.cancel")}</Button><Button variant="danger" disabled={blocked} onClick={() => void remove()}>{action === "delete" ? t("integrations.deleting") : t("integrations.confirmDelete")}</Button></div></div> : null}
    </Panel>
  );
}
