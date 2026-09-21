import { StrictMode } from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ChatModeIndicator, NewChatSetup } from "../components/ChatMode";
import { profiles, sessions } from "../data";
import { db, saveDraft, saveEncryptedTranscript } from "../lib/db";
import { useTemporaryChat } from "../lib/temporaryChat";
import { useAppStore } from "../store/appStore";
import type { SessionSummary } from "../types";

const privateChat: SessionSummary = { ...sessions[0], id: "tmp_private_test", chatMode: "temporary", temporaryAccess: "tab-only-access" };
const supported = { ...profiles[0], capabilitySet: { ...profiles[0].capabilitySet!, features: ["session.mode.memory_read_only", "session.mode.temporary"] } };
function Lifecycle() { useTemporaryChat(privateChat); return null; }

beforeEach(() => useAppStore.setState({ demoMode: false, sessions: [privateChat], csrfToken: "csrf", selectedSessionId: privateChat.id }));
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it("selects the policy before creating and preserves the first message on failure", async () => {
  const onStart = vi.fn().mockRejectedValue(new Error("offline"));
  render(<NewChatSetup profile={supported} disabled={false} voiceAvailable onStart={onStart} />);
  fireEvent.click(screen.getByRole("radio", { name: /Temporal privado/ }));
  expect(onStart).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("textbox"), { target: { value: "private canary" } });
  await act(async () => fireEvent.click(screen.getByRole("button", { name: "Enviar mensaje" })));
  expect(onStart).toHaveBeenCalledWith({ mode: "temporary", text: "private canary", files: [], action: undefined });
  expect(screen.getByRole("textbox")).toHaveValue("private canary");
  expect(screen.getByRole("alert")).toBeVisible();
});

it("fails closed when the runtime has no restricted policy capability", () => {
  render(<NewChatSetup profile={{ ...profiles[0], capabilitySet: undefined }} disabled={false} voiceAvailable={false} onStart={vi.fn()} />);
  expect(screen.getByRole("radio", { name: /Con recuerdos/ })).toBeEnabled();
  expect(screen.getByRole("radio", { name: /Sin nuevos recuerdos/ })).toBeDisabled();
  expect(screen.getByRole("radio", { name: /Temporal privado/ })).toBeDisabled();
});

it("explains the compact mode icon using keyboard focus", () => {
  render(<ChatModeIndicator mode="memory_read_only" />);
  const indicator = screen.getByRole("button", { name: "Sin nuevos recuerdos" });
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
  fireEvent.focus(indicator);
  expect(screen.getByRole("tooltip")).toHaveTextContent("El chat se conserva");
  fireEvent.keyDown(indicator, { key: "Escape" });
  expect(screen.queryByRole("tooltip")).not.toBeInTheDocument();
});

it("renews once under StrictMode, closes on departure and removes local content", async () => {
  vi.useFakeTimers();
  const fetch = vi.fn().mockResolvedValue({ ok: true });
  vi.stubGlobal("fetch", fetch);
  const view = render(<StrictMode><Lifecycle /></StrictMode>);
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(fetch).toHaveBeenCalledTimes(1);
  expect(fetch.mock.calls[0][0]).toContain("/temporary/renew");
  expect(fetch.mock.calls[0][1].headers["X-Temporary-Chat"]).toBe("tab-only-access");
  view.unmount();
  await act(async () => vi.advanceTimersByTimeAsync(1));
  expect(fetch.mock.calls[1][0]).toContain("/temporary/close");
  expect(useAppStore.getState().sessions).not.toContainEqual(privateChat);
});

it("ends immediately on page close or an expired server lease", async () => {
  vi.useFakeTimers();
  const fetch = vi.fn().mockResolvedValue({ ok: false, status: 410 });
  vi.stubGlobal("fetch", fetch);
  render(<Lifecycle />);
  await act(async () => vi.advanceTimersByTimeAsync(30_000));
  expect(useAppStore.getState().sessions).toHaveLength(0);
  fireEvent(window, new Event("pagehide"));
  expect(fetch).toHaveBeenCalledTimes(2); // renew and exactly one close
});

it("never puts temporary drafts or transcripts into IndexedDB", async () => {
  await saveDraft(privateChat.id, "private draft");
  await saveEncryptedTranscript(privateChat.id, "workspace", [{ id: "msg", sessionId: privateChat.id, createdAt: new Date().toISOString(), role: "user", content: "private content" }]);
  expect(await db.drafts.get(privateChat.id)).toBeUndefined();
  expect(await db.transcripts.get(privateChat.id)).toBeUndefined();
});
