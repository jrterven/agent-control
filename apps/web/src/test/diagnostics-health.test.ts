import { describe, expect, it } from "vitest";

import { diagnosticIsOperational } from "../screens/Screens";


describe("diagnostic health projection", () => {
  it("requires fresh upstream readiness in addition to live UI transport", () => {
    expect(diagnosticIsOperational(
      "connected",
      "connected",
      { status: "ready", upstream: "online" },
    )).toBe(true);

    expect(diagnosticIsOperational(
      "connected",
      "connected",
      { status: "ready", upstream: "stale" },
    )).toBe(false);
    expect(diagnosticIsOperational(
      "connected",
      "connected",
      { status: "degraded", upstream: "online" },
    )).toBe(false);
    expect(diagnosticIsOperational("connected", "connected", null)).toBe(false);
  });
});
