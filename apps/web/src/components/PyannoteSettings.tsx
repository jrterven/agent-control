import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Button, Field, Panel } from "@hermes-control/ui";
import { request } from "../lib/api";
import { useAppStore } from "../store/appStore";
import { usePwaUpdateStore } from "../lib/pwaUpdate";
import { RecognitionCapture, notifySpeakerChanged, speakerConfig, speakerMutation, speakerRoot, teeMicrophone, type SpeakerConfiguration, type SpeakerMetrics, type SpeakerState, type VoicePerson } from "../lib/speakerRecognition";
import { SpeakerRecognitionStatus } from "./SpeakerRecognitionStatus";

export function PyannoteSettings() {
  const { t } = useTranslation();
  const owner = useAppStore((s) => `${s.userId}:${s.authGeneration}`);
  const available = useAppStore((s) => s.authState === "authenticated" && !s.demoMode);
  const [config, setConfig] = useState<SpeakerConfiguration>();
  const [people, setPeople] = useState<VoicePerson[]>([]);
  const [metrics, setMetrics] = useState<SpeakerMetrics>();
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(false);
  const [recording, setRecording] = useState<VoicePerson | "test">();
  const [state, setState] = useState<SpeakerState>({ phase: "idle" });
  const capture = useRef<RecognitionCapture | undefined>(undefined);
  const audio = useRef<{ stream: MediaStream; abort: AbortController } | undefined>(undefined);
  const epoch = useRef(0);
  const setUpdateBlocker = usePwaUpdateStore((s) => s.setBlocker);
  useEffect(() => {
    setUpdateBlocker("dictation", state.phase === "collecting" || state.phase === "processing");
    return () => setUpdateBlocker("dictation", false);
  }, [setUpdateBlocker, state.phase]);
  const stopAudio = () => { audio.current?.abort.abort(); audio.current?.stream.getTracks().forEach((track) => track.stop()); audio.current = undefined; };
  const stop = () => { epoch.current += 1; stopAudio(); capture.current?.stop(); capture.current = undefined; setState({ phase: "idle" }); };
  const reload = async () => {
    const current = epoch.current;
    const [next, catalog, report] = await Promise.all([
      request<SpeakerConfiguration>(speakerConfig), request<{ items: VoicePerson[] }>(`${speakerRoot}/people`), request<SpeakerMetrics>(`${speakerRoot}/metrics`),
    ]);
    if (current !== epoch.current) return;
    setConfig(next); setPeople(catalog.items); setMetrics(report);
  };
  useEffect(() => {
    setKey(""); setConfig(undefined); setPeople([]); setMetrics(undefined);
    setName(""); setConsent(false); setRecording(undefined); setBusy(false); setFailure(false);
    if (available) void reload().catch(() => setFailure(true));
    const hidden = () => { if (document.visibilityState === "hidden") stop(); };
    const leaving = () => stop();
    document.addEventListener("visibilitychange", hidden); window.addEventListener("pagehide", leaving);
    return () => { stop(); document.removeEventListener("visibilitychange", hidden); window.removeEventListener("pagehide", leaving); };
  }, [owner, available]);
  const action = async (operation: () => Promise<unknown>, changed = true) => {
    if (!available || busy) return;
    stop(); setBusy(true); setFailure(false);
    const current = epoch.current;
    try {
      await operation();
      if (current !== epoch.current) return;
      if (changed) notifySpeakerChanged();
      await reload();
    } catch { if (current === epoch.current) setFailure(true); }
    finally { if (current === epoch.current) setBusy(false); }
  };
  const start = async () => {
    if (!recording || !config?.enabled || busy || capture.current) return;
    const current = ++epoch.current;
    setFailure(false); setState({ phase: "collecting", seconds: 0 });
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
      if (current !== epoch.current) { stream.getTracks().forEach((track) => track.stop()); return; }
      const abort = new AbortController(); audio.current = { stream, abort };
      const instance = new RecognitionCapture({ mode: recording === "test" ? "test" : "enroll", personId: recording === "test" ? undefined : recording.id, onState: (next) => {
        if (current !== epoch.current) return;
        setState(next);
        if (["processing", "result", "unavailable"].includes(next.phase)) stopAudio();
        if (next.phase === "result") void reload().catch(() => setFailure(true));
      } });
      capture.current = instance;
      await teeMicrophone(stream, instance.pcm, abort.signal);
      await instance.begin();
    } catch { if (current === epoch.current) { stopAudio(); setState({ phase: "unavailable" }); } }
  };
  const blocked = !available || busy;
  const report = metrics?.summary;
  return <Panel className="settings-section speaker-settings" id="speaker-recognition">
    <header><div><h3>{t("speaker.title")}</h3><p>{t("speaker.description")}</p></div></header>
    {failure ? <p role="alert">{t("speaker.failed")}</p> : null}
    <label className="speaker-toggle"><input type="checkbox" checked={config?.enabled ?? false} disabled={blocked || !config?.configured} onChange={(event) => void action(() => speakerMutation(speakerConfig, "PUT", { enabled: event.target.checked, windowSeconds: config?.windowSeconds ?? 5 }))} />{t("speaker.enabled")}</label>
    <div className="speaker-actions"><Field label={t("speaker.key")} type="password" autoComplete="off" autoCapitalize="none" spellCheck={false} value={key} disabled={blocked} onChange={(event) => setKey(event.target.value)} />
      <Button disabled={blocked || !key.trim()} onClick={() => { const value = key; setKey(""); void action(() => speakerMutation(`${speakerConfig}/key`, "PUT", { apiKey: value })); }}>{t("speaker.saveKey")}</Button>
      <Button variant="ghost" disabled={blocked || !config?.configured} onClick={() => void action(() => speakerMutation(`${speakerConfig}/test`, "POST"), false)}>{t("speaker.testConnection")}</Button>
      <Button variant="ghost" disabled={blocked || !config?.configured} onClick={() => void action(() => speakerMutation(`${speakerConfig}/key`, "DELETE"))}>{t("speaker.deleteKey")}</Button>
    </div>
    <p>{t(config?.configured ? "speaker.saved" : "speaker.notConfigured")} · {t("speaker.connectionLabel")}: {t(config?.connectionTested ? "speaker.tested" : "speaker.notTested")} · {t("speaker.recognitionLabel")}: {t(config?.recognitionTested ? "speaker.recognitionTested" : "speaker.notTested")}</p>
    <small>{t("speaker.testHint")}</small>
    <p>{t("speaker.privacy")}</p>
    <label className="hc-field"><span>{t("speaker.window")}</span><select value={config?.windowSeconds ?? 5} disabled={blocked || !config} onChange={(event) => void action(() => speakerMutation(speakerConfig, "PUT", { enabled: config?.enabled ?? false, windowSeconds: Number(event.target.value) }))}>{[5, 10, 20].map((s) => <option key={s} value={s}>{t("speaker.seconds", { count: s })}</option>)}</select></label>
    <h4>{t("speaker.people")}</h4>
    {people.map((person) => <PersonRow key={`${person.id}:${person.name}`} person={person} disabled={blocked} canRecord={config?.enabled ?? false} save={(next) => void action(() => speakerMutation(`${speakerRoot}/people/${person.id}`, "PUT", { name: next, consent: true }))} remove={() => void action(() => speakerMutation(`${speakerRoot}/people/${person.id}`, "DELETE"))} record={() => { stop(); setRecording(person); }} />)}
    <form onSubmit={(event) => { event.preventDefault(); const id = crypto.randomUUID(); void action(async () => { const current = epoch.current; await speakerMutation(`${speakerRoot}/people/${id}`, "PUT", { name, consent }); if (current !== epoch.current) return; setName(""); setConsent(false); setRecording({ id, name, consent: true, ready: false, model: "precision-3" }); }); }}>
      <Field id="speaker-new-name" label={t("speaker.name")} value={name} maxLength={100} onChange={(event) => setName(event.target.value)} disabled={blocked} />
      <label className="speaker-toggle"><input type="checkbox" checked={consent} disabled={blocked} onChange={(event) => setConsent(event.target.checked)} />{t("speaker.consent")}</label>
      <Button type="submit" disabled={blocked || !name.trim() || !consent || people.length >= 50}>{t("speaker.add")}</Button>
    </form>
    <p>{t("speaker.evaluate")}</p>
    <small>{t("speaker.scores")}</small>
    <Button variant="ghost" disabled={blocked || !config?.enabled || !people.some((p) => p.ready)} onClick={() => { stop(); setRecording("test"); }}>{t("speaker.test")}</Button>
    {recording ? <div className="speaker-recording">
      <strong>{recording === "test" ? t("speaker.test") : recording.name}</strong>
      <p>{t(recording === "test" ? "speaker.evaluate" : "speaker.guide")}</p>
      {state.phase === "collecting" ? <p role="status">{t("speaker.recording", { seconds: Math.floor(state.seconds ?? 0) })}</p> : null}
      <div className="speaker-actions"><Button disabled={blocked || !config?.enabled || state.phase !== "idle"} onClick={() => void start()}>{t("speaker.start")}</Button><Button variant="ghost" onClick={() => { stop(); setRecording(undefined); }}>{t("speaker.stop")}</Button></div>
      <SpeakerRecognitionStatus state={state} people={people} />
    </div> : null}
    <p><small>{t("speaker.native")}</small></p>
    <h4>{t("speaker.metrics")}</h4>
    <div className="speaker-actions"><Button variant="ghost" disabled={blocked} onClick={() => void reload().catch(() => setFailure(true))}>{t("speaker.refresh")}</Button>
      <Button variant="ghost" disabled={!metrics} onClick={() => { const url = URL.createObjectURL(new Blob([JSON.stringify(metrics, null, 2)], { type: "application/json" })); const a = document.createElement("a"); a.href = url; a.download = "voice-pilot-report.json"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }}>{t("speaker.export")}</Button>
      <a href="https://dashboard.pyannote.ai" target="_blank" rel="noreferrer">{t("speaker.dashboard")}</a>
    </div>
    {report ? <>
      <p>{t("speaker.jobs")}: {report.jobs} · {t("speaker.reviewed")}: {report.reviewed} · {t("speaker.accuracy")}: {report.reviewed ? `${Math.round(report.correct / report.reviewed * 100)}%` : "—"}</p>
      <p>{t("speaker.consumption")}: {metrics?.estimatedBillableSeconds} · {t("speaker.voiceprints")}: {metrics?.voiceprintsCreated} · {t("speaker.unresolved")}: {metrics?.unresolvedCharges}</p>
      <div className="speaker-table"><table><thead><tr><th>{t("speaker.window")}</th><th>{t("speaker.reviewed")}</th><th>{t("speaker.falseMatches")}</th><th>{t("speaker.latency")}</th></tr></thead><tbody>{Object.entries(metrics?.windows ?? {}).map(([seconds, row]) => <tr key={seconds}><td>{seconds} s</td><td>{row.correct} / {row.reviewed}</td><td>{row.falseMatches}</td><td>{row.latencyMs.totalMs.p50 ?? "—"} / {row.latencyMs.totalMs.p95 ?? "—"} ms</td></tr>)}</tbody></table></div>
      {metrics?.recent.filter((job) => job.result && job.kind === "identify").slice(0, 10).map((job) => <details key={job.id}><summary>{new Date(job.createdAt).toLocaleString()} · {job.duration} s</summary><SpeakerRecognitionStatus state={{ phase: "result", job }} people={people} /></details>)}
    </> : <p>{t("speaker.noData")}</p>}
    <small>{t("speaker.noBalance")} {t("speaker.reportScope")}</small>
  </Panel>;
}

function PersonRow({ person, disabled, canRecord, save, remove, record }: { person: VoicePerson; disabled: boolean; canRecord: boolean; save: (name: string) => void; remove: () => void; record: () => void }) {
  const { t } = useTranslation(); const [name, setName] = useState(person.name);
  return <div className="speaker-person"><Field id={`speaker-name-${person.id}`} label={t("speaker.name")} value={name} maxLength={100} disabled={disabled} onChange={(event) => setName(event.target.value)} /><span>{t(person.ready ? "speaker.ready" : "speaker.pending")}</span><div className="speaker-actions">
    <Button size="sm" variant="ghost" disabled={disabled || !name.trim() || name === person.name} onClick={() => save(name)}>{t("speaker.rename")}</Button>
    <Button size="sm" variant="ghost" disabled={disabled || !canRecord} onClick={record}>{t(person.ready ? "speaker.rerecord" : "speaker.record")}</Button>
    <Button size="sm" variant="ghost" disabled={disabled} onClick={remove}>{t("speaker.remove")}</Button>
  </div></div>;
}
