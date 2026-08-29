import i18n from "i18next";
import { initReactI18next } from "react-i18next";

void i18n.use(initReactI18next).init({
  lng: "es",
  fallbackLng: "es",
  interpolation: { escapeValue: false },
  resources: {
    es: { translation: {
      chats: "Chats", agents: "Agentes", automations: "Automatizaciones", more: "Más",
      connected: "Conectado", reconnecting: "Reconectando", offline: "Sin conexión",
      messagePlaceholder: "Mensaje a {{agent}}…", newChat: "Nuevo chat", search: "Buscar",
    } },
    en: { translation: {
      chats: "Chats", agents: "Agents", automations: "Automations", more: "More",
      connected: "Connected", reconnecting: "Reconnecting", offline: "Offline",
      messagePlaceholder: "Message {{agent}}…", newChat: "New chat", search: "Search",
    } },
  },
});

export default i18n;
