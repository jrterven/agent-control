import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@hermes-control/ui";
import { speakerMutation, speakerRoot, type SpeakerState, type VoicePerson } from "../lib/speakerRecognition";

export function SpeakerRecognitionStatus({ state, people = [], proposal = false }: { state?: SpeakerState; people?: VoicePerson[]; proposal?: boolean }) {
  const { t } = useTranslation();
  const [feedback, setFeedback] = useState<{ id: string; notice: string }>();
  const [expected, setExpected] = useState("");
  const [saving, setSaving] = useState(false);
  if ((!state || state.phase === "idle") && !proposal) return null;
  const job = state?.job;
  const result = job?.result;
  const correct = async (value: "correct" | "incorrect") => {
    if (!job || saving) return;
    setSaving(true);
    try {
      await speakerMutation(`${speakerRoot}/jobs/${job.id}/feedback`, "PUT", { feedback: value, expectedPersonId: expected || null });
      setFeedback({ id: job.id, notice: t("speaker.feedbackSaved") });
    } catch { setFeedback({ id: job.id, notice: t("speaker.feedbackError") }); }
    finally { setSaving(false); }
  };
  return <div className="speaker-observation">
    {state && state.phase !== "idle" ? <>
      <span role="status">{t(`speaker.${result?.state ?? state.phase}`, { name: result?.name ?? "", seconds: Math.floor(state.seconds ?? 0) })}</span>
      {state.errorCode === "PYANNOTE_NO_SPEECH" ? <p>{t("speaker.noSpeech")}</p> : null}
      {state.errorCode === "PYANNOTE_CREDENTIAL_REJECTED" ? <p>{t("speaker.invalidKey")}</p> : null}
      {state.errorCode === "PYANNOTE_QUOTA_EXCEEDED" ? <p>{t("speaker.quota")}</p> : null}
      {["PYANNOTE_BUSY", "PYANNOTE_RATE_LIMITED"].includes(state.errorCode ?? "") ? <p>{t("speaker.limited")}</p> : null}
      <small>{t("speaker.informational")}</small>
      {result?.segments.length ? <details><summary>{result.segments.length} · {t("speaker.seconds", { count: job?.duration })}</summary>
        <ul>{result.segments.map((s, i) => <li key={i}>{s.start.toFixed(1)}–{s.end.toFixed(1)} s · {t(`speaker.${s.state}`, { name: s.name ?? "" })}{s.score !== null ? ` · ${s.score.toFixed(1)}` : ""}</li>)}</ul>
      </details> : null}
      {job?.kind === "identify" && result ? <div className="speaker-actions">
        {people.length ? <label>{t("speaker.expected")}<select value={expected} onChange={(event) => setExpected(event.target.value)}><option value="">{t("speaker.unknownOption")}</option>{people.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}</select></label> : null}
        <Button size="sm" variant="ghost" disabled={saving} onClick={() => void correct("correct")}>{t("speaker.correct")}</Button>
        <Button size="sm" variant="ghost" disabled={saving} onClick={() => void correct("incorrect")}>{t("speaker.incorrect")}</Button>
        {feedback?.id === job.id ? <span role="status">{feedback.notice}</span> : null}
      </div> : null}
    </> : null}
    {proposal ? <p>{t("speaker.proposal")} <a href="/settings#speaker-recognition">{t("speaker.settings")}</a></p> : null}
  </div>;
}
