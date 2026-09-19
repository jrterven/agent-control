import { CheckCircle, Eye, WarningCircle } from "@phosphor-icons/react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Badge, Button, Panel } from "@hermes-control/ui";
import { VISION_INTERVAL_SECONDS, type VisionIntervalSeconds, type VisionModelId, type VisionPreferences } from "@hermes-control/shared-types";
import { visionApi, VISION_PREFERENCES_CHANGED } from "../lib/vision";
import { visionSettingsCopy } from "../lib/visionSettingsCopy";
import { useAppStore } from "../store/appStore";
import "./VisionSettings.css";

export function VisionSettings() {
  const ownerId = useAppStore((state) => state.userId);
  const generation = useAppStore((state) => state.authGeneration);
  const authState = useAppStore((state) => state.authState);
  const demoMode = useAppStore((state) => state.demoMode);
  const configured = useAppStore((state) => state.features?.live?.available);
  // Never render one account's saved preference while the next account loads.
  return <VisionSettingsEditor key={`${ownerId}:${generation}:${authState}:${demoMode}:${configured}`} ownerId={ownerId} generation={generation} />;
}

function VisionSettingsEditor({ ownerId, generation }: { ownerId?: string; generation: number }) {
  const { i18n } = useTranslation();
  const copy = visionSettingsCopy(i18n.resolvedLanguage ?? i18n.language);
  const authState = useAppStore((state) => state.authState);
  const demoMode = useAppStore((state) => state.demoMode);
  const [online, setOnline] = useState(() => navigator.onLine);
  const [saved, setSaved] = useState<VisionPreferences | null>(null);
  const [modelId, setModelId] = useState<VisionModelId>("gpt-5.6-luna");
  const [intervalSeconds, setIntervalSeconds] = useState<VisionIntervalSeconds>(5);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [loadVersion, setLoadVersion] = useState(0);
  const [notice, setNotice] = useState(false);
  const [error, setError] = useState<"load" | "save" | null>(null);
  const mounted = useRef(true);
  const savingRequest = useRef<AbortController | null>(null);
  const blocked = authState !== "authenticated" || demoMode || !online;
  const dirty = saved !== null && (modelId !== saved.modelId || intervalSeconds !== saved.intervalSeconds);

  const permitted = () => {
    const state = useAppStore.getState();
    return mounted.current && state.authState === "authenticated" && !state.demoMode
      && state.userId === ownerId && state.authGeneration === generation && navigator.onLine;
  };

  useEffect(() => {
    mounted.current = true;
    const offline = () => { setOnline(false); savingRequest.current?.abort(); };
    const online = () => setOnline(true);
    window.addEventListener("offline", offline);
    window.addEventListener("online", online);
    return () => {
      mounted.current = false;
      savingRequest.current?.abort();
      window.removeEventListener("offline", offline);
      window.removeEventListener("online", online);
    };
  }, []);

  useEffect(() => {
    if (blocked) return;
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void visionApi.preferences(controller.signal).then((preferences) => {
      if (controller.signal.aborted || !permitted()) return;
      setSaved(preferences);
      setModelId(preferences.modelId);
      setIntervalSeconds(preferences.intervalSeconds);
    }).catch(() => {
      if (!controller.signal.aborted && permitted()) setError("load");
    }).finally(() => {
      if (!controller.signal.aborted && permitted()) setLoading(false);
    });
    return () => controller.abort();
  }, [blocked, loadVersion]);

  const save = async () => {
    if (blocked || loading || !dirty || savingRequest.current || !permitted()) return;
    const controller = new AbortController();
    savingRequest.current = controller;
    setSaving(true);
    setError(null);
    setNotice(false);
    try {
      const preferences = await visionApi.savePreferences({ modelId, intervalSeconds }, useAppStore.getState().csrfToken, controller.signal);
      if (controller.signal.aborted || !permitted()) return;
      setSaved(preferences);
      setModelId(preferences.modelId);
      setIntervalSeconds(preferences.intervalSeconds);
      setNotice(true);
      window.dispatchEvent(new Event(VISION_PREFERENCES_CHANGED));
    } catch {
      if (!controller.signal.aborted && permitted()) setError("save");
    } finally {
      if (savingRequest.current === controller) savingRequest.current = null;
      if (mounted.current) setSaving(false);
    }
  };

  const disabled = blocked || loading || saving || saved === null;
  return <Panel id="camera-preferences" className="settings-section vision-settings" aria-labelledby="vision-settings-title">
    <header><Eye aria-hidden="true" /><div><strong id="vision-settings-title">{copy.title}</strong><p>{copy.description}</p></div></header>
    <div className="vision-settings__connection">
      <Badge tone={saved?.configured ? "positive" : "neutral"}>{loading ? copy.loading : saved?.configured ? copy.configured : copy.notConfigured}</Badge>
      <a href="#openai-integration-title">{copy.configure}</a>
    </div>
    <div className="vision-settings__fields">
      <label className="integration-settings__model"><span>{copy.model}</span>
        <select aria-label={copy.model} value={modelId} disabled={disabled} onChange={(event) => { setModelId(event.target.value as VisionModelId); setNotice(false); }}>
          <option value="gpt-5.6-luna">{copy.luna}</option><option value="gpt-5.6-terra">{copy.terra}</option><option value="gpt-5.6-sol">{copy.sol}</option>
        </select><small>{copy.modelHint}</small>
      </label>
      <label className="integration-settings__model"><span>{copy.interval}</span>
        <select aria-label={copy.interval} value={intervalSeconds} disabled={disabled} onChange={(event) => { setIntervalSeconds(Number(event.target.value) as VisionIntervalSeconds); setNotice(false); }}>
          {VISION_INTERVAL_SECONDS.map((value) => <option key={value} value={value}>{copy.seconds(value)}</option>)}
        </select><small>{copy.intervalHint}</small>
      </label>
    </div>
    <p className="vision-settings__disclosure">{copy.disclosure}</p>
    <Button variant="primary" disabled={disabled || !dirty} onClick={() => void save()}>{saving ? copy.saving : copy.save}</Button>
    {notice ? <p className="integration-settings__notice" role="status"><CheckCircle aria-hidden="true" /> {copy.saved}</p> : null}
    {error ? <div className="vision-settings__error"><p className="form-error" role="alert"><WarningCircle aria-hidden="true" /> {error === "load" ? copy.loadError : copy.saveError}</p>{error === "load" ? <Button variant="ghost" disabled={blocked || loading} onClick={() => setLoadVersion((value) => value + 1)}>{copy.retry}</Button> : null}</div> : null}
    {blocked ? <p className="form-warning" role="status">{!online || authState === "offline" ? copy.offline : copy.unavailable}</p> : null}
  </Panel>;
}
