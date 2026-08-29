import axe from "axe-core";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "../i18n";
import { ChatView } from "../components/ChatView";
import { applyRealtimeEvent } from "../hooks";
import { api, ApiError } from "../lib/api";
import { useAppStore } from "../store/appStore";
import { automations, gateways, profiles, sessions, workspaces } from "../data";

function approvalEvent() {
  return {
    type: "approval.request",
    controlSessionId: "session-papers",
    data: {
      request_id: "approval-1",
      command: "rm -rf /tmp/example",
      description: "Eliminar un directorio temporal",
      pattern_keys: ["recursive_delete"],
      allow_session: true,
      allow_permanent: true,
      choices: ["once", "session", "always", "deny", "unknown"],
    },
  };
}

function batchClarificationEvent() {
  return {
    type: "clarify.request",
    controlSessionId: "session-papers",
    data: {
      request_id: "clarify-batch-1",
      questions: [
        { qid: "q0", question: "¿Entorno?", choices: ["Staging", "Producción"], multi_select: true },
        { qid: "q1", question: "¿Motivo?", choices: null, multi_select: false },
      ],
    },
  };
}

describe("official approval and clarification gates", () => {
  beforeEach(() => {
    useAppStore.getState().resetPrivateState();
    useAppStore.setState({
      authState: "authenticated",
      csrfToken: "csrf-memory-only",
      demoMode: false,
      selectedProfileId: "profile-control-dev",
      selectedSessionId: "session-papers",
      selectedGatewayId: "gateway-home",
      selectedWorkspaceId: "workspace-papers",
      gateways,
      profiles: profiles.map((profile) => profile.id === "profile-control-dev" ? {
        ...profile,
        capabilities: { ...gateways[0].capabilities, approvals: true, clarifications: true },
        capabilitySet: {
          protocol: "dashboard-rpc",
          version: "0.20.5",
          sourceSha: "791e2ae",
          methods: ["approval.respond", "clarify.respond"],
          features: [],
        },
      } : profile),
      sessions: sessions.map((session) => session.id === "session-papers" ? {
        ...session,
        gatewayId: "gateway-home",
        profileName: "control-dev",
        profileId: "profile-control-dev",
      } : session),
      workspaces,
      automations,
      messages: [],
      streamingBySession: { "session-papers": "assistant-stream" },
      approvalsBySession: {},
      clarificationsBySession: {},
    });
  });

  afterEach(() => vi.restoreAllMocks());

  it("stores request events without a message correlation and removes expired or terminal requests", () => {
    expect(applyRealtimeEvent(approvalEvent())).toBe(true);
    expect(applyRealtimeEvent(approvalEvent())).toBe(true);
    expect(useAppStore.getState().approvalsBySession["session-papers"]).toHaveLength(1);
    expect(useAppStore.getState().approvalsBySession["session-papers"][0]).toMatchObject({
      requestId: "approval-1",
      choices: ["once", "session", "always", "deny"],
      patternKeys: ["recursive_delete"],
    });

    expect(applyRealtimeEvent(batchClarificationEvent())).toBe(true);
    expect(useAppStore.getState().clarificationsBySession["session-papers"][0]).toMatchObject({
      requestId: "clarify-batch-1",
      batch: true,
      remainingQuestionIds: ["q0", "q1"],
      questions: [
        { questionId: "q0", question: "¿Entorno?" },
        { questionId: "q1", question: "¿Motivo?" },
      ],
    });

    expect(applyRealtimeEvent({
      type: "clarify.expire",
      controlSessionId: "session-papers",
      data: { request_id: "clarify-batch-1" },
    })).toBe(true);
    expect(useAppStore.getState().clarificationsBySession["session-papers"]).toBeUndefined();

    expect(applyRealtimeEvent({ type: "message.complete", controlSessionId: "session-papers", data: {} })).toBe(true);
    expect(useAppStore.getState().approvalsBySession["session-papers"]).toBeUndefined();
  });

  it("renders an accessible approval card and sends an official choice", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "respondApproval").mockResolvedValue({ requestId: "approval-1", resolved: 1, status: "resolved" });
    applyRealtimeEvent(approvalEvent());

    const { container } = render(<ChatView />);
    expect(screen.getByRole("heading", { name: "Aprobación requerida" })).toBeInTheDocument();
    expect(screen.getByText("rm -rf /tmp/example")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "unknown" })).not.toBeInTheDocument();
    expect((await axe.run(container)).violations).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Permitir una vez" }));
    await waitFor(() => expect(api.respondApproval).toHaveBeenCalledWith(
      "session-papers",
      "approval-1",
      "once",
      "csrf-memory-only",
    ));
    await waitFor(() => expect(screen.queryByRole("heading", { name: "Aprobación requerida" })).not.toBeInTheDocument());
  });

  it("allows an operator-authorized non-default profile with the exact capability", () => {
    useAppStore.setState({
      selectedProfileId: "profile-newton",
      profiles: profiles.map((profile) => profile.id === "profile-newton" ? {
        ...profile,
        mutable: true,
        capabilities: { ...gateways[0].capabilities, approvals: true },
        capabilitySet: {
          protocol: "dashboard-rpc",
          version: "0.20.5",
          sourceSha: "791e2ae",
          methods: ["approval.respond"],
          features: [],
        },
      } : profile),
      sessions: sessions.map((session) => session.id === "session-papers" ? {
        ...session,
        gatewayId: "gateway-home",
        profileName: "default",
        profileId: "profile-newton",
      } : session),
    });
    applyRealtimeEvent(approvalEvent());
    render(<ChatView />);

    expect(screen.getByRole("button", { name: "Permitir una vez" })).toBeEnabled();
    expect(screen.queryByText(/requiere un perfil habilitado por el backend/i)).not.toBeInTheDocument();
  });

  it("keeps controls disabled when the backend marks the profile read-only", () => {
    useAppStore.setState({
      profiles: useAppStore.getState().profiles.map((profile) => profile.id === "profile-control-dev" ? {
        ...profile,
        mutable: false,
      } : profile),
    });
    applyRealtimeEvent(approvalEvent());
    render(<ChatView />);

    expect(screen.getByRole("button", { name: "Permitir una vez" })).toBeDisabled();
    expect(screen.getByText(/requiere un perfil habilitado por el backend/i)).toBeInTheDocument();
  });

  it("blocks an ambiguous mutation until realtime replays the pending request", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "respondApproval").mockRejectedValue(new ApiError(
      409,
      "Mutation outcome is unknown; reconcile before retrying",
      "MUTATION_DELIVERY_UNKNOWN",
    ));
    applyRealtimeEvent(approvalEvent());
    render(<ChatView />);

    await user.click(screen.getByRole("button", { name: "Permitir una vez" }));
    expect(await screen.findByText(/resultado no está confirmado/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Permitir una vez" })).toBeDisabled();
    expect(api.respondApproval).toHaveBeenCalledTimes(1);

    applyRealtimeEvent(approvalEvent());
    await waitFor(() => expect(screen.getByRole("button", { name: "Permitir una vez" })).toBeEnabled());
  });

  it("answers a single clarification without a question id", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "respondClarification").mockResolvedValue({ requestId: "clarify-single-1", status: "ok", remaining: [] });
    applyRealtimeEvent({
      type: "clarify.request",
      controlSessionId: "session-papers",
      data: {
        request_id: "clarify-single-1",
        question: "¿Qué entorno debo usar?",
        choices: ["Staging", "Producción"],
      },
    });
    render(<ChatView />);

    const question = screen.getByRole("heading", { name: "¿Qué entorno debo usar?" }).closest(".clarification-question");
    expect(question).not.toBeNull();
    await user.click(within(question as HTMLElement).getByRole("radio", { name: "Staging" }));
    await user.click(within(question as HTMLElement).getByRole("button", { name: "Responder" }));

    await waitFor(() => expect(api.respondClarification).toHaveBeenCalledWith(
      "session-papers",
      "clarify-single-1",
      "Staging",
      undefined,
      "csrf-memory-only",
    ));
    await waitFor(() => expect(useAppStore.getState().clarificationsBySession["session-papers"]).toBeUndefined());
  });

  it("supports Hermes' official free-text Other row alongside multi-select choices", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "respondClarification").mockResolvedValue({
      requestId: "clarify-other-1",
      status: "ok",
      remaining: [],
    });
    applyRealtimeEvent({
      type: "clarify.request",
      controlSessionId: "session-papers",
      data: {
        request_id: "clarify-other-1",
        question: "¿Qué canales debo habilitar?",
        choices: ["Web", "Terminal"],
        multi_select: true,
      },
    });
    render(<ChatView />);

    const question = screen.getByRole("heading", { name: "¿Qué canales debo habilitar?" }).closest(".clarification-question");
    expect(question).not.toBeNull();
    await user.click(within(question as HTMLElement).getByRole("button", { name: "Web" }));
    await user.click(within(question as HTMLElement).getByRole("button", { name: "Otra respuesta" }));
    await user.type(within(question as HTMLElement).getByRole("textbox", { name: "Otra respuesta: ¿Qué canales debo habilitar?" }), "Móvil");
    await user.click(within(question as HTMLElement).getByRole("button", { name: "Responder" }));

    await waitFor(() => expect(api.respondClarification).toHaveBeenCalledWith(
      "session-papers",
      "clarify-other-1",
      ["Web", "Móvil"],
      undefined,
      "csrf-memory-only",
    ));
  });

  it("locks batch answers by official qid until all questions are complete", async () => {
    const user = userEvent.setup();
    vi.spyOn(api, "respondClarification")
      .mockResolvedValueOnce({ requestId: "clarify-batch-1", status: "ok", remaining: ["q1"] })
      .mockResolvedValueOnce({ requestId: "clarify-batch-1", status: "ok", remaining: [] });
    applyRealtimeEvent(batchClarificationEvent());
    render(<ChatView />);

    const firstQuestion = screen.getByRole("heading", { name: "¿Entorno?" }).closest(".clarification-question");
    expect(firstQuestion).not.toBeNull();
    await user.click(within(firstQuestion as HTMLElement).getByRole("button", { name: "Staging" }));
    await user.click(within(firstQuestion as HTMLElement).getByRole("button", { name: "Producción" }));
    await user.click(within(firstQuestion as HTMLElement).getByRole("button", { name: "Confirmar respuesta" }));
    await waitFor(() => expect(api.respondClarification).toHaveBeenNthCalledWith(
      1,
      "session-papers",
      "clarify-batch-1",
      ["Staging", "Producción"],
      "q0",
      "csrf-memory-only",
    ));
    expect(await screen.findByText("Respondida")).toBeInTheDocument();

    const secondQuestion = screen.getByRole("heading", { name: "¿Motivo?" }).closest(".clarification-question");
    expect(secondQuestion).not.toBeNull();
    await user.type(within(secondQuestion as HTMLElement).getByRole("textbox"), "Validación previa");
    await user.click(within(secondQuestion as HTMLElement).getByRole("button", { name: "Confirmar respuesta" }));
    await waitFor(() => expect(api.respondClarification).toHaveBeenNthCalledWith(
      2,
      "session-papers",
      "clarify-batch-1",
      "Validación previa",
      "q1",
      "csrf-memory-only",
    ));
    await waitFor(() => expect(useAppStore.getState().clarificationsBySession["session-papers"]).toBeUndefined());
  });
});
