(() => {
  const root = document.getElementById("root");
  const status = document.querySelector("[data-agent-control-boot-status]");
  const recovery = document.querySelector("[data-agent-control-boot-recovery]");
  if (!root || !status || !(recovery instanceof HTMLButtonElement)) return;

  const language = (navigator.language || "en").toLowerCase().split("-")[0];
  const messages = {
    de: {
      starting: "Agent Control wird gestartet…",
      failed: "Die App konnte nicht gestartet werden. Lokale Entwürfe bleiben erhalten.",
      action: "Reparieren und neu laden",
      repairing: "App wird repariert…",
    },
    en: {
      starting: "Starting Agent Control…",
      failed: "The app could not start. Your local drafts will be preserved.",
      action: "Repair and reload",
      repairing: "Repairing the app…",
    },
    es: {
      starting: "Iniciando Agent Control…",
      failed: "La app no pudo iniciar. Tus borradores locales se conservarán.",
      action: "Reparar y recargar",
      repairing: "Reparando la app…",
    },
    fr: {
      starting: "Démarrage d’Agent Control…",
      failed: "L’application n’a pas pu démarrer. Vos brouillons locaux seront conservés.",
      action: "Réparer et recharger",
      repairing: "Réparation de l’application…",
    },
    pt: {
      starting: "Iniciando o Agent Control…",
      failed: "O app não pôde iniciar. Seus rascunhos locais serão preservados.",
      action: "Reparar e recarregar",
      repairing: "Reparando o app…",
    },
  };
  const copy = messages[language] || messages.en;
  status.textContent = copy.starting;
  recovery.textContent = copy.action;

  let mounted = false;
  let timer;
  const showRecovery = () => {
    if (mounted || root.dataset.agentControlMounted === "true") return;
    status.textContent = copy.failed;
    recovery.hidden = false;
  };
  const markMounted = () => {
    mounted = true;
    if (timer) window.clearTimeout(timer);
  };

  window.addEventListener("agent-control:mounted", markMounted, { once: true });
  window.addEventListener("error", showRecovery, true);
  window.addEventListener("unhandledrejection", showRecovery, { once: true });
  timer = window.setTimeout(showRecovery, 8_000);

  recovery.addEventListener("click", async () => {
    recovery.disabled = true;
    status.textContent = copy.repairing;
    try {
      if ("serviceWorker" in navigator) {
        const registrations = await navigator.serviceWorker.getRegistrations();
        await Promise.all(registrations.map((registration) => registration.unregister()));
      }
      if ("caches" in window) {
        const cacheNames = await caches.keys();
        await Promise.all(cacheNames.map((cacheName) => caches.delete(cacheName)));
      }
    } finally {
      const nextUrl = new URL(window.location.href);
      nextUrl.searchParams.set("app-recovery", Date.now().toString());
      window.location.replace(nextUrl.href);
    }
  });
})();
