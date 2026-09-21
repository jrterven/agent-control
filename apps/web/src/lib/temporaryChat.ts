import { useEffect } from "react";
import type { SessionSummary } from "../types";
import { useAppStore } from "../store/appStore";

const pendingClose = new Map<string, ReturnType<typeof setTimeout>>();

export async function closeTemporaryChat(session: SessionSummary, csrfToken?: string) {
  if (!session.temporaryAccess) return;
  await fetch(`/api/v1/sessions/${session.id}/temporary/close`, {
    method: "POST", credentials: "same-origin", keepalive: true,
    headers: { "X-Temporary-Chat": session.temporaryAccess, "X-CSRF-Token": csrfToken ?? "" },
  }).catch(() => undefined); // The independent server/runtime leases expire offline chats.
}

/** A private chat belongs only to this mounted conversation in this tab. */
export function useTemporaryChat(session?: SessionSummary) {
  const token = useAppStore((state) => state.csrfToken);
  useEffect(() => {
    if (session?.chatMode !== "temporary") return;
    const { id } = session;
    clearTimeout(pendingClose.get(id));
    pendingClose.delete(id);
    let ended = false;
    let acknowledgedAt = Date.now();
    let renewing = false;
    const close = () => {
      if (ended) return;
      ended = true;
      void closeTemporaryChat(session, token);
      useAppStore.getState().removeSession(id);
    };
    const renew = async () => {
      if (ended || renewing || !session.temporaryAccess) return;
      if (Date.now() - acknowledgedAt >= 300_000) { close(); return; }
      renewing = true;
      try {
        const result = await fetch(`/api/v1/sessions/${id}/temporary/renew`, {
          method: "POST", credentials: "same-origin", signal: AbortSignal.timeout(15_000),
          headers: { "X-Temporary-Chat": session.temporaryAccess, "X-CSRF-Token": token ?? "" },
        });
        if (result.ok) acknowledgedAt = Date.now();
        else if ([401, 403, 404, 409, 410].includes(result.status)) close();
      } catch { /* A reconnect is allowed only within the existing lease. */ }
      finally { renewing = false; }
    };
    const timer = setInterval(() => void renew(), 30_000);
    window.addEventListener("pagehide", close);
    return () => {
      clearInterval(timer);
      window.removeEventListener("pagehide", close);
      // React StrictMode immediately remounts effects; a real departure doesn't.
      pendingClose.set(id, setTimeout(() => { pendingClose.delete(id); close(); }, 0));
    };
  }, [session?.id, session?.temporaryAccess, token]);
}
