import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, afterEach, expect, it, vi } from "vitest";
import { PyannoteSettings } from "../components/PyannoteSettings";
import { useAppStore } from "../store/appStore";

const summary = { jobs: 0, succeeded: 0, reviewed: 0, correct: 0, falseMatches: 0, unknown: 0, inconclusive: 0, latencyMs: { totalMs: { p50: null, p95: null } } };
let configured: boolean;
let enabled: boolean;
let tested: boolean;
let people: { id: string; name: string; ready: boolean }[];
let fetcher: ReturnType<typeof vi.fn>;
const result = (value: unknown) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
beforeEach(() => {
  configured = enabled = tested = false; people = [];
  useAppStore.setState({ authState: "authenticated", userId: "owner", csrfToken: "csrf", demoMode: false });
  fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("pyannote/key")) configured = init?.method !== "DELETE";
    if (url.endsWith("pyannote/test")) tested = true;
    if (url.endsWith("pyannote") && init?.method === "PUT") enabled = JSON.parse(init.body as string).enabled;
    if (/\/people\//.test(url) && init?.method === "PUT") people.push({ id: url.split("/").at(-1)!, name: JSON.parse(init.body as string).name, ready: false });
    if (url.endsWith("/people")) return result({ items: people });
    if (url.endsWith("/metrics")) return result({ summary, windows: { 5: summary, 10: summary, 20: summary }, estimatedBillableSeconds: 0, voiceprintsCreated: 0, unresolvedCharges: 0, recent: [] });
    return result({ configured, enabled, connectionTested: tested, recognitionTested: false, readyPeople: 0, generation: "v", windowSeconds: 5 });
  });
  vi.stubGlobal("fetch", fetcher);
});
afterEach(() => vi.unstubAllGlobals());

it("requires explicit key and enable actions, clears the key field and uses the no-audio connection test", async () => {
  render(<PyannoteSettings />);
  await screen.findByText("Sin clave guardada", { exact: false });
  expect(screen.getByLabelText("Habilitar reconocimiento de personas")).not.toBeChecked();
  const input = screen.getByLabelText("API key de pyannoteAI");
  fireEvent.change(input, { target: { value: "private-key" } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar / reemplazar clave" }));
  await waitFor(() => expect(screen.getByLabelText("Habilitar reconocimiento de personas")).toBeEnabled());
  expect(input).toHaveValue("");
  fireEvent.click(screen.getByRole("button", { name: "Probar conexión" }));
  await screen.findByText(/Conexión comprobada/);
  expect(fetcher.mock.calls.some(([url]) => String(url).includes("/captures/") || String(url).includes("/jobs/"))).toBe(false);
  expect(screen.getByLabelText("Habilitar reconocimiento de personas")).not.toBeChecked();
});

it("requires name and consent before creating a person; it does not acquire audio automatically", async () => {
  const mic = vi.fn(); Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: mic } });
  render(<PyannoteSettings />);
  fireEvent.change(screen.getByLabelText("Nombre"), { target: { value: "Juan Ramón" } });
  expect(screen.getByRole("button", { name: "Añadir persona" })).toBeDisabled();
  fireEvent.click(screen.getByLabelText(/Esta persona acepta/));
  fireEvent.click(screen.getByRole("button", { name: "Añadir persona" }));
  await screen.findByText("Falta grabación");
  expect(people[0].name).toBe("Juan Ramón");
  expect(mic).not.toHaveBeenCalled();
  expect(screen.getByRole("button", { name: "Empezar grabación" })).toBeDisabled();
});

it("clears the UI on logout without exposing a previous account's catalog", async () => {
  people = [{ id: "juan", name: "Juan Ramón", ready: true }]; configured = true;
  const { rerender } = render(<PyannoteSettings />);
  await screen.findByDisplayValue("Juan Ramón");
  act(() => useAppStore.setState({ authState: "unauthenticated", userId: undefined })); rerender(<PyannoteSettings />);
  expect(screen.queryByDisplayValue("Juan Ramón")).not.toBeInTheDocument();
});
