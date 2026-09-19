export const VISION_MODEL_IDS = ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"] as const;
export const VISION_INTERVAL_SECONDS = [2, 5, 10] as const;
export type VisionModelId = typeof VISION_MODEL_IDS[number];
export type VisionIntervalSeconds = typeof VISION_INTERVAL_SECONDS[number];
export type VisionCameraPhase = "idle" | "starting" | "active" | "paused";
export type VisionMode = "on_demand" | "continuous";

export interface VisionPreferenceInput {
  modelId: VisionModelId;
  intervalSeconds: VisionIntervalSeconds;
}

export interface VisionPreferences extends VisionPreferenceInput {
  configured: boolean;
}

export interface VisionIntentInput {
  requestId: string;
  text: string;
  recentContext?: string;
}

export interface VisionIntentResult {
  intent: "visual" | "nonvisual" | "unclear";
  question: string;
}

export interface VisionAnalysisInput {
  requestId: string;
  activationId: string;
  mode: VisionMode;
  capturedAt: string;
  image: string;
  previousImage?: string;
  question?: string;
  recentContext?: string;
}

export interface VisionObservation {
  id: string;
  sessionId: string;
  activationId: string;
  capturedAt: string;
  createdAt: string;
  modelId: VisionModelId;
  mode: VisionMode;
  summary: string;
  meaningfulChange: boolean;
  sceneReset: boolean;
  uncertainties: string[];
}

export interface VisionAnalysisResult {
  observation: VisionObservation;
  published: boolean;
}

export interface VisionObservationPage {
  items: VisionObservation[];
  nextCursor: string | null;
}
