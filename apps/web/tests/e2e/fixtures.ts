import { expect, test as base, type BrowserContext, type Route } from "@playwright/test";

export const bootstrapData = {
  gateways: [
    {
      id: "gateway-e2e",
      name: "Gateway E2E",
      location: "Tailscale · prueba local",
      status: "connected",
      latencyMs: 18,
      version: "0.20.6",
      sha: "9978706e",
      capabilities: {
        realtime: true,
        sessions: true,
        prompts: true,
        interrupt: true,
        cron: true,
        cronCreate: true,
        cronUpdate: true,
        cronDelete: true,
        cronTrigger: true,
        profiles: true,
        config: true,
        memory: true,
      },
    },
  ],
  profiles: [
    {
      id: "profile-newton-e2e",
      gatewayId: "gateway-e2e",
      technicalName: "default",
      displayName: "Newton",
      model: "hermes-e2e",
      status: "ready",
      // This deterministic profile represents a production allowlist entry.
      // The technical name can remain Newton/`default`; mutation authority is
      // projected by the backend-owned `mutable` flag, never inferred here.
      mutable: true,
      capabilities: {
        realtime: true,
        sessions: true,
        prompts: true,
        interrupt: true,
        cron: true,
        cronCreate: true,
        cronUpdate: true,
        cronDelete: true,
        cronTrigger: true,
        profiles: true,
        config: true,
        memory: true,
      },
    },
  ],
  workspaces: [
    {
      id: "workspace-e2e",
      name: "Operación móvil",
      description: "Estado determinista para las pruebas de interfaz",
      sessionCount: 1,
      updatedAt: "2026-08-28T10:14:00Z",
    },
  ],
  sessions: [
    {
      id: "session-e2e",
      gatewayId: "gateway-e2e",
      profileName: "default",
      storedSessionId: "stored-e2e",
      runtimeSessionId: "runtime-e2e",
      workspaceId: "workspace-e2e",
      profileId: "profile-newton-e2e",
      title: "Prueba de reconexión",
      preview: "Un chat aislado y reproducible",
      updatedAt: "10:14",
    },
  ],
  automations: [],
} as const;

const history = {
  items: [
    {
      id: "history-user-e2e",
      role: "user",
      content: "Comprueba la reconexión móvil.",
      createdAt: "2026-08-28T10:14:00Z",
    },
    {
      id: "history-assistant-e2e",
      role: "assistant",
      content: "La sesión está aislada y lista para continuar.",
      createdAt: "2026-08-28T10:14:05Z",
    },
  ],
};

function json(route: Route, body: unknown, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

export async function installMockApi(
  context: BrowserContext,
  options: { authenticated?: boolean } = {},
) {
  let authenticated = options.authenticated ?? true;
  const handler = async (route: Route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();

    if (path === "/api/v1/auth/me") {
      return authenticated
        ? json(route, { id: "admin-e2e", name: "Admin E2E", csrfToken: "csrf-e2e" })
        : json(route, { detail: "Not authenticated" }, 401);
    }
    if (path === "/api/v1/auth/login" && method === "POST") {
      authenticated = true;
      return json(route, { id: "admin-e2e", name: "Admin E2E", csrfToken: "csrf-e2e" });
    }
    if (path === "/api/v1/auth/logout" && method === "POST") {
      authenticated = false;
      return route.fulfill({ status: 204 });
    }
    if (path === "/api/v1/bootstrap") return json(route, bootstrapData);
    if (path === "/api/v1/profiles/refresh" && method === "POST") return json(route, []);
    if (path === "/api/v1/sessions/sync" && method === "POST") return json(route, []);
    if (path === "/api/v1/automations/sync" && method === "POST") return json(route, []);
    if (path === "/api/v1/sessions/session-e2e/messages") return json(route, history);
    if (path === "/api/v1/realtime/tickets" && method === "POST") {
      return json(route, { detail: "Realtime intentionally disabled in deterministic E2E" }, 503);
    }
    return json(route, { detail: `Unexpected E2E request: ${method} ${path}` }, 404);
  };

  await context.route("**/api/v1/**", handler);
  return {
    remove: () => context.unroute("**/api/v1/**", handler),
  };
}

type Fixtures = {
  deterministicApi: Awaited<ReturnType<typeof installMockApi>>;
};

export const test = base.extend<Fixtures>({
  deterministicApi: [async ({ context }, use) => {
    const api = await installMockApi(context);
    await use(api);
  }, { auto: true }],
});

export { expect };
