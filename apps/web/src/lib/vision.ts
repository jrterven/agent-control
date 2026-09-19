import type {
  VisionAnalysisInput, VisionAnalysisResult, VisionIntentInput, VisionIntentResult,
  VisionObservationPage, VisionPreferenceInput, VisionPreferences,
} from "@hermes-control/shared-types";
import { request } from "./api";

const sessionPath = (sessionId: string) => `/sessions/${encodeURIComponent(sessionId)}/vision`;
const csrfHeaders = (csrfToken?: string) => csrfToken ? { "X-CSRF-Token": csrfToken } : undefined;

// No inference retries: a lost response can still represent billed work.
// The shared transport only retries a proven pre-execution CSRF rejection.
export const visionApi = {
  preferences: (signal?: AbortSignal) => request<VisionPreferences>("/vision/preferences", { cache: "no-store", signal }),
  savePreferences: (payload: VisionPreferenceInput, csrfToken?: string, signal?: AbortSignal) => request<VisionPreferences>("/vision/preferences", {
    method: "PUT", cache: "no-store", signal, headers: csrfHeaders(csrfToken), body: JSON.stringify(payload),
  }),
  intent: (sessionId: string, payload: VisionIntentInput, csrfToken?: string, signal?: AbortSignal) => request<VisionIntentResult>(`${sessionPath(sessionId)}/intent`, {
    method: "POST", cache: "no-store", signal, headers: csrfHeaders(csrfToken), body: JSON.stringify(payload),
  }),
  analyze: (sessionId: string, payload: VisionAnalysisInput, csrfToken?: string, signal?: AbortSignal) => request<VisionAnalysisResult>(`${sessionPath(sessionId)}/analyses`, {
    method: "POST", cache: "no-store", signal, headers: csrfHeaders(csrfToken), body: JSON.stringify(payload),
  }),
  observations: (sessionId: string, before?: string, signal?: AbortSignal) => request<VisionObservationPage>(`${sessionPath(sessionId)}/observations${before ? `?before=${encodeURIComponent(before)}` : ""}`, { cache: "no-store", signal }),
};

export const VISION_PREFERENCES_CHANGED = "vision-preferences-changed";
