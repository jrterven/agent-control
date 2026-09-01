import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { NotificationMenu } from "../components/NotificationMenu";
import { TopBar } from "../components/TopBar";
import { useAppStore } from "../store/appStore";

const now = new Date("2026-08-31T16:00:00.000Z");

function session(index: number) {
  return {
    id: `session-${index}`,
    storedSessionId: `stored-${index}`,
    profileId: "profile-1",
    workspaceId: index % 2 ? "workspace-1" : undefined,
    title: `Chat reciente ${index}`,
    preview: "",
    updatedAt: new Date(now.getTime() - index * 86_400_000).toISOString(),
    unread: index < 2,
  };
}

describe("notification menu", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(now);
    useAppStore.setState({
      authState: "authenticated",
      bootstrapLoaded: true,
      demoMode: false,
      csrfToken: "csrf",
      selectedGatewayId: "gateway-1",
      selectedProfileId: "profile-1",
      selectedWorkspaceId: "workspace-1",
      selectedSessionId: "session-0",
      timeZone: "America/Mexico_City",
      connection: "connected",
      notificationsOpen: false,
      profiles: [{
        id: "profile-1",
        gatewayId: "gateway-1",
        technicalName: "newton",
        displayName: "Newton",
        model: "gpt-test",
        status: "ready",
        mutable: true,
      }],
      workspaces: [{
        id: "workspace-1",
        name: "Agent Control",
        description: "",
        sessionCount: 6,
        updatedAt: now.toISOString(),
      }],
      sessions: Array.from({ length: 12 }, (_, index) => session(index)),
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    useAppStore.getState().resetPrivateState();
  });

  it("shows unread count and only the ten most recent chats grouped by day", () => {
    render(<><TopBar /><NotificationMenu /></>);
    expect(screen.getByLabelText("2 chats sin leer")).toHaveTextContent("2");
    fireEvent.click(screen.getByRole("button", { name: "Abrir notificaciones" }));

    const list = screen.getByTestId("recent-notification-chats");
    expect(within(list).getAllByRole("button")).toHaveLength(10);
    expect(within(list).getByText("Hoy")).toBeInTheDocument();
    expect(within(list).getByText("Ayer")).toBeInTheDocument();
    expect(within(list).getByText("Chat reciente 9")).toBeInTheDocument();
    expect(within(list).queryByText("Chat reciente 10")).not.toBeInTheDocument();
    expect(within(list).getAllByText("Agent Control")).not.toHaveLength(0);
    expect(within(list).getAllByText("Sin espacio de trabajo")).not.toHaveLength(0);
  });

  it("requests opening a selected recent chat", () => {
    const opened = vi.fn();
    window.addEventListener("agent-control:open-session", opened);
    render(<><TopBar /><NotificationMenu /></>);
    fireEvent.click(screen.getByRole("button", { name: "Abrir notificaciones" }));
    fireEvent.click(screen.getByRole("button", { name: "Abrir Chat reciente 3" }));
    expect(opened).toHaveBeenCalledOnce();
    const event = opened.mock.calls[0][0] as CustomEvent<{ sessionId: string }>;
    expect(event.detail.sessionId).toBe("session-3");
    window.removeEventListener("agent-control:open-session", opened);
  });
});
