import { createChatForCurrentContext } from "../hooks";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { LeftSidebar } from "../components/LeftSidebar";
import { gateways, profiles, sessions, workspaces } from "../data";
import { api } from "../lib/api";
import { useCloudConfigurationStore } from "../lib/cloud";
import { db } from "../lib/db";
import { useAppStore } from "../store/appStore";
import type { SessionSummary } from "../types";

const navigation = vi.hoisted(() => ({ navigate: vi.fn(), pathname: "/computers" }));

vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => navigation.navigate,
  useRouterState: ({ select }: { select: (state: { location: { pathname: string } }) => unknown }) => select({ location: { pathname: navigation.pathname } }),
  Link: ({ to, children, onClick, ...props }: React.AnchorHTMLAttributes<HTMLAnchorElement> & { to: string }) => <a href={to} {...props} onClick={(event) => { event.preventDefault(); onClick?.(event); }}>{children}</a>,
}));

const newSession: SessionSummary = { ...sessions[0], id: "session-new", title: "Nueva conversación", preview: "" };

beforeEach(() => {
  vi.mocked(window.matchMedia).mockImplementation((query) => ({
    matches: query.includes("prefers-color-scheme: dark"), media: query, onchange: null,
    addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn(),
  }));
  navigation.navigate.mockReset();
  navigation.pathname = "/computers";
  useCloudConfigurationStore.setState({ methods: { mode: "cloud", googleEnabled: true } });
  useAppStore.getState().resetPrivateState();
  useAppStore.setState({
    authState: "authenticated", csrfToken: "csrf-test", demoMode: false,
    connection: "connected", bootstrapLoaded: true,
    leftDrawerOpen: true, gatewayMenuOpen: false,
    gateways, profiles: profiles.map((profile) => ({ ...profile, mutable: true })), sessions, workspaces,
    selectedGatewayId: "gateway-home", selectedProfileId: "profile-newton",
    selectedWorkspaceId: "workspace-papers", selectedSessionId: "session-papers",
  });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  useCloudConfigurationStore.setState({ methods: undefined });
});

describe("equipment selector", () => {
  it("dismisses outside without stealing focus from the clicked control", async () => {
    const user = userEvent.setup();
    render(<><LeftSidebar /><button type="button">Fuera del selector</button></>);
    const trigger = screen.getByRole("button", { name: /gx10-58f9 Tailscale/ });
    await user.click(trigger);
    expect(screen.getByRole("menu")).toBeInTheDocument();
    expect(screen.getByRole("menuitemradio", { name: /gx10-58f9/ })).toHaveFocus();
    const outside = screen.getByRole("button", { name: "Fuera del selector" });
    await user.click(outside);
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(outside).toHaveFocus();
  });

  it("supports keyboard selection and restores the selector after Escape", async () => {
    const user = userEvent.setup();
    render(<LeftSidebar />);
    const trigger = screen.getByRole("button", { name: /gx10-58f9 Tailscale/ });
    trigger.focus();
    await user.keyboard("{ArrowDown}{ArrowDown}");
    expect(screen.getByRole("menuitemradio", { name: /Mock local/ })).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
    await user.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    expect(useAppStore.getState().selectedGatewayId).toBe("gateway-mock");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("dismisses when tabbing away or opening computer management", async () => {
    const user = userEvent.setup();
    render(<LeftSidebar />);
    const trigger = screen.getByRole("button", { name: /gx10-58f9 Tailscale/ });
    await user.click(trigger);
    await user.keyboard("{End}{Tab}");
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Buscar en todo/ })).toHaveFocus();
    await user.click(trigger);
    await user.click(screen.getByRole("menuitem", { name: "Mis equipos" }));
    expect(useAppStore.getState().gatewayMenuOpen).toBe(false);
    expect(useAppStore.getState().leftDrawerOpen).toBe(false);
  });
});

