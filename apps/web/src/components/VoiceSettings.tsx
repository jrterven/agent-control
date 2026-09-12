import { CheckCircle, Key, Microphone, Trash, WarningCircle } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Badge, Button, Field, Panel } from "@hermes-control/ui";
import { api, type OpenAIIntegrationView, type VoiceSettingsView } from "../lib/api";
import { useAppStore } from "../store/appStore";
import type { VoiceProvider } from "../types";
import { OpenAIVoicePicker } from "./OpenAIVoicePicker";

type Action = "load" | "provider" | "save" | "delete" | "";

export function VoiceSettings() {
  const { t } = useTranslation();
  const csrfToken = useAppStore((state) => state.csrfToken);
  const offline = useAppStore((state) => state.authState === "offline");
  const demoMode = useAppStore((state) => state.demoMode);
  const currentProvider = useAppStore((state) => state.features?.voice?.provider ?? "elevenlabs");
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const [settings, setSettings] = useState<VoiceSettingsView | null>(null);
  const [integration, setIntegration] = useState<OpenAIIntegrationView | null>(null);
  const [apiKey, setApiKey] = useState("");
  const [action, setAction] = useState<Action>("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [confirmDelete, setConfirmDelete] = useState(false);
  const blocked = offline || demoMode || Boolean(action);

  useEffect(() => {
    if (offline || demoMode) return;
    let active = true;
    setAction("load");
    setError("");
    void Promise.all([api.voiceSettings(), api.openaiIntegration()])
      .then(([voice, openai]) => {
        if (!active) return;
        setSettings(voice);
        setIntegration(openai);
      })
      .catch(() => { if (active) setError(t("voiceSettings.loadError")); })
      .finally(() => { if (active) setAction(""); });
    return () => { active = false; };
  }, [demoMode, offline, t]);

  const refreshFeatures = async (provider?: VoiceProvider, configured?: boolean) => {
    // Apply confirmed mutations immediately so a failed bootstrap refresh cannot
    // leave the chat using a provider whose credential has just been removed.
    useAppStore.setState((state) => state.features ? {
      features: {
        ...state.features,
        ...(provider ? { voice: { provider } } : {}),
        ...(configured !== undefined ? { live: { available: configured, provider: "openai" as const, modelId: "gpt-live-1" as const } } : {}),
      },
    } : {});
    await api.bootstrap().then(hydrateBootstrap).catch(() => undefined);
  };

  const changeProvider = async (provider: VoiceProvider) => {
    if (blocked || !settings || provider === settings.provider || (provider === "openai_live" && !integration?.configured)) return;
    setAction("provider");
    setNotice("");
    setError("");
    try {
      const saved = await api.saveVoiceProvider(provider, csrfToken);
      setSettings(saved);
      await refreshFeatures(saved.provider);
      setNotice(t("voiceSettings.modeSaved"));
    } catch {
      setError(t("voiceSettings.modeError"));
    } finally {
      setAction("");
    }
  };

  const saveKey = async () => {
    const submittedKey = apiKey.trim();
    if (blocked || !integration || !submittedKey) return;
    setApiKey("");
    setAction("save");
    setNotice("");
    setError("");
    try {
      const saved = await api.saveOpenAIKey(submittedKey, csrfToken);
      setIntegration(saved);
      await refreshFeatures(undefined, saved.configured);
      setNotice(t("voiceSettings.saved"));
    } catch {
      setError(t("voiceSettings.saveError"));
    } finally {
      setAction("");
    }
  };

  const removeKey = async () => {
    if (blocked || !integration?.configured) return;
    setApiKey("");
    setAction("delete");
    setConfirmDelete(false);
    setNotice("");
    setError("");
    try {
      await api.deleteOpenAIKey(csrfToken);
      setIntegration({ configured: false, provider: "openai", modelId: "gpt-live-1" });
      setSettings({ provider: "elevenlabs" });
      await refreshFeatures("elevenlabs", false);
      setNotice(t("voiceSettings.deleted"));
    } catch {
      setError(t("voiceSettings.deleteError"));
    } finally {
      setAction("");
    }
  };

  return <Panel className="settings-section integration-settings" aria-labelledby="voice-settings-title">
    <header>
      <Microphone />
      <div><strong id="voice-settings-title">{t("voiceSettings.title")}</strong><p>{t("voiceSettings.description")}</p></div>
    </header>
    <label className="integration-settings__model">
      <span>{t("voiceSettings.mode")}</span>
      <select
        aria-label={t("voiceSettings.mode")}
        value={settings?.provider ?? currentProvider}
        disabled={blocked || !settings}
        onChange={(event) => void changeProvider(event.target.value as VoiceProvider)}
      >
        <option value="elevenlabs">{t("voiceSettings.elevenLabs")}</option>
        <option value="openai_live" disabled={!integration?.configured}>GPT-Live-1 · OpenAI</option>
      </select>
      <small>{t((settings?.provider ?? currentProvider) === "openai_live" ? "voiceSettings.liveHint" : "voiceSettings.elevenLabsHint")}</small>
      {!integration?.configured ? <small>{t("voiceSettings.keyRequired")}</small> : null}
    </label>
    <section className="integration-settings__voice" aria-labelledby="openai-integration-title">
      <div className="integration-settings__provider">
        <span><Key weight="duotone" /></span>
        <div><strong id="openai-integration-title">OpenAI</strong><small>GPT-Live-1</small></div>
        <div><Badge tone={integration?.configured ? "positive" : "neutral"}>
          {action === "load" ? t("integrations.loading") : integration?.configured ? t("integrations.configured") : t("integrations.notConfigured")}
        </Badge></div>
      </div>
      <OpenAIVoicePicker configured={integration?.configured === true} />
      <Field
        label={integration?.configured ? t("voiceSettings.replaceKey") : t("voiceSettings.apiKey")}
        aria-label={integration?.configured ? t("voiceSettings.replaceKey") : t("voiceSettings.apiKey")}
        type="password"
        value={apiKey}
        autoComplete="off"
        autoCapitalize="none"
        autoCorrect="off"
        spellCheck={false}
        disabled={blocked || !integration}
        placeholder={integration?.configured ? t("integrations.keyConfiguredPlaceholder") : t("integrations.keyPlaceholder")}
        hint={t("integrations.writeOnlyHint")}
        onChange={(event) => setApiKey(event.target.value)}
      />
      <p className="integration-settings__privacy"><WarningCircle /> {t("voiceSettings.disclosure")}</p>
      <div className="integration-settings__actions">
        <Button variant="primary" leadingIcon={<Key />} disabled={blocked || !integration || !apiKey.trim()} onClick={() => void saveKey()}>
          {action === "save" ? t("integrations.saving") : integration?.configured ? t("integrations.replace") : t("integrations.save")}
        </Button>
        {integration?.configured && !confirmDelete ? <Button variant="ghost" leadingIcon={<Trash />} disabled={blocked} onClick={() => setConfirmDelete(true)}>{t("integrations.delete")}</Button> : null}
      </div>
      {confirmDelete ? <div className="integration-settings__confirm" role="group" aria-label={t("voiceSettings.deleteConfirm")}>
        <p>{t("voiceSettings.deleteConfirm")}</p>
        <div><Button variant="ghost" disabled={blocked} onClick={() => setConfirmDelete(false)}>{t("integrations.cancel")}</Button><Button variant="danger" disabled={blocked} onClick={() => void removeKey()}>{t("integrations.confirmDelete")}</Button></div>
      </div> : null}
    </section>
    {notice ? <p className="integration-settings__notice" role="status" aria-live="polite"><CheckCircle weight="fill" /> {notice}</p> : null}
    {error ? <p className="form-error" role="alert"><WarningCircle weight="fill" /> {error}</p> : null}
    {offline ? <p className="form-warning" role="status"><WarningCircle /> {t("integrations.offline")}</p> : null}
  </Panel>;
}
