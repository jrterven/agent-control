import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent } from "react";
import { useTranslation } from "react-i18next";
import { useMediaQuery } from "../lib/useMediaQuery";
import { fitPanelWidths, maxPanelWidth, PANEL_LIMITS, readPanelWidths, savePanelWidths, type PanelSide, type PanelWidths } from "../lib/panelWidths";

export function useDesktopPanelResize(leftOpen: boolean, rightOpen: boolean) {
  const shellRef = useRef<HTMLDivElement>(null);
  const desktop = useMediaQuery("(min-width: 1200px)");
  const [availableWidth, setAvailableWidth] = useState(() => window.innerWidth);
  const [preferred, setPreferred] = useState(readPanelWidths);
  const [resizing, setResizing] = useState(false);
  const widths = fitPanelWidths(preferred, availableWidth, { left: leftOpen, right: rightOpen });
  const current = useRef({ preferred, widths, availableWidth, desktop, leftOpen, rightOpen });
  current.current = { preferred, widths, availableWidth, desktop, leftOpen, rightOpen };
  const drag = useRef<{ side: PanelSide; x: number; initial: PanelWidths; preferred: PanelWidths; next: PanelWidths; element: HTMLDivElement; pointerId: number } | null>(null);

  useLayoutEffect(() => {
    const shell = shellRef.current;
    if (!shell) return;
    const measure = () => setAvailableWidth(shell.getBoundingClientRect().width || window.innerWidth);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(shell);
    return () => observer.disconnect();
  }, []);

  const finish = (cancel = false) => {
    const active = drag.current;
    if (!active) return;
    drag.current = null;
    setResizing(false);
    if (cancel) setPreferred(active.preferred);
    else savePanelWidths(active.next);
    if (active.element.hasPointerCapture?.(active.pointerId)) active.element.releasePointerCapture(active.pointerId);
  };

  useEffect(() => {
    const move = (event: globalThis.PointerEvent) => {
      const active = drag.current;
      if (!active || event.pointerId !== active.pointerId) return;
      const state = current.current;
      if (!state.desktop || !(active.side === "left" ? state.leftOpen : state.rightOpen)) return;
      const other = active.side === "left" ? "right" : "left";
      const delta = (event.clientX - active.x) * (active.side === "left" ? 1 : -1);
      const width = Math.round(Math.max(PANEL_LIMITS[active.side].min, Math.min(maxPanelWidth(active.side, state.availableWidth, active.initial[other]), active.initial[active.side] + delta)));
      active.next = { ...active.preferred, ...(active.initial[other] ? { [other]: active.initial[other] } : {}), [active.side]: width };
      setPreferred(active.next);
    };
    const end = (event: globalThis.PointerEvent) => {
      if (event.pointerId === drag.current?.pointerId) finish(event.type === "pointercancel");
    };
    const cancel = () => finish(true);
    const escape = (event: globalThis.KeyboardEvent) => { if (event.key === "Escape" && drag.current) { event.preventDefault(); cancel(); } };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    window.addEventListener("blur", cancel);
    window.addEventListener("resize", cancel);
    window.addEventListener("keydown", escape);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      window.removeEventListener("blur", cancel);
      window.removeEventListener("resize", cancel);
      window.removeEventListener("keydown", escape);
      const active = drag.current;
      drag.current = null;
      if (active?.element.hasPointerCapture?.(active.pointerId)) active.element.releasePointerCapture(active.pointerId);
    };
  }, []);

  useEffect(() => { finish(true); }, [desktop, leftOpen, rightOpen]);

  const change = (side: PanelSide, next: number) => {
    const state = current.current;
    const other = side === "left" ? "right" : "left";
    const value = Math.round(Math.max(PANEL_LIMITS[side].min, Math.min(maxPanelWidth(side, state.availableWidth, state.widths[other]), next)));
    const updated = { ...state.preferred, ...(state.widths[other] ? { [other]: state.widths[other] } : {}), [side]: value };
    setPreferred(updated);
    savePanelWidths(updated);
  };

  return {
    shellRef,
    resizing,
    style: { "--sidebar-width": `${widths.left}px`, "--context-width": `${widths.right}px` } as CSSProperties,
    handle: (side: PanelSide) => ({
      side,
      visible: desktop && (side === "left" ? leftOpen : rightOpen),
      width: widths[side],
      max: maxPanelWidth(side, availableWidth, widths[side === "left" ? "right" : "left"]),
      onPointerDown: (event: PointerEvent<HTMLDivElement>) => {
        if (event.button !== 0 || !event.isPrimary || drag.current) return;
        event.preventDefault();
        event.currentTarget.focus();
        event.currentTarget.setPointerCapture(event.pointerId);
        drag.current = { side, x: event.clientX, initial: { ...widths }, preferred: { ...preferred }, next: { ...preferred }, element: event.currentTarget, pointerId: event.pointerId };
        setResizing(true);
      },
      onLostPointerCapture: () => finish(),
      onDoubleClick: () => change(side, PANEL_LIMITS[side].default),
      onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => {
        if (drag.current) return;
        let next: number;
        if (event.key === "Home") next = PANEL_LIMITS[side].min;
        else if (event.key === "End") next = PANEL_LIMITS[side].max;
        else if (event.key === "Enter") next = PANEL_LIMITS[side].default;
        else if (event.key === "ArrowLeft" || event.key === "ArrowRight") next = widths[side] + (event.shiftKey ? 40 : 10) * (event.key === "ArrowRight" ? 1 : -1) * (side === "left" ? 1 : -1);
        else return;
        event.preventDefault();
        change(side, next);
      },
    }),
  };
}

export function PanelResizeHandle({ side, visible, width, max, ...events }: ReturnType<ReturnType<typeof useDesktopPanelResize>["handle"]>) {
  const { t } = useTranslation();
  if (!visible) return null;
  return <>
    <div className={`panel-resize-handle panel-resize-handle--${side}`} role="separator" tabIndex={0}
      aria-orientation="vertical" aria-controls={side === "left" ? "left-sidebar" : "activity-panel"}
      aria-label={t(`nav.resize${side === "left" ? "Sidebar" : "Context"}`)}
      aria-valuemin={PANEL_LIMITS[side].min} aria-valuemax={max} aria-valuenow={width}
      aria-valuetext={t("nav.panelWidth", { width })} aria-describedby={`resize-help-${side}`}
      title={t("nav.resizePanelHelp")} {...events} />
    <span className="sr-only" id={`resize-help-${side}`}>{t("nav.resizePanelHelp")}</span>
  </>;
}
