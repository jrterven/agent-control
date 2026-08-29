import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import { loadPreference, savePreference } from "./lib/db";
import { baseTranslations } from "./locales/base";
import { adminResources } from "./locales/admin";
import { chatResources } from "./locales/chat";
import { dictationResources } from "./locales/dictation";
import { integrationResources } from "./locales/integrations";
import { navigationResources } from "./locales/navigation";
import { screenResources } from "./locales/screens";
import { runtimeResources } from "./locales/runtime";

export const SUPPORTED_LANGUAGES = ["en", "es", "fr", "de", "pt"] as const;
export type SupportedLanguage = (typeof SUPPORTED_LANGUAGES)[number];

export const DEFAULT_LANGUAGE: SupportedLanguage = "es";
export const LANGUAGE_PREFERENCE_KEY = "language";

export const LANGUAGE_OPTIONS: ReadonlyArray<{
  code: SupportedLanguage;
  nativeName: string;
}> = [
  { code: "en", nativeName: "English" },
  { code: "es", nativeName: "Español" },
  { code: "fr", nativeName: "Français" },
  { code: "de", nativeName: "Deutsch" },
  { code: "pt", nativeName: "Português" },
];

const supportedLanguageSet = new Set<string>(SUPPORTED_LANGUAGES);
const asResource = (value: unknown): Record<string, unknown> => value as Record<string, unknown>;

export function normalizeSupportedLanguage(value: string | null | undefined): SupportedLanguage | undefined {
  if (!value) return undefined;
  const baseLanguage = value.trim().toLowerCase().replace("_", "-").split("-")[0];
  return supportedLanguageSet.has(baseLanguage) ? baseLanguage as SupportedLanguage : undefined;
}

export function detectSupportedLanguage(languages?: readonly string[]): SupportedLanguage {
  const browserLanguages = languages ?? (
    typeof navigator === "undefined"
      ? []
      : navigator.languages.length
        ? navigator.languages
        : [navigator.language]
  );

  for (const language of browserLanguages) {
    const supported = normalizeSupportedLanguage(language);
    if (supported) return supported;
  }
  return DEFAULT_LANGUAGE;
}

function applyDocumentLanguage(language: SupportedLanguage) {
  if (typeof document !== "undefined") document.documentElement.lang = language;
}

const i18nInitialization = i18n.use(initReactI18next).init({
  lng: DEFAULT_LANGUAGE,
  fallbackLng: DEFAULT_LANGUAGE,
  supportedLngs: [...SUPPORTED_LANGUAGES],
  nonExplicitSupportedLngs: true,
  load: "languageOnly",
  interpolation: { escapeValue: false },
  resources: Object.fromEntries(
    SUPPORTED_LANGUAGES.map((language) => [language, { translation: Object.assign(
      {},
      asResource(baseTranslations[language]),
      asResource(adminResources[language]),
      asResource(navigationResources[language]),
      asResource(chatResources[language]),
      asResource(dictationResources[language]),
      asResource(integrationResources[language]),
      asResource(screenResources[language]),
      asResource(runtimeResources[language]),
    ) }]),
  ),
});
applyDocumentLanguage(DEFAULT_LANGUAGE);

async function applyLanguage(language: SupportedLanguage) {
  await i18nInitialization;
  await i18n.changeLanguage(language);
  applyDocumentLanguage(language);
}

/**
 * Hydrates the UI language before React mounts. An invalid or unavailable saved
 * preference is ignored in favour of the first supported browser language.
 */
export async function initializeLanguagePreference(languages?: readonly string[]): Promise<SupportedLanguage> {
  const savedLanguage = await loadPreference(LANGUAGE_PREFERENCE_KEY).catch(() => undefined);
  const language = normalizeSupportedLanguage(savedLanguage) ?? detectSupportedLanguage(languages);
  await applyLanguage(language);
  return language;
}

/** Applies and persists an explicit user choice in IndexedDB. */
export async function setLanguagePreference(language: SupportedLanguage): Promise<void> {
  if (!supportedLanguageSet.has(language)) throw new RangeError(`Unsupported language: ${language}`);
  await applyLanguage(language);
  await savePreference(LANGUAGE_PREFERENCE_KEY, language);
}

export function getCurrentLanguage(): SupportedLanguage {
  return normalizeSupportedLanguage(i18n.resolvedLanguage ?? i18n.language) ?? DEFAULT_LANGUAGE;
}

export default i18n;
