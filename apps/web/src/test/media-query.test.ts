import { describe, expect, it, vi } from "vitest";
import { subscribeToMediaQuery } from "../lib/mediaQuery";

function mediaQueryList(overrides: Partial<MediaQueryList>): MediaQueryList {
  return {
    matches: false,
    media: "(max-width: 700px)",
    onchange: null,
    addEventListener: undefined,
    removeEventListener: undefined,
    addListener: undefined,
    removeListener: undefined,
    dispatchEvent: vi.fn(),
    ...overrides,
  } as unknown as MediaQueryList;
}

describe("MediaQueryList compatibility", () => {
  it("uses and removes the modern change listener when available", () => {
    const addEventListener = vi.fn();
    const removeEventListener = vi.fn();
    const addListener = vi.fn();
    const removeListener = vi.fn();
    const listener = vi.fn();
    const media = mediaQueryList({ addEventListener, removeEventListener, addListener, removeListener });

    const unsubscribe = subscribeToMediaQuery(media, listener);

    expect(addEventListener).toHaveBeenCalledWith("change", listener);
    expect(addListener).not.toHaveBeenCalled();
    unsubscribe();
    expect(removeEventListener).toHaveBeenCalledWith("change", listener);
    expect(removeListener).not.toHaveBeenCalled();
  });

  it("falls back to the legacy listener pair used by older WebViews", () => {
    const addListener = vi.fn();
    const removeListener = vi.fn();
    const listener = vi.fn();
    const media = mediaQueryList({ addListener, removeListener });

    const unsubscribe = subscribeToMediaQuery(media, listener);

    expect(addListener).toHaveBeenCalledWith(listener);
    unsubscribe();
    expect(removeListener).toHaveBeenCalledWith(listener);
  });

  it("remains a no-op when no complete listener API is available", () => {
    const listener = vi.fn();
    const media = mediaQueryList({ addEventListener: vi.fn() });

    const unsubscribe = subscribeToMediaQuery(media, listener);

    expect(unsubscribe).not.toThrow();
    expect(listener).not.toHaveBeenCalled();
  });
});
