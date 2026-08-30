(() => {
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

  let mounted = false;
  let fatal = false;
  let recoveryRequested = false;
  let shell;
  let status;
  let recovery;
  let timer;

  const clearRecoveryTimer = () => {
    if (timer) window.clearTimeout(timer);
    timer = undefined;
  };

  const renderRecovery = () => {
    if (!shell || !status || !recovery) return;
    shell.hidden = false;
    shell.dataset.agentControlBootState = "failed";
    status.textContent = copy.failed;
    recovery.hidden = false;
  };

  const requestRecovery = (isFatal = false) => {
    fatal = fatal || isFatal;
    recoveryRequested = true;
    mounted = false;
    clearRecoveryTimer();
    document.getElementById("root")?.removeAttribute("data-agent-control-mounted");
    renderRecovery();
  };

  const markMounted = () => {
    if (fatal) return;
    mounted = true;
    recoveryRequested = false;
    clearRecoveryTimer();
    if (shell) {
      shell.dataset.agentControlBootState = "mounted";
      shell.hidden = true;
    }
  };

  // This file is loaded synchronously after the persistent shell and before the
  // application module, so fatal listeners exist before any module executes.
  window.addEventListener("agent-control:mounted", markMounted);
  window.addEventListener("agent-control:fatal", () => requestRecovery(true));
  window.addEventListener("error", () => {
    if (!mounted) requestRecovery();
  }, true);
  window.addEventListener("unhandledrejection", () => {
    if (!mounted) requestRecovery();
  });

  let reloadStarted = false;
  const reloadWithCacheBust = () => {
    if (reloadStarted) return;
    reloadStarted = true;
    try {
      const nextUrl = new URL(window.location.href);
      nextUrl.searchParams.set("app-recovery", Date.now().toString());
      window.location.replace(nextUrl.href);
    } catch {
      window.location.reload();
    }
  };

  const cleanApplicationCaches = async () => {
    const cleanups = [];
    if ("serviceWorker" in navigator) {
      cleanups.push((async () => {
        const registrations = await navigator.serviceWorker.getRegistrations();
        await Promise.allSettled(registrations.map((registration) => registration.unregister()));
      })());
    }
    if ("caches" in window) {
      cleanups.push((async () => {
        const cacheNames = await caches.keys();
        await Promise.allSettled(cacheNames.map((cacheName) => caches.delete(cacheName)));
      })());
    }
    await Promise.allSettled(cleanups);
  };

  const repair = async () => {
    if (!status || !recovery) return;
    recovery.disabled = true;
    status.textContent = copy.repairing;

    // Browser storage APIs can remain pending indefinitely after a WebView
    // crash. Reload on a hard deadline while still giving cleanup time to win.
    const hardReload = window.setTimeout(reloadWithCacheBust, 2_500);
    try {
      await Promise.race([
        cleanApplicationCaches(),
        new Promise((resolve) => window.setTimeout(resolve, 1_800)),
      ]);
    } finally {
      window.clearTimeout(hardReload);
      reloadWithCacheBust();
    }
  };

  const initializeShell = () => {
    shell = document.querySelector("[data-agent-control-boot]");
    status = document.querySelector("[data-agent-control-boot-status]");
    const recoveryElement = document.querySelector("[data-agent-control-boot-recovery]");
    if (!shell || !status || !(recoveryElement instanceof HTMLButtonElement)) return;
    recovery = recoveryElement;
    status.textContent = copy.starting;
    recovery.textContent = copy.action;
    recovery.addEventListener("click", () => { void repair(); });

    if (fatal || recoveryRequested) {
      renderRecovery();
    } else if (mounted) {
      markMounted();
    } else {
      shell.dataset.agentControlBootState = "starting";
      timer = window.setTimeout(() => requestRecovery(), 8_000);
    }
  };

  initializeShell();
})();
