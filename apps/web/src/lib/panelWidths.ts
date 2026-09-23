export type PanelSide = "left" | "right";
export type PanelWidths = Record<PanelSide, number>;
export const PANEL_LIMITS = {
  left: { min: 240, max: 440, default: 282 },
  right: { min: 280, max: 480, default: 320 },
} as const;
export const MIN_CHAT_WIDTH = 480;
export const PANEL_WIDTHS_KEY = "agent-control.desktop-panel-widths";

export function clampPanelWidth(side: PanelSide, value: unknown): number {
  const limits = PANEL_LIMITS[side];
  return typeof value === "number" && Number.isFinite(value)
    ? Math.round(Math.max(limits.min, Math.min(limits.max, value)))
    : limits.default;
}

export function readPanelWidths(): PanelWidths {
  try {
    const value = JSON.parse(localStorage.getItem(PANEL_WIDTHS_KEY) ?? "null");
    return { left: clampPanelWidth("left", value?.left), right: clampPanelWidth("right", value?.right) };
  } catch {
    return { left: PANEL_LIMITS.left.default, right: PANEL_LIMITS.right.default };
  }
}

export function savePanelWidths(widths: PanelWidths) {
  try {
    localStorage.setItem(PANEL_WIDTHS_KEY, JSON.stringify(widths));
  } catch {
    // Resizing remains available when local preferences cannot be stored.
  }
}

/** Fit visible panels without replacing the preferred widths on window resize. */
export function fitPanelWidths(preferred: PanelWidths, availableWidth: number, open: Record<PanelSide, boolean>): PanelWidths {
  const left = open.left ? clampPanelWidth("left", preferred.left) : 0;
  const right = open.right ? clampPanelWidth("right", preferred.right) : 0;
  const leftMin = open.left ? PANEL_LIMITS.left.min : 0;
  const rightMin = open.right ? PANEL_LIMITS.right.min : 0;
  const budget = Math.max(leftMin + rightMin, availableWidth - MIN_CHAT_WIDTH);
  if (left + right <= budget) return { left, right };
  const extra = budget - leftMin - rightMin;
  const leftExtra = Math.floor(extra * (left - leftMin) / (left + right - leftMin - rightMin));
  return { left: leftMin + leftExtra, right: rightMin + Math.floor(extra - leftExtra) };
}

export function maxPanelWidth(side: PanelSide, availableWidth: number, otherWidth: number) {
  return Math.max(PANEL_LIMITS[side].min, Math.min(PANEL_LIMITS[side].max, Math.floor(availableWidth - MIN_CHAT_WIDTH - otherWidth)));
}
