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
  await initializeLanguagePreference();
  restorePwaUpdateContext();
  initializePwaUpdates();

  ReactDOM.createRoot(document.getElementById("root")!).render(
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>
    </React.StrictMode>,
  );
}

void mountApplication();
