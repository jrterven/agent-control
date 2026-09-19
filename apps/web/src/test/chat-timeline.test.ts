import { describe, expect, it } from "vitest";
import { conversationTimeline } from "../lib/chatTimeline";
import type { VisionObservation } from "@hermes-control/shared-types";
import type { ChatMessage } from "../types";

const message = (id: string, timestamp?: string): ChatMessage => ({ id, sessionId: "chat", role: "user", content: id, createdAt: "12:01 a. m.", timestamp });
const call = (id: string, createdAt: string) => ({ id, createdAt, fragments: [] });

describe("mixed voice and text history", () => {
  it("places each call before the later text messages across midnight without sorting localized clock labels", () => {
    const messages = [message("before", "2026-09-13T23:58:00-06:00"), message("after", "2026-09-14T00:01:00-06:00"), message("latest", "2026-09-14T00:03:00-06:00")];
    const calls = [call("second", "2026-09-14T06:02:00Z"), call("first", "2026-09-14T05:59:00Z")];
    expect(conversationTimeline(messages, calls).map((item) => item.id)).toEqual(["before", "voice-first", "after", "voice-second", "latest"]);
    expect(calls.map((item) => item.id)).toEqual(["second", "first"]);
  });

  it("preserves undated cached rows and Hermes order while keeping new messages after the voice call", () => {
    const messages = [message("cached"), message("prompt", "2026-09-13T19:12:00Z"), message("tool"), message("answer", "2026-09-13T19:11:59Z")];
    expect(conversationTimeline(messages, [call("voice", "2026-09-13T19:10:00Z")]).map((item) => item.id)).toEqual(["cached", "voice-voice", "prompt", "tool", "answer"]);
  });
  it("interleaves visual evidence without moving undated tool rows or exposing image fields", () => {
    const observations = [{ id: "one", mode: "continuous", capturedAt: "2026-09-13T19:11:00Z", summary: "a book" }] as VisionObservation[];
    const messages = [message("cached"), message("prompt", "2026-09-13T19:12:00Z"), message("tool")];
    const timeline = conversationTimeline(messages, [call("call", "2026-09-13T19:10:00Z")], observations);
    expect(timeline.map((item) => item.id)).toEqual(["cached", "voice-call", "vision-one", "prompt", "tool"]);
    expect(timeline[2]).toEqual({ kind: "vision", id: "vision-one", observation: observations[0] });
  });

  it("keeps on-demand evidence out of the timeline so only the normal agent answer appears", () => {
    const observations = [{ id: "question", mode: "on_demand", capturedAt: "2026-09-13T19:11:00Z", summary: "a book" }] as VisionObservation[];
    const messages = [message("prompt", "2026-09-13T19:10:00Z"), { ...message("answer", "2026-09-13T19:12:00Z"), role: "assistant" as const }];
    expect(conversationTimeline(messages, [], observations).map((item) => item.id)).toEqual(["prompt", "answer"]);
    expect(observations).toHaveLength(1);
    expect(observations[0].summary).toBe("a book");
  });
});
