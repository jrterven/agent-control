import { useEffect, useState } from "react";
import { api } from "./api";
import { useAppStore } from "../store/appStore";

export function useSessionMedia(sessionId: string, mediaId: string, variant?: "thumbnail" | "full") {
  const url = api.sessionMediaUrl(sessionId, mediaId, variant);
  const access = useAppStore((state) => state.sessions.find((session) => session.id === sessionId)?.temporaryAccess);
  const [blob, setBlob] = useState<{ url: string; source: string }>();
  useEffect(() => {
    if (!access) return;
    const controller = new AbortController();
    let objectUrl: string | undefined;
    void fetch(url, { credentials: "same-origin", headers: { "X-Temporary-Chat": access }, signal: controller.signal })
      .then((response) => { if (!response.ok) throw new Error("Media unavailable"); return response.blob(); })
      .then((content) => { if (!controller.signal.aborted) { objectUrl = URL.createObjectURL(content); setBlob({ url: objectUrl, source: url }); } })
      .catch(() => undefined);
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl); };
  }, [url, access]);
  return sessionId.startsWith("tmp_") ? (blob?.source === url ? blob.url : undefined) : url;
}
