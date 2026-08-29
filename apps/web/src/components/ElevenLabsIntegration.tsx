import { CheckCircle, Key, Microphone, Trash, WarningCircle } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Badge, Button, Field, Panel } from "@hermes-control/ui";
import { api, type ElevenLabsIntegrationView } from "../lib/api";
import { useAppStore } from "../store/appStore";

type Action = "load" | "save" | "test" | "delete" | "";

export function ElevenLabsIntegration() {
  const { t } = useTranslation();
  const csrfToken = useAppStore((state) => state.csrfToken);
  const offline = useAppStore((state) => state.authState === "offline");
  const demoMode = useAppStore((state) => state.demoMode);
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const [view, setView] = useState<ElevenLabsIntegrationView | null>(null);
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
    void api.elevenLabsIntegration()
      .then((integration) => { if (active) setView(integration); })
      .catch(() => { if (active) setError(t("integrations.loadError")); })
      .finally(() => { if (active) setAction(""); });
    return () => { active = false; };
  }, [demoMode, offline, t]);

  const refreshFeatures = async () => {
    const bootstrap = await api.bootstrap();
    hydrateBootstrap(bootstrap);
  };

  const save = async () => {
    const submittedKey = apiKey.trim();
    if (!submittedKey || blocked) return;
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

  const remove = async () => {
    if (!view?.configured || blocked) return;
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
