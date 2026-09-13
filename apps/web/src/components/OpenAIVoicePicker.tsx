import { CheckCircle, Play, SpeakerHigh, Stop, WarningCircle } from "@phosphor-icons/react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@hermes-control/ui";
import { api, type OpenAILiveVoiceId } from "../lib/api";
import { OPENAI_LIVE_VOICES } from "../lib/openaiLiveVoices";
import { OpenAILiveVoicePreview, voicePreviewSupported } from "../lib/openaiLiveVoicePreview";
import type { LivePhase } from "../lib/openaiLiveClient";
import { claimVoicePreview, releaseVoicePreview } from "../lib/voicePreviewPlayback";
import { useAppStore } from "../store/appStore";
import { getCurrentLanguage } from "../i18n";

export function OpenAIVoicePicker({ configured }: { configured: boolean }) {
  const { t } = useTranslation();
  const profiles = useAppStore((state) => state.profiles);
  const gateways = useAppStore((state) => state.gateways);
  const authState = useAppStore((state) => state.authState);
  const [scope, setScope] = useState("");
  const profileId = profiles.some((profile) => profile.id === scope) ? scope : "";

  return <div>
    {profiles.length > 0 ? <label className="integration-settings__model">
      <span>{t("voiceSettings.scope")}</span>
      <select aria-label={t("voiceSettings.scope")} value={profileId} onChange={(event) => setScope(event.target.value)}>
        <option value="">{t("voiceSettings.generalVoice")}</option>
        {profiles.map((profile) => {
          const gateway = gateways.find((item) => item.id === profile.gatewayId);
          return <option key={profile.id} value={profile.id}>{profile.displayName}{gateway ? ` · ${gateway.name}` : ""}</option>;
        })}
      </select>
      <small>{t("voiceSettings.scopeHint")}</small>
    </label> : null}
    <OpenAIVoiceEditor key={`${authState}:${profileId}`} configured={configured} profileId={profileId || undefined} />
  </div>;
}

