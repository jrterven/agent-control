import { describe, expect, it } from "vitest";
import { navigationResources } from "../locales/navigation";

function flattenKeys(value: object, prefix = ""): string[] {
  return Object.entries(value).flatMap(([key, child]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return typeof child === "object" && child !== null ? flattenKeys(child, path) : [path];
  }).sort();
}

describe("navigation translations", () => {
  it("keeps every navigation key available in all supported languages", () => {
    const expectedKeys = flattenKeys(navigationResources.es);

    for (const messages of Object.values(navigationResources)) {
      expect(flattenKeys(messages)).toEqual(expectedKeys);
      expect(Object.values(messages).every((section) => (
        Object.values(section).every((message) => typeof message === "string" && message.length > 0)
      ))).toBe(true);
    }
  });
});
