import { describe, expect, it } from "vitest";
import { conversationTimeline } from "../lib/chatTimeline";
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
});
