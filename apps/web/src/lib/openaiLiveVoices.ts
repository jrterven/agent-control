// Built-in GPT-Live voice names; custom voice IDs are intentionally excluded.
// https://developers.openai.com/api/reference/resources/live/methods/create
// https://developers.openai.com/api/docs/guides/live-conversations#voice-options
export const OPENAI_LIVE_VOICES = [
  { id: "marin", name: "Marin" },
  { id: "cedar", name: "Cedar" },
  { id: "alloy", name: "Alloy" },
  { id: "ash", name: "Ash" },
  { id: "ballad", name: "Ballad" },
  { id: "beacon", name: "Beacon" },
  { id: "bossa", name: "Bossa" },
  { id: "cinder", name: "Cinder" },
  { id: "coral", name: "Coral" },
  { id: "delta", name: "Delta" },
  { id: "echo", name: "Echo" },
  { id: "gleam", name: "Gleam" },
  { id: "meridian", name: "Meridian" },
  { id: "quartz", name: "Quartz" },
  { id: "ripple", name: "Ripple" },
  { id: "sage", name: "Sage" },
  { id: "shimmer", name: "Shimmer" },
  { id: "stone", name: "Stone" },
  { id: "tempo", name: "Tempo" },
  { id: "verse", name: "Verse" },
  { id: "vesper", name: "Vesper" },
  { id: "willow", name: "Willow" },
] as const;

export type OpenAILiveVoiceId = typeof OPENAI_LIVE_VOICES[number]["id"];
