import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createElement } from "react";
import i18n, {
  DEFAULT_LANGUAGE,
  getCurrentLanguage,
  initializeLanguagePreference,
  LANGUAGE_PREFERENCE_KEY,
  normalizeSupportedLanguage,
  setLanguagePreference,
} from "../i18n";
import { db, loadPreference, savePreference } from "../lib/db";
import { useLanguagePreference } from "../hooks/useLanguagePreference";
import { useThemePreference } from "../hooks";
import { SettingsScreen } from "../screens/Screens";
import { TIME_ZONE_PREFERENCE_KEY } from "../lib/dateTime";
import { useAppStore } from "../store/appStore";

describe("language preferences", () => {
  const preferenceMocks: { mockRestore(): void }[] = [];
  const mockBrowserLanguage = (language: string) => {
    preferenceMocks.push(
      vi.spyOn(navigator, "languages", "get").mockReturnValue([language]),
      vi.spyOn(navigator, "language", "get").mockReturnValue(language),
    );
  };

  beforeEach(async () => {
    await db.preferences.delete(LANGUAGE_PREFERENCE_KEY);
    await db.preferences.delete(TIME_ZONE_PREFERENCE_KEY);
    await i18n.changeLanguage(DEFAULT_LANGUAGE);
    document.documentElement.lang = DEFAULT_LANGUAGE;
    useAppStore.setState({ timeZone: "America/Mexico_City" });
  });

  afterEach(async () => {
    preferenceMocks.splice(0).forEach((mock) => mock.mockRestore());
    await db.preferences.delete(LANGUAGE_PREFERENCE_KEY);
    await db.preferences.delete(TIME_ZONE_PREFERENCE_KEY);
    await i18n.changeLanguage(DEFAULT_LANGUAGE);
    document.documentElement.lang = DEFAULT_LANGUAGE;
  });

  it("normalizes supported regional language tags", () => {
    expect(normalizeSupportedLanguage("pt-BR")).toBe("pt");
    expect(normalizeSupportedLanguage("DE_de")).toBe("de");
    expect(normalizeSupportedLanguage("ja-JP")).toBeUndefined();
  });

  it("applies and persists an explicit language choice without localStorage", async () => {
    await setLanguagePreference("de");

    expect(getCurrentLanguage()).toBe("de");
    expect(document.documentElement.lang).toBe("de");
    expect(await loadPreference(LANGUAGE_PREFERENCE_KEY)).toBe("de");
  });

  it("hydrates an explicit saved language independently of the browser language", async () => {
    mockBrowserLanguage("fr-FR");
    await savePreference(LANGUAGE_PREFERENCE_KEY, "pt");

    await expect(initializeLanguagePreference()).resolves.toBe("pt");
    expect(getCurrentLanguage()).toBe("pt");
    expect(document.documentElement.lang).toBe("pt");
  });

  it.each(["es-MX", "fr-FR", "ja-JP"])("starts in English without a saved preference for a %s browser", async (language) => {
    mockBrowserLanguage(language);

    expect(DEFAULT_LANGUAGE).toBe("en");
    await expect(initializeLanguagePreference()).resolves.toBe("en");
    expect(getCurrentLanguage()).toBe("en");
    expect(document.documentElement.lang).toBe("en");
    expect(await loadPreference(LANGUAGE_PREFERENCE_KEY)).toBeUndefined();
  });

  it("falls back to English when the saved preference is unsupported", async () => {
    await savePreference(LANGUAGE_PREFERENCE_KEY, "ja-JP");
    mockBrowserLanguage("es-MX");

    await expect(initializeLanguagePreference()).resolves.toBe("en");
    expect(getCurrentLanguage()).toBe("en");
    expect(document.documentElement.lang).toBe("en");
  });

  it("falls back to English when preference storage is unavailable", async () => {
    preferenceMocks.push(vi.spyOn(db.preferences, "get").mockRejectedValueOnce(new Error("Storage unavailable")));
    mockBrowserLanguage("fr-FR");

    await expect(initializeLanguagePreference()).resolves.toBe("en");
    expect(getCurrentLanguage()).toBe("en");
    expect(document.documentElement.lang).toBe("en");
  });

  it("exposes language changes to preferences UI consumers", async () => {
    const { result } = renderHook(() => useLanguagePreference());

    await act(async () => { await result.current.changeLanguage("es"); });

    expect(result.current.language).toBe("es");
    expect(result.current.languageOptions.map(({ code }) => code)).toEqual(["en", "es", "fr", "de", "pt"]);
  });

  it("changes the complete interface language from Preferences", async () => {
    await setLanguagePreference("es");
    const user = userEvent.setup();
    render(createElement(SettingsScreen));

    await user.selectOptions(screen.getByRole("combobox", { name: "Idioma de la interfaz" }), "fr");

    await waitFor(() => expect(screen.getByRole("heading", { name: "Paramètres" })).toBeVisible());
    expect(screen.getByRole("combobox", { name: "Langue de l’interface" })).toHaveValue("fr");
    expect(document.documentElement.lang).toBe("fr");
    expect(await loadPreference(LANGUAGE_PREFERENCE_KEY)).toBe("fr");
  });

  it("edits and persists the user time zone from Preferences", async () => {
    await setLanguagePreference("es");
    const user = userEvent.setup();
    render(createElement(SettingsScreen));

    await user.selectOptions(screen.getByRole("combobox", { name: "Zona horaria del usuario" }), "America/New_York");

    expect(useAppStore.getState().timeZone).toBe("America/New_York");
    await waitFor(() => expect(loadPreference(TIME_ZONE_PREFERENCE_KEY)).resolves.toBe("America/New_York"));
  });

  it("restores the saved time zone when the app starts again", async () => {
    await savePreference(TIME_ZONE_PREFERENCE_KEY, "America/New_York");
    useAppStore.setState({ timeZone: "UTC" });

    renderHook(() => useThemePreference());

    await waitFor(() => expect(useAppStore.getState().timeZone).toBe("America/New_York"));
  });
});
