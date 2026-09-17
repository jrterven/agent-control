import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ConnectorView } from "@hermes-control/shared-types";
import { CloudInstallerOptions } from "../components/CloudInstallerOptions";
import { ConnectorReadiness } from "../components/ConnectorReadiness";
import { api } from "../lib/api";
import { parseManagedDownloads } from "../lib/managedDownloads";
import { useCloudConfigurationStore } from "../lib/cloud";
import { ConnectorsScreen } from "../screens/CloudScreens";
import { useAppStore } from "../store/appStore";
import { gateways, profiles, sessions } from "../data";
import type { BootstrapData } from "../types";
import i18n from "../i18n";

const navigate = vi.hoisted(() => vi.fn());
vi.mock("@tanstack/react-router", () => ({
  useNavigate: () => navigate,
  Link: ({ children, to, ...props }: { children: ReactNode; to: string }) => <a href={to} {...props}>{children}</a>,
}));

const origin = window.location.origin;
const manifest = {
  schemaVersion: 1, version: "a".repeat(40),
  downloads: { macosArm64: { url: "/downloads/agent-control/releases/release/Agent-Control.dmg", sha256: "b".repeat(64), minOsVersion: "13" }, linux: { installerUrl: "/downloads/agent-control/install.sh" } },
};
const computer: ConnectorView = { id: "computer-1", name: "Mi equipo", status: "offline", version: "0.2.0", profiles: ["default"], gatewayId: gateways[0].id, lastSeenAt: null };
const readyProfile = { ...profiles[0], mutable: true };
const empty: BootstrapData = { gateways: [], profiles: [], sessions: [], workspaces: [], automations: [] };

beforeEach(async () => {
  await i18n.changeLanguage("es");
  navigate.mockReset();
  useAppStore.getState().resetPrivateState();
  useAppStore.setState({ authState: "authenticated", csrfToken: "csrf", connection: "connected", demoMode: false });
  useCloudConfigurationStore.setState({ methods: { mode: "cloud", googleEnabled: true } });
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false }));
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); useCloudConfigurationStore.setState({ methods: undefined }); });

