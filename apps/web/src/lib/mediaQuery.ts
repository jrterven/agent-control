/**
 * Subscribes to MediaQueryList changes across modern browsers and older
 * WebViews that only expose the deprecated listener API.
 */
export function subscribeToMediaQuery(media: MediaQueryList, listener: () => void): () => void {
  if (
    typeof media.addEventListener === "function"
    && typeof media.removeEventListener === "function"
  ) {
    media.addEventListener("change", listener);
    return () => media.removeEventListener("change", listener);
  }

  if (typeof media.addListener === "function" && typeof media.removeListener === "function") {
    media.addListener(listener);
    return () => media.removeListener(listener);
  }

  return () => undefined;
}
