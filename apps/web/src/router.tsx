import { createRootRoute, createRoute, createRouter, Navigate, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { useCallback } from "react";
import { useTranslation } from "react-i18next";
import { Button } from "@hermes-control/ui";
import { AppShell } from "./components/AppShell";
import { ChatView } from "./components/ChatView";
import { useAuthBootstrap, useBootstrapData, useOfflineTranscriptCache, useRealtimeConnection, useSessionHistory, useThemePreference } from "./hooks";
import {
  AdminScreen, AgentsScreen, AutomationsScreen, ConfigScreen, DiagnosticsScreen,
  GatewaysScreen, LoginScreen, MoreScreen, SearchScreen, SettingsScreen,
} from "./screens/Screens";
import { useAppStore } from "./store/appStore";
import { BrandMark } from "./components/BrandMark";
import { useChatNotificationRuntime } from "./lib/chatNotifications";
import { pairingCodeFromReturnTo, useCloudConfiguration } from "./lib/cloud";
import { ConnectorsScreen } from "./screens/CloudScreens";

function RootLayout() {
  const { t } = useTranslation();
  const configuration = useCloudConfiguration();
  useAuthBootstrap();
  useBootstrapData();
  useThemePreference();
  useRealtimeConnection();
  useSessionHistory();
  useOfflineTranscriptCache();
  const navigate = useNavigate();
  const openNotificationSession = useCallback((sessionId: string) => {
    useAppStore.getState().selectSession(sessionId);
    void navigate({ to: "/chats" });
  }, [navigate]);
  useChatNotificationRuntime(openNotificationSession);
  const authState = useAppStore((state) => state.authState);
  const bootstrapLoaded = useAppStore((state) => state.bootstrapLoaded);
  // Android can restore a mounted PWA with an expired cookie. Subscribe to the
  // router's canonical location so the auth redirect cannot leave a stale,
  // empty <Navigate> tree after the persistent boot shell has been hidden.
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const searchStr = useRouterState({ select: (state) => state.location.searchStr });
  const pairingCode = pairingCodeFromReturnTo(new URLSearchParams(searchStr).get("returnTo"));
  if (authState === "checking") return <main className="boot-screen"><BrandMark size="lg" label="Agent Control" /><p>Preparando tu centro de control…</p></main>;
  if (authState === "unauthenticated" && pathname !== "/login") return <><Navigate to="/login" search={{ returnTo: pathname === "/connect" ? `${pathname}${searchStr}` : undefined }} replace /><main className="boot-screen"><BrandMark size="lg" label="Agent Control" /><p>Abriendo el acceso seguro…</p></main></>;
  if ((authState === "authenticated" || authState === "offline") && pathname === "/login") return <>{pairingCode ? <Navigate to="/connect" search={{ code: pairingCode }} replace /> : <Navigate to="/chats" replace />}<main className="boot-screen"><BrandMark size="lg" label="Agent Control" /><p>Abriendo tus conversaciones…</p></main></>;
  if (authState === "authenticated" && !configuration.methods) return <main className="boot-screen"><BrandMark size="lg" label="Agent Control" /><p role={configuration.error ? "alert" : "status"}>{t(configuration.error ? "cloud.unavailable" : "cloud.loading")}</p>{configuration.error ? <Button onClick={() => void configuration.load()}>{t("cloud.retry")}</Button> : null}</main>;
  if (authState === "authenticated" && !bootstrapLoaded) return <main className="boot-screen" aria-live="polite"><BrandMark size="lg" label="Agent Control" /><p>Conectando con tus agentes…</p></main>;
  return <Outlet />;
}

const rootRoute = createRootRoute({ component: RootLayout });
const indexRoute = createRoute({ getParentRoute: () => rootRoute, path: "/", component: () => <Navigate to="/chats" replace /> });
const loginRoute = createRoute({ getParentRoute: () => rootRoute, path: "/login", validateSearch: (search: Record<string, unknown>): { returnTo?: string; error?: string } => ({ returnTo: typeof search.returnTo === "string" ? search.returnTo : undefined, error: typeof search.error === "string" ? search.error : undefined }), component: LoginScreen });

function shell(component: React.ReactNode, conversation = false) {
  return <AppShell conversation={conversation}>{component}</AppShell>;
}

const chatRoute = createRoute({ getParentRoute: () => rootRoute, path: "/chats", component: () => shell(<ChatView />, true) });
const agentsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/agents", component: () => shell(<AgentsScreen />) });
const automationsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/automations", component: () => shell(<AutomationsScreen />) });
const moreRoute = createRoute({ getParentRoute: () => rootRoute, path: "/more", component: () => shell(<MoreScreen />) });
const searchRoute = createRoute({ getParentRoute: () => rootRoute, path: "/search", component: () => shell(<SearchScreen />) });
const gatewaysRoute = createRoute({ getParentRoute: () => rootRoute, path: "/gateways", component: () => shell(<GatewaysScreen />) });
const computersRoute = createRoute({ getParentRoute: () => rootRoute, path: "/computers", component: () => shell(<ConnectorsScreen />) });
const connectRoute = createRoute({ getParentRoute: () => rootRoute, path: "/connect", validateSearch: (search: Record<string, unknown>): { code?: string } => ({ code: typeof search.code === "string" ? search.code.slice(0, 64) : undefined }), component: () => shell(<ConnectorsScreen pairing />) });
const configRoute = createRoute({ getParentRoute: () => rootRoute, path: "/config", component: () => shell(<ConfigScreen />) });
const diagnosticsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/diagnostics", component: () => shell(<DiagnosticsScreen />) });
const settingsRoute = createRoute({ getParentRoute: () => rootRoute, path: "/settings", component: () => shell(<SettingsScreen />) });
const adminRoute = createRoute({ getParentRoute: () => rootRoute, path: "/admin", component: () => shell(<AdminScreen />) });

const routeTree = rootRoute.addChildren([indexRoute, loginRoute, chatRoute, agentsRoute, automationsRoute, moreRoute, searchRoute, gatewaysRoute, computersRoute, connectRoute, configRoute, diagnosticsRoute, settingsRoute, adminRoute]);

export const router = createRouter({ routeTree, defaultPreload: "intent", scrollRestoration: true });

declare module "@tanstack/react-router" {
  interface Register { router: typeof router }
}
