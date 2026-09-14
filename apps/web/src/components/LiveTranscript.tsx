import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Waveform } from "@phosphor-icons/react";
import { Button } from "@hermes-control/ui";
import { groupLiveFragments, type LiveTranscript as Transcript } from "../lib/liveTranscripts";
import type { LiveConversationRow } from "../lib/liveDelegation";

export function LiveTranscriptRows({ rows, agentName }: { rows: LiveConversationRow[]; agentName: string }) {
  const { t } = useTranslation();
  return <div className="live-transcript__rows">
    {rows.map((row, index) => <div key={row.id ?? index} className={`live-transcript__row live-transcript__row--${row.role}`} data-speaker={row.role}>
      <small>{row.role === "user" ? t("liveVoice.inputCaption") : agentName}</small><p>{row.text}</p>
    </div>)}
  </div>;
}

export function LiveTranscript({ call, agentName, retry }: { call: Transcript; agentName: string; retry: () => void }) {
  const { t, i18n } = useTranslation();
  const rows = useMemo(() => groupLiveFragments(call.fragments), [call.fragments]);
  return <section className="live-transcript" aria-label={t("liveVoice.transcript")}>
    <header className="live-transcript__header"><Waveform aria-hidden="true" /><strong>{t("liveVoice.transcript")}</strong><time dateTime={call.createdAt}>{new Date(call.createdAt).toLocaleString(i18n.language, { dateStyle: "short", timeStyle: "short" })}</time></header>
    <LiveTranscriptRows rows={rows} agentName={agentName} />
    {call.saveState === "failed" ? <div className="live-transcript__save" role="status"><span>{t("liveVoice.transcriptSaveError")}</span><Button size="sm" variant="ghost" onClick={retry}>{t("liveVoice.transcriptRetry")}</Button></div> : null}
  </section>;
}