describe("published managed installers", () => {
  it("keeps unpublished downloads unavailable and preserves the existing connector command", async () => {
    const user = userEvent.setup();
    render(<CloudInstallerOptions existingCommand="existing-safe-connector-command" loading={false} />);
    expect(await screen.findAllByText(/Este instalador aún no está publicado/)).toHaveLength(2);
    expect(screen.queryByRole("link", { name: /Descargar para Mac/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/curl/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: /Conectar mi Hermes existente/ }));
    expect(screen.getByText("existing-safe-connector-command")).toBeInTheDocument();
    expect(screen.getByText(/Google te identifica/)).toBeInTheDocument();
    expect(screen.getByText(/OpenRouter, OpenAI, Anthropic o Gemini/)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("offers validated DMG and Linux downloads without replacing the existing installer", async () => {
    vi.mocked(fetch).mockResolvedValue({ ok: true, json: async () => manifest } as Response);
    render(<CloudInstallerOptions existingCommand="old-connector-command" loading={false} />);
    expect(await screen.findByRole("link", { name: "Descargar para Mac (.dmg)" })).toHaveAttribute("href", `${origin}${manifest.downloads.macosArm64.url}`);
    expect(screen.getByText(`curl --proto '=https' --tlsv1.2 -fsSL ${origin}/downloads/agent-control/install.sh | sh -s -- --server ${origin}`)).toBeInTheDocument();
    expect(screen.getByText(/macOS 13 o posterior/)).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith("/downloads/agent-control/latest.json", expect.objectContaining({ cache: "no-store", credentials: "omit", redirect: "error" }));
  });

  it.each([
    ["external host", "https://untrusted.example/downloads/agent-control/a.dmg"],
    ["escaped directory", "/downloads/agent-control/../connector/a.dmg"],
    ["insecure host", "http://untrusted.example/downloads/agent-control/a.dmg"],
    ["query", "/downloads/agent-control/a.dmg?next=bad"],
  ])("does not offer a manifest with an unsafe %s download", (_kind, url) => {
    const parsed = parseManagedDownloads({ ...manifest, downloads: { macosArm64: { ...manifest.downloads.macosArm64, url } } }, origin);
    expect(parsed?.macosArm64).toBeUndefined();
  });

  it("requires a versioned manifest and a checksum for the Mac package", () => {
    expect(parseManagedDownloads({ ...manifest, schemaVersion: 2 }, origin)).toBeNull();
    expect(parseManagedDownloads({ ...manifest, version: "latest" }, origin)).toBeNull();
    expect(parseManagedDownloads({ ...manifest, downloads: { macosArm64: { ...manifest.downloads.macosArm64, sha256: "" } } }, origin)?.macosArm64).toBeUndefined();
  });
});

describe("paired computer readiness", () => {
  it("polls connector, Hermes and agent readiness without creating chats or sending prompts", async () => {
    vi.useFakeTimers();
    const list = vi.spyOn(api, "connectors").mockResolvedValue({ items: [computer], installCommand: "" });
    const bootstrap = vi.spyOn(api, "bootstrap").mockResolvedValue(empty);
    const create = vi.spyOn(api, "createSession");
    const prompt = vi.spyOn(api, "submitPrompt");
    const { unmount } = render(<ConnectorReadiness computer={computer} />);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(screen.getByRole("status")).toHaveTextContent("Esperando que el conector se conecte");
    list.mockResolvedValue({ items: [{ ...computer, status: "online" }], installCommand: "" });
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(screen.getByRole("status")).toHaveTextContent("Esperando a Hermes");
    bootstrap.mockResolvedValue({ ...empty, gateways: [gateways[0]], profiles: [profiles[0]] });
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(screen.getByRole("status")).toHaveTextContent("todavía no permite conversar");
    bootstrap.mockResolvedValue({ ...empty, gateways: [gateways[0]], profiles: [readyProfile] });
    await act(async () => { await vi.advanceTimersByTimeAsync(3_000); });
    expect(screen.getByRole("status")).toHaveTextContent("listo para conversar");
    expect(screen.getByRole("combobox", { name: "Agente para tu primer chat" })).toHaveValue(readyProfile.id);
    expect(create).not.toHaveBeenCalled();
    expect(prompt).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
    unmount();
  });

  it("opens a blank chat only after the user explicitly chooses it", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "connectors").mockResolvedValue({ items: [{ ...computer, status: "online" }], installCommand: "" });
    vi.spyOn(api, "bootstrap").mockResolvedValue({ ...empty, gateways: [gateways[0]], profiles: [readyProfile] });
    const create = vi.spyOn(api, "createSession").mockResolvedValue({ ...sessions[0], id: "new-chat", workspaceId: undefined, preview: "" });
    const prompt = vi.spyOn(api, "submitPrompt");
    render(<ConnectorReadiness computer={computer} />);
    const open = await screen.findByRole("button", { name: "Abrir un chat nuevo" });
    expect(create).not.toHaveBeenCalled();
    await user.click(open);
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/chats" }));
    expect(create).toHaveBeenCalledWith(readyProfile.id, undefined, "csrf");
    expect(useAppStore.getState().selectedSessionId).toBe("new-chat");
    expect(prompt).not.toHaveBeenCalled();
  });

  it("never treats another computer's profiles as ready and blocks revoked access", async () => {
    const user = userEvent.setup();
    const list = vi.spyOn(api, "connectors").mockResolvedValue({ items: [{ ...computer, status: "online", gatewayId: "other-gateway" }], installCommand: "" });
    vi.spyOn(api, "bootstrap").mockResolvedValue({ ...empty, gateways: [gateways[0]], profiles: [readyProfile] });
    render(<ConnectorReadiness computer={computer} />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Esperando a Hermes"));
    expect(screen.queryByRole("button", { name: "Abrir un chat nuevo" })).not.toBeInTheDocument();
    list.mockResolvedValue({ items: [{ ...computer, status: "revoked" }], installCommand: "" });
    await user.click(screen.getByRole("button", { name: "Actualizar" }));
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("Se revocó el acceso"));
  });

  it("discards a chat creation response after logout instead of adding it to another account", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "connectors").mockResolvedValue({ items: [{ ...computer, status: "online" }], installCommand: "" });
    vi.spyOn(api, "bootstrap").mockResolvedValue({ ...empty, gateways: [gateways[0]], profiles: [readyProfile] });
    let finish!: (session: typeof sessions[number]) => void;
    vi.spyOn(api, "createSession").mockImplementation(() => new Promise((resolve) => { finish = resolve; }));
    render(<ConnectorReadiness computer={computer} />);
    await user.click(await screen.findByRole("button", { name: "Abrir un chat nuevo" }));
    await act(async () => {
      useAppStore.getState().resetPrivateState();
      useAppStore.setState({ authState: "authenticated", csrfToken: "new-account", connection: "connected" });
      finish({ ...sessions[0], id: "private-old-account-session" });
    });
    expect(useAppStore.getState().sessions).toHaveLength(0);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("does not hydrate or navigate after the account changes during a poll", async () => {
    let resolve!: (value: BootstrapData) => void;
    vi.spyOn(api, "connectors").mockResolvedValue({ items: [{ ...computer, status: "online" }], installCommand: "" });
    vi.spyOn(api, "bootstrap").mockImplementation(() => new Promise((done) => { resolve = done; }));
    render(<ConnectorReadiness computer={computer} />);
    await act(async () => {
      useAppStore.setState({ authState: "unauthenticated", csrfToken: undefined });
      resolve({ ...empty, gateways: [gateways[0]], profiles: [readyProfile] });
    });
    expect(useAppStore.getState().profiles).toHaveLength(0);
    expect(navigate).not.toHaveBeenCalled();
  });

  it("recovers from a failed readiness check without automatically creating anything", async () => {
    vi.spyOn(api, "connectors").mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ items: [{ ...computer, status: "online" }], installCommand: "" });
    vi.spyOn(api, "bootstrap").mockResolvedValue({ ...empty, gateways: [gateways[0]], profiles: [readyProfile] });
    render(<ConnectorReadiness computer={computer} />);
    await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("No se pudo comprobar"));
    fireEvent.click(screen.getByRole("button", { name: "Actualizar" }));
    expect(await screen.findByRole("button", { name: "Abrir un chat nuevo" })).toBeInTheDocument();
  });
});

describe("computer installation metadata", () => {
  it("shows reported installation and Hermes versions while supporting older connectors", async () => {
    vi.spyOn(api, "connectors").mockResolvedValue({ items: [{ ...computer, installationKind: "managed", hermesVersion: "0.21.2" }, { ...computer, id: "legacy", name: "Legacy computer" }], installCommand: "" });
    render(<ConnectorsScreen />);
    expect(await screen.findByText("Agent Control con Hermes")).toBeInTheDocument();
    expect(screen.getByText("0.21.2")).toBeInTheDocument();
    expect(screen.getAllByText("Versión de Hermes")).toHaveLength(1);
    expect(screen.getByText("Legacy computer")).toBeInTheDocument();
  });
});
