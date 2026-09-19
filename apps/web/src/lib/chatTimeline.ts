import type { VisionObservation } from "@hermes-control/shared-types";
import type { ChatMessage } from "../types";
import type { LiveTranscript } from "./liveTranscripts";

/** Hermes uses epoch seconds; browser messages and voice calls use ISO dates. */
export function absoluteTimestamp(value: unknown): string | undefined {
  const date = typeof value === "number" && Number.isFinite(value)
    ? new Date(value > 10_000_000_000 ? value : value * 1_000)
    : typeof value === "string" && /^\d{4}-\d{2}-\d{2}[T ]/.test(value)
      ? new Date(value) : undefined;
  return date && Number.isFinite(date.getTime()) ? date.toISOString() : undefined;
}

type TimelineItem =
  | { kind: "message"; id: string; message: ChatMessage }
  | { kind: "transcript"; id: string; call: LiveTranscript }
  | { kind: "vision"; id: string; observation: VisionObservation };

/** Insert whole voice calls at their start, preserving Hermes' canonical order. */
export function conversationTimeline(messages: ChatMessage[], calls: LiveTranscript[], observations: VisionObservation[] = []): TimelineItem[] {
  const time = (value: unknown) => {
    const timestamp = absoluteTimestamp(value);
    return timestamp ? Date.parse(timestamp) : Number.NEGATIVE_INFINITY;
  };
  const inserts = [
    ...calls.map((call) => ({ time: time(call.createdAt), item: { kind: "transcript", id: `voice-${call.id}`, call } as TimelineItem })),
    // On-demand observations are evidence for the normal agent answer, not a
    // second answer. Keep previously published continuous comments in history.
    ...observations.filter((observation) => observation.mode === "continuous").map((observation) => ({ time: time(observation.capturedAt), item: { kind: "vision", id: `vision-${observation.id}`, observation } as TimelineItem })),
  ].sort((a, b) => a.time - b.time);
  const result: TimelineItem[] = [];
  let callIndex = 0;
  let messageTime = Number.NEGATIVE_INFINITY;
  for (const message of messages) {
    // Older cached messages can have only a localized clock label. Keep their
    // relative position; never parse that label as a date or move a tool row.
    messageTime = Math.max(messageTime, time(message.timestamp ?? message.createdAt));
    while (callIndex < inserts.length && inserts[callIndex].time <= messageTime) {
      result.push(inserts[callIndex++].item);
    }
    result.push({ kind: "message", id: message.id, message });
  }
  for (const insert of inserts.slice(callIndex)) result.push(insert.item);
  return result;
}
