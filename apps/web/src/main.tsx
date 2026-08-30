import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import "@hermes-control/ui/styles.css";
import { initializeLanguagePreference } from "./i18n";
import { initializePwaUpdates, restorePwaUpdateContext } from "./lib/pwaUpdate";
import "./styles.css";
import { router } from "./router";

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 15_000, retry: 1, refetchOnWindowFocus: false }, mutations: { retry: false } },
});

async function mountApplication() {
  restorePwaUpdateContext();
  initializePwaUpdates();

  // IndexedDB can remain blocked after an interrupted Android WebView/PWA
  // update. Language hydration must never keep the entire application blank.
  const languageReady = initializeLanguagePreference().catch(() => undefined);
  await Promise.race([
    languageReady,
    new Promise<void>((resolve) => window.setTimeout(resolve, 1_500)),
  ]);

  const root = document.getElementById("root");
  if (!root) throw new Error("Agent Control root element is missing");
  ReactDOM.createRoot(root).render(
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </React.StrictMode>,
  );
  window.requestAnimationFrame(() => {
    root.dataset.agentControlMounted = "true";
    window.dispatchEvent(new Event("agent-control:mounted"));
  });
}

void mountApplication();
