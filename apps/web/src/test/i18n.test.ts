import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { createElement } from "react";
import i18n, {
  DEFAULT_LANGUAGE,
  detectSupportedLanguage,
  getCurrentLanguage,
  initializeLanguagePreference,
  LANGUAGE_PREFERENCE_KEY,
  normalizeSupportedLanguage,
  setLanguagePreference,
} from "../i18n";
import { db, loadPreference, savePreference } from "../lib/db";
import { useLanguagePreference } from "../hooks/useLanguagePreference";
import { SettingsScreen } from "../screens/Screens";

describe("language preferences", () => {
  beforeEach(async () => {
    await db.preferences.delete(LANGUAGE_PREFERENCE_KEY);
    await i18n.changeLanguage(DEFAULT_LANGUAGE);
    document.documentElement.lang = DEFAULT_LANGUAGE;
  });

  afterEach(async () => {
    await db.preferences.delete(LANGUAGE_PREFERENCE_KEY);
    await i18n.changeLanguage(DEFAULT_LANGUAGE);
    document.documentElement.lang = DEFAULT_LANGUAGE;
  });

  it("normalizes supported regional language tags", () => {
    expect(normalizeSupportedLanguage("pt-BR")).toBe("pt");
    expect(normalizeSupportedLanguage("DE_de")).toBe("de");
    expect(normalizeSupportedLanguage("ja-JP")).toBeUndefined();
  });

  it("detects the first supported browser language and falls back to Spanish", () => {
    expect(detectSupportedLanguage(["ja-JP", "fr-CA", "en-US"])).toBe("fr");
    expect(detectSupportedLanguage(["ja-JP", "ko-KR"])).toBe("es");
  });

  it("applies and persists an explicit language choice without localStorage", async () => {
    await setLanguagePreference("de");

    expect(getCurrentLanguage()).toBe("de");
    expect(document.documentElement.lang).toBe("de");
    expect(await loadPreference(LANGUAGE_PREFERENCE_KEY)).toBe("de");
  });

  it("hydrates a saved language before considering browser detection", async () => {
    await savePreference(LANGUAGE_PREFERENCE_KEY, "pt");

    await expect(initializeLanguagePreference(["fr-FR"])).resolves.toBe("pt");
    expect(getCurrentLanguage()).toBe("pt");
    expect(document.documentElement.lang).toBe("pt");
  });

  it("uses browser detection when no saved preference exists", async () => {
    await expect(initializeLanguagePreference(["fr-FR"])).resolves.toBe("fr");
    expect(getCurrentLanguage()).toBe("fr");
    expect(document.documentElement.lang).toBe("fr");
  });

  it("exposes language changes to preferences UI consumers", async () => {
    const { result } = renderHook(() => useLanguagePreference());

    await act(async () => { await result.current.changeLanguage("en"); });

    expect(result.current.language).toBe("en");
    expect(result.current.languageOptions.map(({ code }) => code)).toEqual(["en", "es", "fr", "de", "pt"]);
  });

  it("changes the complete interface language from Preferences", async () => {
    const user = userEvent.setup();
    render(createElement(SettingsScreen));

    await user.selectOptions(screen.getByRole("combobox", { name: "Idioma de la interfaz" }), "fr");

    await waitFor(() => expect(screen.getByRole("heading", { name: "Paramètres" })).toBeVisible());
    expect(screen.getByRole("combobox", { name: "Langue de l’interface" })).toHaveValue("fr");
    expect(document.documentElement.lang).toBe("fr");
    expect(await loadPreference(LANGUAGE_PREFERENCE_KEY)).toBe("fr");
  });
});
