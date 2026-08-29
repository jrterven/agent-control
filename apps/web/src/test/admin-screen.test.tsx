import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AdminScreen } from "../screens/Screens";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";


describe("administration screen", () => {
  afterEach(() => vi.restoreAllMocks());

  it("loads sanitized audit events instead of exposing an inert control", async () => {
    useAppStore.setState({ authState: "authenticated" });
    vi.spyOn(api, "audit").mockResolvedValue([
      {
        id: "event-1",
        action: "session.export",
        targetType: "session",
        targetId: "session-1",
        outcome: "success",
        createdAt: "2026-08-28T12:00:00Z",
      },
    ]);

    render(<AdminScreen />);
    await userEvent.click(screen.getByRole("button", { name: "Abrir eventos" }));

    await waitFor(() => expect(api.audit).toHaveBeenCalledOnce());
    expect(await screen.findByText("session.export")).toBeInTheDocument();
    expect(screen.getByText("session · session-1")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Ver runbook" })).not.toBeInTheDocument();
  });
});
