import { createRootRoute, createRoute, createRouter, Navigate, Outlet, useRouterState } from "@tanstack/react-router";
import { AppShell } from "./components/AppShell";
import { ChatView } from "./components/ChatView";
import { useAuthBootstrap, useBootstrapData, useOfflineTranscriptCache, useRealtimeConnection, useSessionHistory, useThemePreference } from "./hooks";
import {
  AdminScreen, AgentsScreen, AutomationsScreen, ConfigScreen, DiagnosticsScreen,
  GatewaysScreen, LoginScreen, MoreScreen, SearchScreen, SettingsScreen,
} from "./screens/Screens";
import { useAppStore } from "./store/appStore";
import { BrandMark } from "./components/BrandMark";

function RootLayout() {
  useAuthBootstrap();
  useBootstrapData();
  useThemePreference();
  useRealtimeConnection();
  useSessionHistory();
  useOfflineTranscriptCache();
  const authState = useAppStore((state) => state.authState);
  const bootstrapLoaded = useAppStore((state) => state.bootstrapLoaded);
  // Android can restore a mounted PWA with an expired cookie. Subscribe to the
  // router's canonical location so the auth redirect cannot leave a stale,
  // empty <Navigate> tree after the persistent boot shell has been hidden.
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  if (authState === "checking") return <main className="boot-screen"><BrandMark size="lg" label="Agent Control" /><p>Preparando tu centro de control…</p></main>;
  if (authState === "unauthenticated" && pathname !== "/login") return <><Navigate to="/login" replace /><main className="boot-screen"><BrandMark size="lg" label="Agent Control" /><p>Abriendo el acceso seguro…</p></main></>;
  if ((authState === "authenticated" || authState === "offline") && pathname === "/login") return <><Navigate to="/chats" replace /><main className="boot-screen"><BrandMark size="lg" label="Agent Control" /><p>Abriendo tus conversaciones…</p></main></>;
  if (authState === "authenticated" && !bootstrapLoaded) return <main className="boot-screen" aria-live="polite"><BrandMark size="lg" label="Agent Control" /><p>Conectando con tus agentes…</p></main>;
  return <Outlet />;
}

const rootRoute = createRootRoute({ component: RootLayout });
const indexRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: () => <Navigate to="/chats" replace /> });
const loginRoute = createRoute({ getParentRoute: () => rootRoute, path: "/login", component: LoginScreen });

function shell(component: React.ReactNode, conversation = false) {
  return <AppShell conversation={conversation}>{component}</AppShell>;
}

const chatRoute = createRoute({ getParentRoute: () => rootRoute, path: "/chats", component: () => shell(<ChatView />, true) });
const agentsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/agents", component: () => shell(<AgentsScreen />) });
const automationsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/automations", component: () => shell(<AutomationsScreen />) });
const moreRoute = createRoute({ getParentRoute: () => rootRoute, path: "/more", component: () => shell(<MoreScreen />) });
const searchRoute = createRoute({ getParentRoute: () => rootRoute, path: "/search", component: () => shell(<SearchScreen />) });
const gatewaysRoute = createRoute({ getParentRoute: () => rootRoute, path: "/gateways", component: () => shell(<GatewaysScreen />) });
const configRoute = createRoute({ getParentRoute: () => rootRoute, path: "/config", component: () => shell(<ConfigScreen />) });
const diagnosticsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/diagnostics", component: () => shell(<DiagnosticsScreen />) });
const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings", component: () => shell(<SettingsScreen />) });
const adminRoute = createRoute({ getParentRoute: () => rootRoute, path: "/admin", component: () => shell(<AdminScreen />) });

const routeTree = rootRoute.addChildren([indexRoute, loginRoute, chatRoute, agentsRoute, automationsRoute, moreRoute, searchRoute, gatewaysRoute, configRoute, diagnosticsRoute, settingsRoute, adminRoute]);

export const router = createRouter({ routeTree, defaultPreload: "intent", scrollRestoration: true });

declare module "@tanstack/react-router" {
  interface Register { router: typeof router }
}
