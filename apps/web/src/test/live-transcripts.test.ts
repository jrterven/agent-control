import { afterEach, describe, expect, it, vi } from "vitest";
import { groupLiveFragments, LiveTranscriptWriter } from "../lib/liveTranscripts";
import type { LiveFragment } from "../lib/openaiLiveClient";

const part = (text: string, order: number, start: number, role: "user" | "assistant" = "user"): LiveFragment => ({ text, order, start, end: start + 100, role });

afterEach(() => vi.useRealTimers());

describe("passive Live transcripts", () => {
  it("preserves exact text with overlaps, late fragments, interrupted speech and long pauses", () => {
    const fragments = [part("Quiero", 0, 100), part(" Sí.", 1, 250, "assistant"), part("dos cosas", 2, 500), part(" una, ", 3, 300), part(" La primera.", 4, 2000, "assistant"), part("Espera", 5, 2200), part(" Claro.", 6, 2500, "assistant"), part("Otra idea", 7, 8000)];
    const rows = groupLiveFragments(fragments);
    expect(rows.map((row) => [row.role, row.text])).toEqual([
      ["user", "Quiero una, dos cosas"], ["assistant", " Sí."], ["assistant", " La primera."],
      ["user", "Espera"], ["assistant", " Claro."], ["user", "Otra idea"],
    ]);
    expect(rows[0].id).toBe("user-0");
    expect(fragments[2].text).toBe("dos cosas");
  });

  it("renders immediately while coalescing saves and never waits on the audio path", async () => {
    vi.useFakeTimers();
    let finish!: () => void;
    const save = vi.fn().mockImplementationOnce(() => new Promise<void>((resolve) => { finish = resolve; })).mockResolvedValue(undefined);
    const status = vi.fn();
    const writer = new LiveTranscriptWriter(save, status);
    writer.append([part("Hola", 0, 100)]);
    expect(save).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1000);
    const latest = [part("Hola", 0, 100), part(" mundo", 1, 300)];
    writer.append(latest);
    await vi.advanceTimersByTimeAsync(5000);
    expect(save).toHaveBeenCalledTimes(1);
    finish();
    await vi.advanceTimersByTimeAsync(1000);
    expect(save).toHaveBeenLastCalledWith(latest.slice(1), 1);
    expect(status).toHaveBeenLastCalledWith("saved");
  });

  it("flushes at stop, retries failures safely and preserves the full snapshot", async () => {
    vi.useFakeTimers();
    const save = vi.fn().mockRejectedValue(new Error("offline"));
    const status = vi.fn();
    const writer = new LiveTranscriptWriter(save, status);
    const fragments = [part("No perder esto", 0, 100)];
    writer.append(fragments);
    await writer.flush();
    expect(save).toHaveBeenCalledTimes(1);
    await vi.runAllTimersAsync();
    expect(save).toHaveBeenCalledTimes(3);
    expect(status).toHaveBeenLastCalledWith("failed");
    save.mockResolvedValue(undefined);
    writer.retry();
    await vi.runAllTimersAsync();
    expect(save).toHaveBeenLastCalledWith(fragments, 0);
    expect(status).toHaveBeenLastCalledWith("saved");
  });
});
