import type { LiveFragment } from "./openaiLiveClient";

export type TranscriptSaveState = "saving" | "saved" | "failed";
export type LiveTranscript = { id: string; createdAt: string; fragments: LiveFragment[]; saveState?: TranscriptSaveState };
export type LiveTranscriptPage = { items: LiveTranscript[]; nextCursor: string | null };

/** Display groups are revisable captions, never completed turns or task triggers. */
export function groupLiveFragments(fragments: LiveFragment[]) {
  const rows: { id: string; role: LiveFragment["role"]; text: string; start: number; end: number; order: number }[] = [];
  const userStarts = fragments.filter((part) => part.role === "user").map((part) => part.start).sort((a, b) => a - b);
  for (const role of ["user", "assistant"] as const) {
    let previous: typeof rows[number] | undefined;
    let userIndex = 0;
    for (const part of fragments.filter((item) => item.role === role).sort((a, b) => a.start - b.start || a.order - b.order)) {
      while (previous && userIndex < userStarts.length && userStarts[userIndex] < previous.end) userIndex++;
      const interrupted = role === "assistant" && previous && userStarts[userIndex] < part.start;
      if (previous && part.start - previous.end <= 1200 && !interrupted) {
        previous.text += part.text;
        previous.end = Math.max(previous.end, part.end);
        previous.order = Math.min(previous.order, part.order);
        previous.id = `${role}-${previous.order}`;
      } else {
        previous = { id: `${role}-${part.order}`, ...part };
        rows.push(previous);
      }
    }
  }
  return rows.sort((a, b) => a.start - b.start || a.order - b.order);
}

/** Batch only new fragments outside the media callback; saves never block audio. */
export class LiveTranscriptWriter {
  private fragments: LiveFragment[] = [];
  private saved = 0;
  private inFlight = false;
  private timer?: ReturnType<typeof setTimeout>;
  private attempts = 0;

  constructor(private save: (fragments: LiveFragment[], offset: number) => Promise<void>, private status: (state: TranscriptSaveState) => void) {}

  append(fragments: LiveFragment[]) {
    this.fragments = fragments;
    this.status("saving");
    if (!this.timer && !this.inFlight) this.timer = setTimeout(() => { this.timer = undefined; void this.flush(); }, 1000);
  }

  async flush() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = undefined;
    if (this.inFlight || this.saved >= this.fragments.length) return;
    this.inFlight = true;
    const snapshot = this.fragments;
    this.status("saving");
    try {
      await this.save(snapshot.slice(this.saved), this.saved);
      this.saved = snapshot.length;
      this.attempts = 0;
      this.status(this.saved === this.fragments.length ? "saved" : "saving");
    } catch {
      this.attempts += 1;
      this.status("failed");
    } finally {
      this.inFlight = false;
      if (this.saved < this.fragments.length && this.attempts < 3) {
        this.timer = setTimeout(() => { this.timer = undefined; void this.flush(); }, this.attempts ? this.attempts * 2000 : 1000);
      }
    }
  }

  retry() { this.attempts = 0; void this.flush(); }
}
