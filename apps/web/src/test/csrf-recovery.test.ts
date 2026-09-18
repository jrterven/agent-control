import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const csrfRejected = () => json({ detail: "Invalid CSRF token" }, 403);
const freshIdentity = { id: "owner-a", name: "Owner A", csrfToken: "fresh-csrf" };
const session = { id: "session-a", gatewayId: "gateway-a", profileName: "default", storedSessionId: "stored-a", title: "Chat", updatedAt: "now", workspaceId: "destination" };

beforeEach(() => {
  useAppStore.getState().resetPrivateState();
  useAppStore.setState({ authState: "authenticated", userId: "owner-a", csrfToken: "old-csrf", demoMode: false });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  useAppStore.getState().resetPrivateState();
});

describe("CSRF recovery after a shared browser session changes", () => {
  it("shares one refresh across rejected bulk moves and preserves every idempotency key and body", async () => {
    let resolveIdentity!: (response: Response) => void;
    const identity = new Promise<Response>((resolve) => { resolveIdentity = resolve; });
    const fetchMock = vi.fn(async (path: string, init: RequestInit) => {
      if (path === "/api/v1/auth/me") return identity;
      return new Headers(init.headers).get("X-CSRF-Token") === "fresh-csrf" ? json(session) : csrfRejected();
    });
    vi.stubGlobal("fetch", fetchMock);
    const moves = ["a", "b", "c"].map((id) => api.moveSession(id, "destination", "old-csrf"));
    await vi.waitFor(() => expect(fetchMock.mock.calls.filter(([path]) => path === "/api/v1/auth/me")).toHaveLength(1));
    resolveIdentity(json(freshIdentity));
    const moved = await Promise.all(moves);

    expect(moved.every((row) => row.workspaceId === "destination")).toBe(true);
    expect(useAppStore.getState().csrfToken).toBe("fresh-csrf");
    expect(fetchMock.mock.calls.filter(([path]) => path === "/api/v1/auth/me")).toHaveLength(1);
    for (const id of ["a", "b", "c"]) {
      const requests = fetchMock.mock.calls.filter(([path]) => path === `/api/v1/sessions/${id}`);
      expect(requests).toHaveLength(2);
      const [first, retry] = requests.map(([, init]) => init);
      expect(new Headers(first.headers).get("X-CSRF-Token")).toBe("old-csrf");
      expect(retry).toEqual({ ...first, headers: { ...first.headers, "X-CSRF-Token": "fresh-csrf" } });
    }
    expect(fetchMock.mock.calls.find(([path]) => path === "/api/v1/auth/me")?.[1]).toEqual(expect.objectContaining({ credentials: "same-origin", cache: "no-store" }));
  });

  it("preserves deletion confirmation and replays at most once even if the new token is rejected", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce(csrfRejected()).mockResolvedValueOnce(json(freshIdentity)).mockResolvedValueOnce(csrfRejected());
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.deleteSessionFromHermes("session-a", "stored-a", "old-csrf")).rejects.toMatchObject({ status: 403 });
    expect(fetchMock).toHaveBeenCalledTimes(3);
    const first = fetchMock.mock.calls[0][1] as RequestInit;
    const retry = fetchMock.mock.calls[2][1] as RequestInit;
    expect(retry).toEqual({ ...first, headers: { ...first.headers, "X-CSRF-Token": "fresh-csrf" } });
    expect(new Headers(retry.headers).get("X-Confirm-Delete")).toBe("stored-a");
  });

  it.each([403, 409, 500, 503])("does not replay other HTTP %s failures", async (status) => {
    const fetchMock = vi.fn().mockResolvedValue(json({ detail: status === 403 ? "Permission denied" : "Invalid CSRF token" }, status));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.moveSession("session-a", "destination", "old-csrf")).rejects.toMatchObject({ status });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("never replays a network failure whose outcome is unknown", async () => {
    const fetchMock = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.moveSession("session-a", "destination", "old-csrf")).rejects.toThrow("Failed to fetch");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("invalidates the old identity without retrying as another account", async () => {
    const unauthorized = vi.fn();
    window.addEventListener("hermes-control:unauthorized", unauthorized);
    const fetchMock = vi.fn().mockResolvedValueOnce(csrfRejected()).mockResolvedValueOnce(json({ ...freshIdentity, id: "owner-b" }));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(api.moveSession("session-a", "destination", "old-csrf")).rejects.toMatchObject({ status: 403 });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(unauthorized).toHaveBeenCalledOnce();
      expect(useAppStore.getState().csrfToken).toBe("old-csrf");
    } finally {
      window.removeEventListener("hermes-control:unauthorized", unauthorized);
    }
  });

  it("handles an expired session during refresh without sending the mutation again", async () => {
    const unauthorized = vi.fn();
    window.addEventListener("hermes-control:unauthorized", unauthorized);
    const fetchMock = vi.fn().mockResolvedValueOnce(csrfRejected()).mockResolvedValueOnce(json({ detail: "Authentication required" }, 401));
    vi.stubGlobal("fetch", fetchMock);
    try {
      await expect(api.moveSession("session-a", "destination", "old-csrf")).rejects.toMatchObject({ status: 403 });
      expect(fetchMock).toHaveBeenCalledTimes(2);
      expect(unauthorized).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener("hermes-control:unauthorized", unauthorized);
    }
  });

  it("does not restore credentials or retry when the user signs out during refresh", async () => {
    let resolveIdentity!: (response: Response) => void;
    const identity = new Promise<Response>((resolve) => { resolveIdentity = resolve; });
    const fetchMock = vi.fn().mockResolvedValueOnce(csrfRejected()).mockReturnValueOnce(identity);
    vi.stubGlobal("fetch", fetchMock);
    const outcome = api.moveSession("session-a", "destination", "old-csrf").catch((error: unknown) => error);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    useAppStore.getState().setAuth("unauthenticated");
    resolveIdentity(json(freshIdentity));
    expect(await outcome).toMatchObject({ status: 403 });
    expect(useAppStore.getState().csrfToken).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not refresh a token from a callback that no longer matches the active auth state", async () => {
    useAppStore.setState({ userId: "owner-b", csrfToken: "other-owner-csrf" });
    const fetchMock = vi.fn().mockResolvedValue(csrfRejected());
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.moveSession("session-a", "destination", "old-csrf")).rejects.toMatchObject({ status: 403 });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(useAppStore.getState().csrfToken).toBe("other-owner-csrf");
  });

  it("does not replay an old action after logout and a new login to the same account", async () => {
    let resolveIdentity!: (response: Response) => void;
    const identity = new Promise<Response>((resolve) => { resolveIdentity = resolve; });
    const fetchMock = vi.fn().mockResolvedValueOnce(csrfRejected()).mockReturnValueOnce(identity);
    vi.stubGlobal("fetch", fetchMock);
    const outcome = api.moveSession("session-a", "destination", "old-csrf").catch((error: unknown) => error);
    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    useAppStore.getState().setAuth("unauthenticated");
    useAppStore.getState().setAuth("authenticated", "Owner A", "new-login-csrf", false, "owner-a");
    resolveIdentity(json(freshIdentity));
    expect(await outcome).toMatchObject({ status: 403 });
    expect(useAppStore.getState().csrfToken).toBe("new-login-csrf");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
