import { describe, expect, it } from "vitest";
import {
  formatConversationTimestamp,
  parseControlTimestamp,
  zonedDayDistance,
  zonedDayKey,
} from "../lib/dateTime";

describe("conversation date and time", () => {
  it("treats zone-less Hermes timestamps as UTC and shows only minute precision", () => {
    const value = "2026-09-01T01:14:23.872776";

    expect(parseControlTimestamp(value)?.toISOString()).toBe("2026-09-01T01:14:23.872Z");
    const formatted = formatConversationTimestamp(value, "es-MX", "America/Mexico_City");
    expect(formatted).toContain("31/08/2026");
    expect(formatted).toContain("19:14");
    expect(formatted).not.toContain("23.872776");
    expect(formatted).not.toContain("T");
  });

  it("uses the selected time zone on both sides of UTC midnight", () => {
    const value = "2026-09-01T01:14:23Z";

    expect(formatConversationTimestamp(value, "es-MX", "UTC")).toContain("01/09/2026");
    expect(formatConversationTimestamp(value, "es-MX", "America/Mexico_City")).toContain("31/08/2026");
    expect(zonedDayKey(value, "America/Mexico_City")).toBe("2026-08-31");
    expect(zonedDayDistance(value, "America/Mexico_City", new Date("2026-09-01T02:00:00Z"))).toBe(0);
  });

  it("leaves non-date status labels unchanged", () => {
    expect(formatConversationTimestamp("ahora", "es-MX", "America/Mexico_City")).toBe("ahora");
  });
});
