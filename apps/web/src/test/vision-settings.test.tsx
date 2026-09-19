import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { VisionPreferences } from "@hermes-control/shared-types";
import { VisionSettings } from "../components/VisionSettings";
import i18n from "../i18n";
import { visionApi, VISION_PREFERENCES_CHANGED } from "../lib/vision";
import { useAppStore } from "../store/appStore";

const preferences: VisionPreferences = { modelId: "gpt-5.6-luna", intervalSeconds: 5, configured: true };
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

describe("camera preferences", () => {
  beforeEach(async () => {
    await i18n.changeLanguage("es");
    useAppStore.setState({ authState: "authenticated", userId: "owner-a", authGeneration: 1, demoMode: false, csrfToken: "csrf-a", features: undefined });
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(true);
    vi.spyOn(visionApi, "preferences").mockResolvedValue(preferences);
    vi.spyOn(visionApi, "savePreferences").mockImplementation(async (input) => ({ ...input, configured: true }));
  });
  afterEach(() => vi.restoreAllMocks());

  it("saves only the camera model and interval without inference or credential storage", async () => {
    const user = userEvent.setup();
    const event = vi.fn();
    const intent = vi.spyOn(visionApi, "intent");
    const analyze = vi.spyOn(visionApi, "analyze");
    const storage = vi.spyOn(Storage.prototype, "setItem");
    window.addEventListener(VISION_PREFERENCES_CHANGED, event);
    render(<VisionSettings />);
    const model = screen.getByRole("combobox", { name: "Modelo de la cámara" });
    await waitFor(() => expect(model).toBeEnabled());
    expect(model).toHaveValue("gpt-5.6-luna");
    expect(screen.getByRole("combobox", { name: "Intervalo de observación continua" })).toHaveValue("5");
    expect(screen.getByRole("button", { name: "Guardar preferencias de cámara" })).toBeDisabled();
    await user.selectOptions(model, "gpt-5.6-terra");
    await user.selectOptions(screen.getByRole("combobox", { name: "Intervalo de observación continua" }), "10");
    await user.click(screen.getByRole("button", { name: "Guardar preferencias de cámara" }));
    expect(visionApi.savePreferences).toHaveBeenCalledWith({ modelId: "gpt-5.6-terra", intervalSeconds: 10 }, "csrf-a", expect.any(AbortSignal));
    expect(await screen.findByText(/Preferencias de cámara guardadas/)).toBeVisible();
    expect(event).toHaveBeenCalledTimes(1);
    expect(intent).not.toHaveBeenCalled();
    expect(analyze).not.toHaveBeenCalled();
    expect(storage).not.toHaveBeenCalled();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    window.removeEventListener(VISION_PREFERENCES_CHANGED, event);
  });

  it("allows preference changes without a key and points to the existing OpenAI connection", async () => {
    vi.mocked(visionApi.preferences).mockResolvedValue({ ...preferences, configured: false });
    render(<VisionSettings />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Modelo de la cámara" })).toBeEnabled());
    expect(screen.getByText("Necesitas una clave de OpenAI para usar la cámara")).toBeVisible();
    expect(screen.getByRole("link", { name: "Administrar la conexión de OpenAI" })).toHaveAttribute("href", "#openai-integration-title");
    expect(screen.getByText(/consume tu cuota de OpenAI/)).toBeVisible();
    expect(screen.getByText(/no garantiza que el proveedor no conserve datos/)).toBeVisible();
  });

  it("keeps the edited choice and gives a safe error after a failed save", async () => {
    const user = userEvent.setup();
    vi.mocked(visionApi.savePreferences).mockRejectedValue(new Error("secret-provider-debug-payload"));
    render(<VisionSettings />);
    const model = screen.getByRole("combobox", { name: "Modelo de la cámara" });
    await waitFor(() => expect(model).toBeEnabled());
    await user.selectOptions(model, "gpt-5.6-sol");
    await user.click(screen.getByRole("button", { name: "Guardar preferencias de cámara" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudieron guardar");
    expect(model).toHaveValue("gpt-5.6-sol");
    expect(screen.getByRole("button", { name: "Guardar preferencias de cámara" })).toBeEnabled();
    expect(screen.queryByText(/secret-provider/)).not.toBeInTheDocument();
    expect(visionApi.savePreferences).toHaveBeenCalledTimes(1);
  });

  it("does not load or save while offline", async () => {
    vi.spyOn(navigator, "onLine", "get").mockReturnValue(false);
    render(<VisionSettings />);
    expect(screen.getByRole("combobox", { name: "Modelo de la cámara" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Guardar preferencias de cámara" })).toBeDisabled();
    expect(screen.getByText("Conéctate para guardar las preferencias de cámara.")).toBeVisible();
    expect(visionApi.preferences).not.toHaveBeenCalled();
    expect(visionApi.savePreferences).not.toHaveBeenCalled();
  });

  it("retries a failed preference read only after the retry action", async () => {
    const user = userEvent.setup();
    vi.mocked(visionApi.preferences).mockRejectedValueOnce(new Error("load failed"));
    render(<VisionSettings />);
    expect(await screen.findByRole("alert")).toHaveTextContent("No se pudieron cargar");
    expect(visionApi.preferences).toHaveBeenCalledTimes(1);
    await user.click(screen.getByRole("button", { name: "Reintentar carga" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Modelo de la cámara" })).toBeEnabled());
    expect(visionApi.preferences).toHaveBeenCalledTimes(2);
  });

  it("ignores an old account's late load and resets immediately when ownership changes", async () => {
    const oldLoad = deferred<VisionPreferences>();
    vi.mocked(visionApi.preferences).mockReturnValueOnce(oldLoad.promise).mockResolvedValueOnce({ ...preferences, modelId: "gpt-5.6-terra" });
    render(<VisionSettings />);
    const oldSignal = vi.mocked(visionApi.preferences).mock.calls[0][0];
    act(() => useAppStore.setState({ userId: "owner-b", authGeneration: 2, csrfToken: "csrf-b" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Modelo de la cámara" })).toHaveValue("gpt-5.6-terra"));
    expect(oldSignal?.aborted).toBe(true);
    await act(async () => oldLoad.resolve({ ...preferences, modelId: "gpt-5.6-sol" }));
    expect(screen.getByRole("combobox", { name: "Modelo de la cámara" })).toHaveValue("gpt-5.6-terra");
  });

  it("does not publish an old account's completed save or leak its choice", async () => {
    const user = userEvent.setup();
    const oldSave = deferred<VisionPreferences>();
    const changed = vi.fn();
    vi.mocked(visionApi.savePreferences).mockReturnValue(oldSave.promise);
    window.addEventListener(VISION_PREFERENCES_CHANGED, changed);
    render(<VisionSettings />);
    const model = screen.getByRole("combobox", { name: "Modelo de la cámara" });
    await waitFor(() => expect(model).toBeEnabled());
    await user.selectOptions(model, "gpt-5.6-sol");
    await user.click(screen.getByRole("button", { name: "Guardar preferencias de cámara" }));
    const saveSignal = vi.mocked(visionApi.savePreferences).mock.calls[0][2];
    act(() => useAppStore.setState({ userId: "owner-b", authGeneration: 2, csrfToken: "csrf-b" }));
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Modelo de la cámara" })).toBeEnabled());
    await act(async () => oldSave.resolve({ ...preferences, modelId: "gpt-5.6-sol" }));
    expect(saveSignal?.aborted).toBe(true);
    expect(screen.getByRole("combobox", { name: "Modelo de la cámara" })).toHaveValue("gpt-5.6-luna");
    expect(screen.queryByText(/Preferencias de cámara guardadas/)).not.toBeInTheDocument();
    expect(changed).not.toHaveBeenCalled();
    window.removeEventListener(VISION_PREFERENCES_CHANGED, changed);
  });

  it("renders English labels after language changes", async () => {
    await i18n.changeLanguage("en");
    render(<VisionSettings />);
    await waitFor(() => expect(screen.getByRole("combobox", { name: "Camera model" })).toBeEnabled());
    expect(screen.getByRole("combobox", { name: "Continuous observation interval" })).toBeVisible();
    expect(screen.getByText(/does not guarantee zero provider retention/)).toBeVisible();
  });
});
