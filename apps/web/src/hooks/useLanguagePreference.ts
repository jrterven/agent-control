import { useCallback, useEffect, useState } from "react";
import i18n, {
  getCurrentLanguage,
  LANGUAGE_OPTIONS,
  setLanguagePreference,
  type SupportedLanguage,
} from "../i18n";

export function useLanguagePreference() {
  const [language, setLanguage] = useState<SupportedLanguage>(getCurrentLanguage);

  useEffect(() => {
    const handleLanguageChange = () => setLanguage(getCurrentLanguage());
    i18n.on("languageChanged", handleLanguageChange);
    return () => { i18n.off("languageChanged", handleLanguageChange); };
  }, []);

  const changeLanguage = useCallback((nextLanguage: SupportedLanguage) => (
    setLanguagePreference(nextLanguage)
  ), []);

  return { language, changeLanguage, languageOptions: LANGUAGE_OPTIONS };
}
