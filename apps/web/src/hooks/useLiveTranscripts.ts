import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../lib/api";
import { LiveTranscriptWriter, type LiveTranscript } from "../lib/liveTranscripts";
import type { LiveFragment } from "../lib/openaiLiveClient";

export function useLiveTranscripts(sessionId: string, csrfToken: string | undefined, authenticated: boolean) {
  const scope = `${sessionId}:${csrfToken ?? ""}:${authenticated}`;
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const [history, setHistory] = useState<{ scope: string; calls: LiveTranscript[]; next?: string | null; loading: boolean; error: boolean }>({ scope, calls: [], loading: false, error: false });
  const writers = useRef(new Map<string, LiveTranscriptWriter>());

  const load = useCallback(async (before?: string, signal?: AbortSignal) => {
    if (!sessionId || !authenticated) return;
    setHistory((current) => ({ ...current, loading: true, error: false }));
    try {
      const page = await api.liveTranscripts(sessionId, before, signal);
      if (signal?.aborted || scopeRef.current !== scope) return;
      setHistory((current) => {
        const calls = new Map(page.items.map((call) => [call.id, call]));
        // Local snapshots may be newer than a concurrent history response.
        current.calls.forEach((call) => calls.set(call.id, call));
        return { scope, calls: [...calls.values()].sort((a, b) => a.createdAt.localeCompare(b.createdAt)), next: page.nextCursor, loading: false, error: false };
      });
    } catch {
      if (!signal?.aborted && scopeRef.current === scope) setHistory((current) => ({ ...current, loading: false, error: true }));
    }
  }, [authenticated, scope, sessionId]);

  useEffect(() => {
    const controller = new AbortController();
    const currentWriters = writers.current = new Map();
    setHistory({ scope, calls: [], loading: authenticated && Boolean(sessionId), error: false });
    void load(undefined, controller.signal);
    return () => {
      controller.abort();
      // Flush captured text to its original owned conversation, never the newly selected one.
      currentWriters.forEach((writer) => void writer.flush());
    };
  }, [scope, load, authenticated, sessionId]);

  const begin = () => {
    const id = crypto.randomUUID();
    const createdAt = new Date().toISOString();
    const writer = new LiveTranscriptWriter(
      (fragments, offset) => api.saveLiveTranscript(sessionId, id, fragments, csrfToken, AbortSignal.timeout(15_000), offset),
      (saveState) => { if (scopeRef.current === scope) setHistory((current) => ({ ...current, calls: current.calls.map((call) => call.id === id ? { ...call, saveState } : call) })); },
    );
    writers.current.set(id, writer);
    return {
      append: (fragments: LiveFragment[]) => {
        if (scopeRef.current !== scope || !fragments.length) return;
        setHistory((current) => ({ ...current, calls: [...current.calls.filter((call) => call.id !== id), { id, createdAt, fragments, saveState: "saving" }] }));
        writer.append(fragments);
      },
      flush: () => { void writer.flush(); },
    };
  };
  return {
    calls: history.scope === scope ? history.calls : [],
    loading: history.scope === scope && history.loading,
    historyError: history.scope === scope && history.error,
    hasMore: history.scope === scope && Boolean(history.next),
    loadMore: () => void load(history.next ?? undefined),
    retrySave: (id: string) => writers.current.get(id)?.retry(),
    begin,
  };
}
