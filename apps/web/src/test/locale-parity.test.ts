import { describe, expect, it } from "vitest";
import { adminResources } from "../locales/admin";
import { baseTranslations } from "../locales/base";
import { chatResources } from "../locales/chat";
import { navigationResources } from "../locales/navigation";
import { runtimeResources } from "../locales/runtime";
import { screenResources } from "../locales/screens";
import { dictationResources } from "../locales/dictation";
import { integrationResources } from "../locales/integrations";
import { speechResources } from "../locales/speech";
import { updateResources } from "../locales/updates";

function leaves(value: unknown, prefix = ""): string[] {
  if (typeof value === "string") {
    expect(value.trim(), `empty translation at ${prefix}`).not.toBe("");
    return [prefix];
  }
  expect(value, `invalid translation node at ${prefix}`).toBeTypeOf("object");
  return Object.entries(value as Record<string, unknown>)
    .flatMap(([key, child]) => leaves(child, prefix ? `${prefix}.${key}` : key));
}

describe("locale catalogs", () => {
  it("keeps identical, non-empty key sets in every supported language", () => {
    const catalogs = [baseTranslations, navigationResources, chatResources, adminResources, dictationResources, integrationResources, speechResources, screenResources, runtimeResources, updateResources];
    for (const catalog of catalogs) {
      const reference = leaves(catalog.es).sort();
      for (const language of ["en", "fr", "de", "pt"] as const) {
        expect(leaves(catalog[language]).sort()).toEqual(reference);
      }
    }
  });
});