function OpenAIVoiceEditor({ configured, profileId }: { configured: boolean; profileId?: string }) {
  const { t } = useTranslation();
  const csrfToken = useAppStore((state) => state.csrfToken);
  const authState = useAppStore((state) => state.authState);
  const demoMode = useAppStore((state) => state.demoMode);
  const [online, setOnline] = useState(() => navigator.onLine);
  const [voiceId, setVoiceId] = useState<OpenAILiveVoiceId>("marin");
  const [savedVoiceId, setSavedVoiceId] = useState<OpenAILiveVoiceId | null>(null);
  const [inherited, setInherited] = useState(false);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [previewError, setPreviewError] = useState("");
  const [previewPhase, setPreviewPhase] = useState<LivePhase>("idle");
  const [playbackBlocked, setPlaybackBlocked] = useState(false);
  const previewRef = useRef<OpenAILiveVoicePreview | null>(null);
  const previewOwner = useRef({});
  const blocked = authState !== "authenticated" || demoMode || !online;
  const selectedVoice = OPENAI_LIVE_VOICES.find((voice) => voice.id === voiceId);
  const savedVoice = OPENAI_LIVE_VOICES.find((voice) => voice.id === savedVoiceId);
  const previewActive = previewPhase === "connecting" || previewPhase === "listening" || previewPhase === "stopping";
  const previewSupported = voicePreviewSupported();

  const releasePreview = useCallback(() => {
    const preview = previewRef.current;
    previewRef.current = null;
    releaseVoicePreview(previewOwner.current);
    preview?.dispose();
  }, []);

  const stopPreview = useCallback(() => {
    releasePreview();
    setPreviewPhase("idle");
    setPlaybackBlocked(false);
  }, [releasePreview]);

  useEffect(() => {
    const offline = () => { setOnline(false); stopPreview(); };
    const online = () => setOnline(true);
    const hidden = () => { if (document.visibilityState === "hidden") stopPreview(); };
    window.addEventListener("offline", offline);
    window.addEventListener("online", online);
    window.addEventListener("pagehide", stopPreview);
    document.addEventListener("visibilitychange", hidden);
    return () => {
      releasePreview();
      window.removeEventListener("offline", offline);
      window.removeEventListener("online", online);
      window.removeEventListener("pagehide", stopPreview);
      document.removeEventListener("visibilitychange", hidden);
    };
  }, [releasePreview, stopPreview]);

  useEffect(() => {
    if (blocked || !configured) stopPreview();
  }, [blocked, configured, stopPreview]);

  useEffect(() => {
    if (blocked) return;
    let active = true;
    setLoading(true);
    setError("");
    void (profileId ? api.openaiProfileVoice(profileId) : api.openaiVoice())
      .then((saved) => {
        if (!active) return;
        setVoiceId(saved.voiceId);
        setSavedVoiceId(saved.voiceId);
        setInherited("inherited" in saved && saved.inherited === true);
      })
      .catch(() => { if (active) setError(t("voiceSettings.voiceLoadError")); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [blocked, profileId, t]);

  const saveVoice = async (useGeneral = false) => {
    if (blocked || saving || loading || !savedVoiceId) return;
    if (useGeneral ? !profileId || inherited : voiceId === savedVoiceId && (!profileId || !inherited)) return;
    stopPreview();
    setSaving(true);
    setNotice("");
    setError("");
    try {
      const saved = profileId
        ? useGeneral ? await api.deleteOpenAIProfileVoice(profileId, csrfToken) : await api.saveOpenAIProfileVoice(profileId, voiceId, csrfToken)
        : await api.saveOpenAIVoice(voiceId, csrfToken);
      setSavedVoiceId(saved.voiceId);
      setVoiceId(saved.voiceId);
      setInherited("inherited" in saved && saved.inherited === true);
      setNotice(t("voiceSettings.voiceSaved"));
    } catch {
      setError(t("voiceSettings.voiceSaveError"));
    } finally {
      setSaving(false);
    }
  };

  const playPreview = async () => {
    if (previewRef.current) { previewRef.current.stop(); return; }
    if (blocked || loading || !savedVoiceId || !configured || !selectedVoice || !previewSupported || document.visibilityState === "hidden") return;
    setPreviewError("");
    setPlaybackBlocked(false);
    const preview = new OpenAILiveVoicePreview({
      negotiate: (sdp, signal) => api.createLiveVoicePreview({ sdp, voiceId, language: getCurrentLanguage() }, csrfToken, signal),
      onPhase: (phase) => {
        if (previewRef.current !== preview) return;
        setPreviewPhase(phase);
        if (phase === "idle" || phase === "error") {
          releasePreview();
          setPlaybackBlocked(false);
        }
      },
      onIssue: (issue) => {
        if (previewRef.current !== preview) return;
        setPreviewError(t(issue === "quota" || issue === "auth" ? `liveVoice.${issue}` : "voiceSettings.previewError"));
      },
      onPlaybackBlocked: (value) => { if (previewRef.current === preview) setPlaybackBlocked(value); },
    });
    previewRef.current = preview;
    claimVoicePreview(previewOwner.current, stopPreview);
    setPreviewPhase("connecting");
    try {
      await preview.start();
    } catch {
      if (previewRef.current !== preview) return;
      stopPreview();
      setPreviewError(t("voiceSettings.previewError"));
    }
  };

  return <div className="integration-settings__voice">
    <div className="integration-settings__voice-heading"><SpeakerHigh weight="fill" /><span><strong>{t("voiceSettings.voice")}</strong><small>{t("voiceSettings.voiceHint")}</small></span></div>
    <div className="integration-settings__voice-controls">
      <select
        aria-label={t("voiceSettings.voice")}
        value={voiceId}
        disabled={blocked || loading || saving || !savedVoiceId}
        onChange={(event) => {
          stopPreview();
          setPreviewError("");
          setNotice("");
          setVoiceId(event.target.value as OpenAILiveVoiceId);
        }}
      >
        {OPENAI_LIVE_VOICES.map((voice) => <option key={voice.id} value={voice.id}>{voice.name}</option>)}
      </select>
      <Button
        variant="secondary"
        leadingIcon={previewActive ? <Stop weight="fill" /> : <Play />}
        aria-label={t(previewActive ? "voiceSettings.stopPreview" : "voiceSettings.preview")}
        disabled={previewPhase === "stopping" || (!previewActive && (blocked || loading || saving || !savedVoiceId || !configured || !selectedVoice || !previewSupported))}
        onClick={() => void playPreview()}
      >{t(previewPhase === "connecting" ? "voiceSettings.loadingPreview" : previewActive ? "voiceSettings.stopPreview" : "voiceSettings.preview")}</Button>
      <Button variant="secondary" disabled={blocked || loading || saving || !savedVoiceId || (voiceId === savedVoiceId && (!profileId || !inherited))} onClick={() => void saveVoice()}>
        {t(saving ? "voiceSettings.savingVoice" : "voiceSettings.saveVoice")}
      </Button>
      {profileId ? <Button variant="ghost" disabled={blocked || loading || saving || !savedVoiceId || inherited} onClick={() => void saveVoice(true)}>{t("voiceSettings.useGeneral")}</Button> : null}
    </div>
    {playbackBlocked ? <Button variant="secondary" leadingIcon={<Play />} onClick={() => void previewRef.current?.play()}>{t("voiceSettings.playPreview")}</Button> : null}
    <small className="form-hint">{t("voiceSettings.previewHint")}</small>
    {!configured ? <small className="form-hint">{t("voiceSettings.previewKeyRequired")}</small> : !previewSupported ? <small className="form-hint">{t("voiceSettings.previewUnavailable")}</small> : null}
    {loading ? <small role="status">{t("integrations.loading")}</small> : savedVoice ? <small className="integration-settings__voice-current"><CheckCircle weight="fill" /> {t(profileId ? inherited ? "voiceSettings.inheritedVoice" : "voiceSettings.agentVoice" : "voiceSettings.currentVoice", { voice: savedVoice.name })}</small> : null}
    {notice ? <p className="integration-settings__notice" role="status"><CheckCircle weight="fill" /> {notice}</p> : null}
    {error || previewError ? <p className="form-error" role="alert"><WarningCircle weight="fill" /> {error || previewError}</p> : null}
  </div>;
}