describe("new chat navigation", () => {
  it("opens a successfully recovered chat after its CSRF token is renewed", async () => {
    const user = userEvent.setup();
    useAppStore.setState({ userId: "owner-a" });
    const generation = useAppStore.getState().authGeneration;
    const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(json({ detail: "Invalid CSRF token" }, 403))
      .mockResolvedValueOnce(json({ id: "owner-a", csrfToken: "fresh-csrf" }))
      .mockResolvedValueOnce(json(newSession));
    vi.stubGlobal("fetch", fetchMock);
    render(<LeftSidebar />);

    await user.click(screen.getByRole("button", { name: "Nuevo chat" }));
    await act(async () => { await createChatForCurrentContext(); });

    await waitFor(() => expect(navigation.navigate).toHaveBeenCalledWith({ to: "/chats" }));
    expect(useAppStore.getState()).toMatchObject({ selectedSessionId: newSession.id, csrfToken: "fresh-csrf", authGeneration: generation });
    expect(useAppStore.getState().sessions.filter((session) => session.id === newSession.id)).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(fetchMock.mock.calls.map(([path]) => path)).toEqual(["/api/v1/sessions", "/api/v1/auth/me", "/api/v1/sessions"]);
  });

  it("discards a completed chat after signing out and back in as the same owner", async () => {
    const user = userEvent.setup();
    useAppStore.setState({ userId: "owner-a" });
    let finish!: (session: SessionSummary) => void;
    vi.spyOn(api, "createSession").mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    render(<LeftSidebar />);
    await user.click(screen.getByRole("button", { name: "Nuevo chat" }));
    const creation = createChatForCurrentContext();

    await act(async () => {
      useAppStore.getState().setAuth("unauthenticated");
      useAppStore.getState().setAuth("authenticated", "Owner A", "new-login-csrf", false, "owner-a");
      finish(newSession);
      await creation;
    });

    expect(navigation.navigate).toHaveBeenCalledWith({ to: "/chats" });
    expect(useAppStore.getState().sessions).toHaveLength(0);
    expect(useAppStore.getState().selectedSessionId).toBe("");
  });

  it.each(["/computers", "/settings"])("opens the mode selector from %s without creating a session", async (pathname) => {
    navigation.pathname = pathname;
    const user = userEvent.setup();
    const create = vi.spyOn(api, "createSession");
    render(<LeftSidebar />);
    await user.click(screen.getByRole("button", { name: "Nuevo chat" }));
    expect(create).not.toHaveBeenCalled();
    expect(navigation.navigate).toHaveBeenCalledWith({ to: "/chats" });
    expect(useAppStore.getState()).toMatchObject({
      preparingChat: true, selectedSessionId: "", selectedProfileId: "profile-newton",
      selectedGatewayId: "gateway-home", selectedWorkspaceId: "workspace-papers", leftDrawerOpen: false,
    });
    useAppStore.getState().hydrateBootstrap({ gateways, profiles, workspaces, sessions, automations: [] });
    expect(useAppStore.getState().selectedSessionId).toBe("");
  });

  it("preserves preparation after a failed first-send creation", async () => {
    useAppStore.getState().prepareChat();
    vi.spyOn(api, "createSession").mockRejectedValueOnce(new Error("offline"));
    await expect(createChatForCurrentContext()).rejects.toThrow("offline");
    expect(useAppStore.getState()).toMatchObject({ selectedSessionId: "", preparingChat: true });
  });

  it("can open preparation offline without sending a creation request", async () => {
    const user = userEvent.setup();
    useAppStore.setState({ connection: "offline" });
    const create = vi.spyOn(api, "createSession");
    render(<LeftSidebar />);
    await user.click(screen.getByRole("button", { name: "Nuevo chat" }));
    expect(create).not.toHaveBeenCalled();
    expect(navigation.navigate).toHaveBeenCalledWith({ to: "/chats" });
  });
});

describe("existing conversation navigation", () => {
  it.each(["/agents", "/computers", "/settings", "/automations", "/chats"])("opens the selected conversation directly from %s", async (pathname) => {
    navigation.pathname = pathname;
    const user = userEvent.setup();
    const create = vi.spyOn(api, "createSession");
    render(<LeftSidebar />);

    await user.click(screen.getByRole("button", { name: /^Memoria de agentes · agosto/ }));

    expect(navigation.navigate).toHaveBeenCalledExactlyOnceWith({ to: "/chats" });
    expect(useAppStore.getState()).toMatchObject({
      selectedSessionId: "session-papers", selectedProfileId: "profile-newton",
      selectedGatewayId: "gateway-home", selectedWorkspaceId: "workspace-papers", leftDrawerOpen: false,
    });
    expect(create).not.toHaveBeenCalled();
  });

  it("opens a pinned conversation by keyboard, restoring its computer and context without clearing drafts", async () => {
    navigation.pathname = "/agents";
    const user = userEvent.setup();
    const profile = { ...profiles[1], id: "profile-other-computer", gatewayId: "gateway-mock" };
    const pinned: SessionSummary = {
      ...sessions[1], id: "session-pinned", profileId: profile.id, workspaceId: undefined,
      title: "Conversación fijada", pinnedAt: "2026-09-17T10:00:00Z",
    };
    const draft = { sessionId: "session-papers", content: "Borrador sin enviar", updatedAt: 1 };
    await db.drafts.put(draft);
    useAppStore.setState({ profiles: [...profiles, profile], sessions: [...sessions, pinned] });
    render(<LeftSidebar />);

    screen.getByRole("button", { name: /^Conversación fijada/ }).focus();
    await user.keyboard("{Enter}");

    expect(navigation.navigate).toHaveBeenCalledExactlyOnceWith({ to: "/chats" });
    expect(useAppStore.getState()).toMatchObject({
      selectedSessionId: pinned.id, selectedProfileId: profile.id,
      selectedGatewayId: "gateway-mock", selectedWorkspaceId: "", leftDrawerOpen: false,
    });
    expect(await db.drafts.get(draft.sessionId)).toEqual(draft);
    await db.drafts.delete(draft.sessionId);
  });
});
