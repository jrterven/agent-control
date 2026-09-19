import type { VisionObservation } from "@hermes-control/shared-types";
import { Eye } from "@phosphor-icons/react";
import { useTranslation } from "react-i18next";
import "./VisionObservationCard.css";

export const visionInteractionCopy = (language: string) => language.toLowerCase().startsWith("es") ? {
  label: "Contexto visual", continuous: "Seguimiento continuo", onDemand: "Consulta puntual", older: "Observaciones anteriores",
  uncertainties: "Incertidumbres", defaultQuestion: "¿Qué ves?", wait: "Resuelve la solicitud pendiente del agente antes de hacer otra consulta.",
  clarify: "¿Quieres que mire por la cámara? Usa Mirar ahora para confirmar o precisa tu pregunta.", failed: "No se pudo obtener una observación actual. Revisa los controles de la cámara.",
} : {
  label: "Visual context", continuous: "Continuous observation", onDemand: "Look on request", older: "Earlier observations",
  uncertainties: "Uncertainties", defaultQuestion: "What do you see?", wait: "Resolve the agent's pending request before asking another question.",
  clarify: "Do you want me to look through the camera? Use Look now to confirm, or clarify your question.", failed: "A current observation could not be obtained. Check the camera controls.",
};

export function VisionObservationCard({ observation }: { observation: VisionObservation }) {
  const { i18n } = useTranslation();
  const copy = visionInteractionCopy(i18n.language);
  const date = new Date(observation.capturedAt);
  return <article className="vision-observation" aria-label={copy.label}>
    <header><Eye aria-hidden="true" /><strong>{copy.label}</strong><span>{observation.mode === "continuous" ? copy.continuous : copy.onDemand}</span></header>
    <p>{observation.summary}</p>
    {observation.uncertainties.length > 0 ? <p className="vision-observation__uncertainties">{copy.uncertainties}: {observation.uncertainties.join("; ")}</p> : null}
    <footer><time dateTime={observation.capturedAt}>{Number.isFinite(date.getTime()) ? date.toLocaleString(i18n.language) : observation.capturedAt}</time><span>{observation.modelId}</span></footer>
  </article>;
}
