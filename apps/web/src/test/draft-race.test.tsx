import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../i18n";
import { ChatView } from "../components/ChatView";
import { gateways, initialMessages, profiles, sessions, workspaces } from "../data";
import { db, type DraftRecord } from "../lib/db";
import { useAppStore } from "../store/appStore";

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((next) => { resolve = next; });
  return { promise, resolve };
}

describe("draft loading across sessions", () => {
  beforeEach(() => {
    useAppStore.setState({
      authState: "authenticated", demoMode: true, selectedProfileId: "profile-newton", selectedSessionId: "session-papers",
      gateways, profiles, sessions, workspaces, messages: initialMessages, streamingBySession: {},
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("ignores a stale draft when session loads resolve out of order", async () => {
    const first = deferred<DraftRecord | undefined>();
    const second = deferred<DraftRecord | undefined>();
    vi.spyOn(db.drafts, "get")
      .mockReturnValueOnce(first.promise as never)
      .mockReturnValueOnce(second.promise as never);
    render(<ChatView />);

    act(() => useAppStore.getState().selectSession("session-evals"));
    const composer = screen.getByRole("textbox", { name: /Mensaje a/i });
    expect(composer).toHaveValue("");

    await act(async () => second.resolve({ sessionId: "session-evals", content: "Borrador B", updatedAt: 2 }));
    await waitFor(() => expect(composer).toHaveValue("Borrador B"));

    await act(async () => first.resolve({ sessionId: "session-papers", content: "Borrador A obsoleto", updatedAt: 1 }));
    expect(composer).toHaveValue("Borrador B");
  });

  it("starts persisting a draft before an immediate page teardown can discard it", () => {
    const put = vi.spyOn(db.drafts, "put").mockResolvedValue("session-papers");
    const { unmount } = render(<ChatView />);
    const composer = screen.getByRole("textbox", { name: /Mensaje a/i });

    fireEvent.change(composer, { target: { value: "Borrador antes de recargar" } });

    expect(put).toHaveBeenCalledWith(expect.objectContaining({
      sessionId: "session-papers",
      content: "Borrador antes de recargar",
    }));
    unmount();
  });
});
