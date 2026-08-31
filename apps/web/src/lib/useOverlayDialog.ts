import { useEffect, useRef, useState, type RefObject } from "react";
import { subscribeToMediaQuery } from "./mediaQuery";

const focusableSelector = [
  "a[href]",
  "button:not([disabled]):not([tabindex='-1'])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "summary",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

type OverlayDialogOptions = {
  open: boolean;
  onClose: () => void;
  mediaQuery: string;
};

type OverlayDialogResult<T extends HTMLElement> = {
  containerRef: RefObject<T | null>;
  isOverlay: boolean;
  active: boolean;
};

function queryMatches(mediaQuery: string) {
  return typeof window !== "undefined" && Boolean(window.matchMedia?.(mediaQuery)?.matches);
}

export function useOverlayDialog<T extends HTMLElement>({ open, onClose, mediaQuery }: OverlayDialogOptions): OverlayDialogResult<T> {
  const containerRef = useRef<T>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  const [isOverlay, setIsOverlay] = useState(() => queryMatches(mediaQuery));
  const active = isOverlay && open;

  useEffect(() => { onCloseRef.current = onClose; }, [onClose]);

  useEffect(() => {
    const media = window.matchMedia(mediaQuery);
    if (!media) return;
    const update = () => setIsOverlay(media.matches);
    update();
    return subscribeToMediaQuery(media, update);
  }, [mediaQuery]);

  useEffect(() => {
    if (!active) return;
    const container = containerRef.current;
    if (!container) return;
    returnFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;

    const focusable = () => Array.from(container.querySelectorAll<HTMLElement>(focusableSelector))
      .filter((element) => !element.hidden && element.getAttribute("aria-hidden") !== "true");
    (focusable()[0] ?? container).focus();

    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      const returnTarget = returnFocusRef.current;
      returnFocusRef.current = null;
      if (returnTarget?.isConnected) returnTarget.focus();
    };
  }, [active]);

  return { containerRef, isOverlay, active };
}
