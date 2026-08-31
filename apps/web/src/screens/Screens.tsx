import { zodResolver } from "@hookform/resolvers/zod";
import {
  ArrowClockwise, ArrowRight, CheckCircle, Clock, CloudCheck, Code,
  Database, DownloadSimple, FileText, FolderOpen, Gauge, GearSix, HardDrives,
  Key, Lightning, MagnifyingGlass, Plus, Robot, ShieldCheck, SpinnerGap, Translate,
  PencilSimple, Play, Pulse, SlidersHorizontal, TerminalWindow, Trash, UserCircle, WarningCircle,
} from "@phosphor-icons/react";
import { Link, useNavigate } from "@tanstack/react-router";
import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useMemo, useRef, useState } from "react";
import { useForm } from "react-hook-form";
import { useTranslation } from "react-i18next";
import { z } from "zod";
import { Badge, Button, Field, Panel, StatusDot, Switch, cx } from "@hermes-control/ui";
import { api, ApiError, type AutomationUpdateInput, type ReadinessView } from "../lib/api";
import { clearPrivateCache, clearTranscriptCache, savePreference } from "../lib/db";
import { createAndProvisionGateway } from "../lib/gatewayProvisioning";
import { buildSearchResults } from "../lib/search";
import { useOverlayDialog } from "../lib/useOverlayDialog";
import { useAppStore } from "../store/appStore";
import type { Automation, AutomationRun, Gateway, Profile, SearchResult, SessionSummary, ThemePreference } from "../types";
import { BrandMark } from "../components/BrandMark";
import { ProfileAvatar } from "../components/ProfileAvatar";
import { InteractionCards } from "../components/ChatView";
import { AdminConfigScreen } from "../components/AdminConfigScreen";
import { ElevenLabsIntegration } from "../components/ElevenLabsIntegration";
import i18n from "../i18n";
import { useLanguagePreference } from "../hooks/useLanguagePreference";
import { APP_VERSION, checkForPwaUpdate, hasPwaUpdateBlockers, requestPwaUpdate, usePwaUpdateStore } from "../lib/pwaUpdate";
import { prepareProfileAvatar } from "../lib/profileAvatar";

export function PageHeader({ eyebrow, title, description, action }: { eyebrow: string; title: string; description: string; action?: React.ReactNode }) {
  return <header className="page-header"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1><p>{description}</p></div>{action}</header>;
}

type LoginValues = { username: string; password: string };

export function LoginScreen() {
  const { t } = useTranslation();
  const setAuth = useAppStore((state) => state.setAuth);
  const [serverError, setServerError] = useState("");
  const loginSchema = useMemo(() => z.object({
    username: z.string().min(1, t("login.usernameRequired")),
    password: z.string().min(12, t("login.passwordLength")),
  }), [t]);
  const { register, handleSubmit, formState: { errors, isSubmitting } } = useForm<LoginValues>({ resolver: zodResolver(loginSchema), defaultValues: { username: "", password: "" } });
  const onSubmit = handleSubmit(async (values) => {
    setServerError("");
    try {
      const user = await api.login(values.username, values.password);
      setAuth("authenticated", user.name, user.csrfToken, false);
    } catch {
      setServerError(t("login.error"));
    }
  });
  return (
    <main className="login-screen">
      <section className="login-card" aria-labelledby="login-title">
        <div className="login-brand"><BrandMark size="lg" label="Agent Control" /><div><strong>Agent</strong><span>Control</span></div></div>
        <span className="eyebrow">{t("login.protected")}</span>
        <h1 id="login-title">{t("login.title")}</h1>
        <p>{t("login.description")}</p>
        <form onSubmit={onSubmit} noValidate>
          <Field label={t("login.username")} autoComplete="username" error={errors.username?.message} {...register("username")} />
          <Field label={t("login.password")} type="password" autoComplete="current-password" error={errors.password?.message} {...register("password")} />
          {serverError ? <p className="form-error" role="alert"><WarningCircle /> {serverError}</p> : null}
          <Button type="submit" variant="primary" disabled={isSubmitting}>{isSubmitting ? t("login.checking") : t("login.submit")}</Button>
        </form>
        <footer><ShieldCheck size={18} /> {t("login.footer")}</footer>
      </section>
    </main>
  );
}

const administrativeWriteMethods = new Set([
  "session.delete", "cron.create", "cron.update", "cron.delete", "cron.trigger",
  "models.set", "config.set", "soul.set", "memory.provider.set", "memory.reset",
  "skills.toggle", "toolsets.toggle", "mcp.create", "mcp.delete", "mcp.toggle",
  "channels.update", "secrets.set", "secrets.delete",
]);

function profileWritePolicy(profile: Profile, t: (key: string) => string) {
  if (!profile.mutable) return t("agentsPage.readOnly");
  if (profile.capabilitySet?.methods.some((method) => administrativeWriteMethods.has(method))) {
    return t("agentsPage.conversationAdmin");
  }
  return t("agentsPage.fullConversation");
}

type AgentCreateValues = {
  gatewayId: string;
  technicalName: string;
  displayName: string;
  description: string;
};

const MAX_AGENT_BRIEF_CHARACTERS = 200_000;

function normalizeAgentTechnicalName(value: string) {
  return value.trim().toLowerCase().replace(/[\s_]+/g, "-").replace(/-+/g, "-");
}

const emptyAgentCreateValues: AgentCreateValues = {
  gatewayId: "",
  technicalName: "",
  displayName: "",
  description: "",
};

type AgentCreationPhase = "idle" | "creatingProfile" | "creatingSession" | "configuring" | "finalizing" | "error";

type PendingAgentSetup = {
  profile?: Profile;
  avatarUploaded?: boolean;
  setupSession?: SessionSummary;
  operationId?: string;
  setupComplete?: boolean;
  setupArchived?: boolean;
  cleanSession?: SessionSummary;
};

type AvatarSelection = { blob: Blob; previewUrl: string };

const emptyInteractionRequests = [] as const;
const agentCreationSteps = ["creatingProfile", "creatingSession", "configuring", "finalizing"] as const;
const terminalAgentSetupStatuses = new Set(["completed", "failed", "interrupted"]);

async function waitForAgentSetup(
  sessionId: string,
  operationId: string,
  initialStatus: string,
): Promise<string> {
  let status = initialStatus;
  const deadline = Date.now() + 10 * 60 * 1_000;
  while (!terminalAgentSetupStatuses.has(status)) {
    if (status === "delivery_unknown") throw new Error("AGENT_SETUP_DELIVERY_UNKNOWN");
    if (Date.now() >= deadline) throw new Error("AGENT_SETUP_TIMEOUT");
    const operation = await api.promptOperation(sessionId, operationId);
    status = operation.status;
    if (!terminalAgentSetupStatuses.has(status)) {
      if (status === "delivery_unknown") throw new Error("AGENT_SETUP_DELIVERY_UNKNOWN");
      await new Promise((resolve) => window.setTimeout(resolve, 1_000));
    }
  }
  return status;
}

export function AgentsScreen() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const selectedProfileId = useAppStore((state) => state.selectedProfileId);
  const selectProfile = useAppStore((state) => state.selectProfile);
  const gateways = useAppStore((state) => state.gateways);
  const profiles = useAppStore((state) => state.profiles);
  const selectedGatewayId = useAppStore((state) => state.selectedGatewayId);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const authState = useAppStore((state) => state.authState);
  const offline = authState === "offline";
  const demoMode = useAppStore((state) => state.demoMode);
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const [creatorOpen, setCreatorOpen] = useState(false);
  const [createError, setCreateError] = useState("");
  const [createAvatarError, setCreateAvatarError] = useState("");
  const [newAgentAvatar, setNewAgentAvatar] = useState<AvatarSelection | null>(null);
  const [avatarEditorProfile, setAvatarEditorProfile] = useState<Profile | null>(null);
  const [avatarDraft, setAvatarDraft] = useState<AvatarSelection | null>(null);
  const [avatarRemoved, setAvatarRemoved] = useState(false);
  const [avatarError, setAvatarError] = useState("");
  const [avatarBusy, setAvatarBusy] = useState(false);
  const [pendingSetup, setPendingSetup] = useState<PendingAgentSetup | null>(null);
  const [creationValues, setCreationValues] = useState<AgentCreateValues | null>(null);
  const [creationPhase, setCreationPhase] = useState<AgentCreationPhase>("idle");
  const [lastProgressPhase, setLastProgressPhase] = useState<Exclude<AgentCreationPhase, "idle" | "error">>("creatingProfile");
  const setupSessionId = pendingSetup?.setupSession?.id ?? "";
  const setupApprovals = useAppStore((state) => state.approvalsBySession[setupSessionId] ?? emptyInteractionRequests);
  const setupClarifications = useAppStore((state) => state.clarificationsBySession[setupSessionId] ?? emptyInteractionRequests);
  const eligibleGatewayIds = useMemo(() => new Set(
    profiles
      .filter((profile) => profile.capabilities?.profileCreate)
      .map((profile) => profile.gatewayId),
  ), [profiles]);
  const eligibleGateways = useMemo(
    () => gateways.filter((gateway) => eligibleGatewayIds.has(gateway.id)),
    [eligibleGatewayIds, gateways],
  );
  const defaultCreateGatewayId = eligibleGatewayIds.has(selectedGatewayId)
    ? selectedGatewayId
    : eligibleGateways[0]?.id ?? "";
  const canCreateProfile = Boolean(
    eligibleGateways.length
    && !offline
    && !demoMode,
  );
  const canEditAvatars = authState === "authenticated" && !demoMode;
  const agentCreateSchema = useMemo(() => z.object({
    gatewayId: z.string().refine((value) => eligibleGatewayIds.has(value), t("agentsPage.validationGateway")),
    technicalName: z.string()
      .trim()
      .min(2, t("agentsPage.validationTechnicalName"))
      .max(64, t("agentsPage.validationTechnicalName"))
      .regex(/^[a-z][a-z0-9-]*$/, t("agentsPage.validationTechnicalName")),
    displayName: z.string().trim().min(2, t("agentsPage.validationDisplayName")).max(80, t("agentsPage.validationDisplayName")),
    description: z.string().trim()
      .min(10, t("agentsPage.validationDescription"))
      .max(MAX_AGENT_BRIEF_CHARACTERS, t("agentsPage.validationDescriptionTooLong")),
  }).superRefine((values, context) => {
    if (profiles.some((profile) => (
      profile.gatewayId === values.gatewayId
      && profile.technicalName.toLowerCase() === values.technicalName.toLowerCase()
      && profile.id !== pendingSetup?.profile?.id
    ))) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: t("agentsPage.duplicateTechnicalName"),
        path: ["technicalName"],
      });
    }
  }), [eligibleGatewayIds, pendingSetup?.profile?.id, profiles, t]);
  const { register, handleSubmit, reset, formState: { errors, isSubmitting } } = useForm<AgentCreateValues>({
    resolver: zodResolver(agentCreateSchema),
    defaultValues: emptyAgentCreateValues,
  });
  useEffect(() => () => {
    if (newAgentAvatar?.previewUrl) URL.revokeObjectURL(newAgentAvatar.previewUrl);
  }, [newAgentAvatar?.previewUrl]);
  useEffect(() => () => {
    if (avatarDraft?.previewUrl) URL.revokeObjectURL(avatarDraft.previewUrl);
  }, [avatarDraft?.previewUrl]);
  const avatarErrorMessage = (error: unknown) => {
    const code = error instanceof Error ? error.message : "";
    if (code === "AVATAR_UNSUPPORTED_TYPE") return t("agentsPage.avatarUnsupported");
    if (code === "AVATAR_DECODE_FAILED") return t("agentsPage.avatarDecodeFailed");
    if (code === "AVATAR_TOO_LARGE") return t("agentsPage.avatarTooLarge");
    return code || t("agentsPage.avatarSaveError");
  };
  const selectAvatarFile = async (
    file: File | undefined,
    setSelection: (selection: AvatarSelection) => void,
    setError: (message: string) => void,
  ) => {
    if (!file) return;
    setError("");
    try {
      const blob = await prepareProfileAvatar(file);
      setSelection({ blob, previewUrl: URL.createObjectURL(blob) });
    } catch (error) {
      setError(avatarErrorMessage(error));
    }
  };
  const closeCreator = () => {
    setCreatorOpen(false);
    setCreateError("");
    setPendingSetup(null);
    setCreationValues(null);
    setCreationPhase("idle");
    setLastProgressPhase("creatingProfile");
    setCreateAvatarError("");
    setNewAgentAvatar(null);
    reset(emptyAgentCreateValues);
  };
  const creationInProgress = creationPhase !== "idle" && creationPhase !== "error";
  const creatorDialog = useOverlayDialog<HTMLDivElement>({
    open: creatorOpen,
    onClose: () => {
      if (!creationInProgress) closeCreator();
    },
    mediaQuery: "(min-width: 0px)",
  });
  const openCreator = () => {
    setCreateError("");
    setPendingSetup(null);
    setCreationValues(null);
    setCreationPhase("idle");
    setLastProgressPhase("creatingProfile");
    setCreateAvatarError("");
    setNewAgentAvatar(null);
    reset({ ...emptyAgentCreateValues, gatewayId: defaultCreateGatewayId });
    setCreatorOpen(true);
  };
  const advanceCreation = (phase: Exclude<AgentCreationPhase, "idle" | "error">) => {
    setLastProgressPhase(phase);
    setCreationPhase(phase);
  };
  const runAgentCreation = async (values: AgentCreateValues) => {
    const sourceProfile = profiles.find((profile) => (
      profile.gatewayId === values.gatewayId && profile.capabilities?.profileCreate
    ));
    if (!canCreateProfile || !sourceProfile) {
      setCreateError(t("agentsPage.createUnsupported"));
      return;
    }
    setCreationValues(values);
    setCreateError("");
    try {
      let progress = pendingSetup ?? {};
      advanceCreation("creatingProfile");
      let created = progress.profile ?? await api.createProfile({
        gatewayId: values.gatewayId,
        technicalName: values.technicalName,
        displayName: values.displayName,
        description: values.description,
      }, csrfToken);
      progress = { ...progress, profile: created };
      setPendingSetup(progress);

      if (newAgentAvatar && !progress.avatarUploaded) {
        const avatar = await api.setProfileAvatar(created.id, newAgentAvatar.blob, csrfToken);
        created = { ...created, avatarUrl: avatar.avatarUrl };
        progress = { ...progress, profile: created, avatarUploaded: true };
        setPendingSetup(progress);
      }

      advanceCreation("creatingSession");
      const setupSession = progress.setupSession ?? await api.createSession(created.id, undefined, csrfToken);
      progress = { ...progress, setupSession };
      setPendingSetup(progress);

      advanceCreation("configuring");
      if (!progress.setupComplete) {
        const setupPrompt = t("agentsPage.setupPrompt", {
          displayName: values.displayName.trim(),
          brief: values.description.trim(),
        });
        const existingOperationId = progress.operationId;
        const submittedOperationId = existingOperationId ?? crypto.randomUUID();
        if (!existingOperationId) {
          progress = { ...progress, operationId: submittedOperationId };
          setPendingSetup(progress);
        }
        let operationStatus = "accepted";
        let operationId = submittedOperationId;
        if (!existingOperationId) {
          try {
            const receipt = await api.submitPrompt(setupSession.id, setupPrompt, submittedOperationId, csrfToken);
            operationStatus = receipt.status;
            operationId = receipt.operationId;
            progress = { ...progress, operationId };
            setPendingSetup(progress);
          } catch (error) {
            const definitiveClientRejection = error instanceof ApiError
              && error.status >= 400
              && error.status < 500
              && !(error.status === 409 && /unknown|desconoc|delivery/i.test(`${error.code ?? ""} ${error.message}`));
            if (definitiveClientRejection) {
              progress = { ...progress, operationId: undefined };
              setPendingSetup(progress);
            }
            throw error;
          }
        }
        const terminalStatus = await waitForAgentSetup(setupSession.id, operationId, operationStatus);
        if (terminalStatus !== "completed") {
          progress = { ...progress, operationId: undefined };
          setPendingSetup(progress);
          throw new Error(terminalStatus === "interrupted" ? "AGENT_SETUP_INTERRUPTED" : "AGENT_SETUP_FAILED");
        }
        progress = { ...progress, setupComplete: true };
        setPendingSetup(progress);
      }

      advanceCreation("finalizing");
      if (!progress.setupArchived) {
        await api.archiveSession(setupSession.id, csrfToken);
        useAppStore.getState().clearSessionInteractions(setupSession.id);
        progress = { ...progress, setupArchived: true };
        setPendingSetup(progress);
      }
      const cleanSession = progress.cleanSession ?? await api.createSession(created.id, undefined, csrfToken);
      progress = { ...progress, cleanSession };
      setPendingSetup(progress);

      const bootstrap = await api.bootstrap();
      const refreshedProfile = bootstrap.profiles.find((profile) => (
        profile.id === created.id
        || (profile.gatewayId === values.gatewayId && profile.technicalName === created.technicalName)
      ));
      const activeProfile = refreshedProfile ?? created;
      const visibleSessions = [
        cleanSession,
        ...bootstrap.sessions.filter((session) => session.id !== setupSession.id && session.id !== cleanSession.id),
      ];
      hydrateBootstrap(refreshedProfile ? { ...bootstrap, sessions: visibleSessions } : {
        ...bootstrap,
        profiles: [...bootstrap.profiles.filter((profile) => profile.id !== created.id), created],
        sessions: visibleSessions,
      });
      selectProfile(activeProfile.id);
      useAppStore.getState().addSession(cleanSession);
      useAppStore.getState().selectSession(cleanSession.id);
      closeCreator();
      await navigate({ to: "/chats" });
    } catch (error) {
      const errorMessage = error instanceof Error ? error.message : "";
      const localizedError = errorMessage === "AGENT_SETUP_DELIVERY_UNKNOWN"
        ? t("agentsPage.progressDeliveryUnknown")
        : errorMessage === "AGENT_SETUP_TIMEOUT"
          ? t("agentsPage.progressTimeout")
          : errorMessage === "AGENT_SETUP_INTERRUPTED"
            ? t("agentsPage.progressInterrupted")
            : errorMessage === "AGENT_SETUP_FAILED"
              ? t("agentsPage.progressFailed")
              : errorMessage || (pendingSetup ? t("agentsPage.setupError") : t("agentsPage.createError"));
      setCreateError(localizedError);
      setCreationPhase("error");
    }
  };
  const createAgent = handleSubmit(runAgentCreation);
  const technicalNameField = register("technicalName");
  const progressPhase = creationPhase === "error" ? lastProgressPhase : creationPhase;
  const progressIndex = progressPhase === "idle" ? -1 : agentCreationSteps.indexOf(progressPhase);
  const setupProfile = pendingSetup?.profile;
  const canApproveSetup = Boolean(
    setupProfile?.mutable
    && setupProfile.capabilities?.approvals
    && setupProfile.capabilitySet?.methods.includes("approval.respond"),
  );
  const canClarifySetup = Boolean(
    setupProfile?.mutable
    && setupProfile.capabilities?.clarifications
    && setupProfile.capabilitySet?.methods.includes("clarify.respond"),
  );
  const avatarDialog = useOverlayDialog<HTMLDivElement>({
    open: Boolean(avatarEditorProfile),
    onClose: () => {
      if (!avatarBusy) {
        setAvatarEditorProfile(null);
        setAvatarDraft(null);
        setAvatarRemoved(false);
        setAvatarError("");
      }
    },
    mediaQuery: "(min-width: 0px)",
  });
  const openAvatarEditor = (profile: Profile) => {
    setAvatarEditorProfile(profile);
    setAvatarDraft(null);
    setAvatarRemoved(false);
    setAvatarError("");
  };
  const closeAvatarEditor = () => {
    if (avatarBusy) return;
    setAvatarEditorProfile(null);
    setAvatarDraft(null);
    setAvatarRemoved(false);
    setAvatarError("");
  };
  const saveAvatar = async () => {
    if (!avatarEditorProfile || avatarBusy || (!avatarDraft && !avatarRemoved)) return;
    setAvatarBusy(true);
    setAvatarError("");
    try {
      await api.setProfileAvatar(avatarEditorProfile.id, avatarRemoved && !avatarDraft ? null : avatarDraft!.blob, csrfToken);
      hydrateBootstrap(await api.bootstrap());
      setAvatarEditorProfile(null);
      setAvatarDraft(null);
      setAvatarRemoved(false);
      setAvatarError("");
    } catch (error) {
      setAvatarError(avatarErrorMessage(error));
    } finally {
      setAvatarBusy(false);
    }
  };
  return (
    <div className="page-wrap">
      <PageHeader eyebrow={t("agentsPage.eyebrow")} title={t("agentsPage.title")} description={t("agentsPage.description")} />
      <div className="agent-grid">
        {profiles.map((profile) => (
          <Panel key={profile.id} className={cx("agent-card", profile.id === selectedProfileId && "is-selected")}>
            <header><ProfileAvatar profile={profile} size="card" fallback="robot" /><span><strong>{profile.displayName}</strong><small>{profile.technicalName}</small></span><Badge tone={profile.status === "ready" ? "positive" : "warning"}>{profile.status === "ready" ? t("agentsPage.available") : profile.status === "busy" ? t("agentsPage.working") : t("agentsPage.unconfigured")}</Badge></header>
            <dl><div><dt>{t("agentsPage.model")}</dt><dd>{profile.model}</dd></div><div><dt>{t("agentsPage.functions")}</dt><dd>{profileWritePolicy(profile, t)}</dd></div></dl>
            <div className="agent-card__actions"><Button variant={profile.id === selectedProfileId ? "primary" : "secondary"} onClick={() => selectProfile(profile.id)}>{profile.id === selectedProfileId ? t("agentsPage.active") : t("agentsPage.use")}</Button>{profile.capabilities?.config ? <Link to="/config" className="hc-button hc-button--ghost hc-button--md">{t("agentsPage.viewConfig")}</Link> : null}{canEditAvatars ? <Button variant="ghost" leadingIcon={<PencilSimple />} onClick={() => openAvatarEditor(profile)}>{t("agentsPage.editImage")}</Button> : null}</div>
          </Panel>
        ))}
      </div>
      <Panel className="safety-callout"><ShieldCheck size={24} /><div><strong>{t("agentsPage.safetyTitle")}</strong><p>{t("agentsPage.safetyBody")}</p></div></Panel>
      <Panel className="create-agent-callout">
        <span className="create-agent-callout__icon"><Robot weight="duotone" /></span>
        <div><strong>{t("agentsPage.createCalloutTitle")}</strong><p>{canCreateProfile ? t("agentsPage.createCalloutBody") : t("agentsPage.createUnavailable")}</p></div>
        <Button variant="primary" leadingIcon={<Plus />} onClick={openCreator} disabled={!canCreateProfile}>{t("agentsPage.newAgent")}</Button>
      </Panel>
      {creatorOpen ? <div ref={creatorDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="agent-creator-title" aria-describedby="agent-creator-description">
        <button className="modal-scrim" aria-label={t("agentsPage.closeCreator")} onClick={() => { if (!creationInProgress) closeCreator(); }} />
        <Panel className={cx("form-modal agent-creator", creationPhase !== "idle" && "agent-creation-progress")}>
          {creationPhase === "idle" ? <>
            <span className="eyebrow">{t("agentsPage.creatorEyebrow")}</span>
            <h2 id="agent-creator-title">{t("agentsPage.creatorTitle")}</h2>
            <p id="agent-creator-description">{t("agentsPage.creatorDescription")}</p>
            <form onSubmit={createAgent} noValidate>
              <label className="hc-field">
                <span className="hc-field__label">{t("agentsPage.gateway")}</span>
                <select aria-label={t("agentsPage.gateway")} disabled={Boolean(pendingSetup)} {...register("gatewayId")}>
                  {eligibleGateways.map((gateway) => <option key={gateway.id} value={gateway.id}>{gateway.name}</option>)}
                </select>
                {errors.gatewayId?.message ? <span className="hc-field__error">{errors.gatewayId.message}</span> : <span className="hc-field__hint">{t("agentsPage.gatewayHint")}</span>}
              </label>
              <Field label={t("agentsPage.technicalName")} aria-label={t("agentsPage.technicalName")} hint={t("agentsPage.technicalNameHint")} autoCapitalize="none" autoCorrect="off" spellCheck={false} error={errors.technicalName?.message} disabled={Boolean(pendingSetup)} {...technicalNameField} onChange={(event) => {
                event.currentTarget.value = normalizeAgentTechnicalName(event.currentTarget.value);
                void technicalNameField.onChange(event);
              }} />
              <Field label={t("agentsPage.displayName")} aria-label={t("agentsPage.displayName")} autoComplete="off" error={errors.displayName?.message} disabled={Boolean(pendingSetup)} {...register("displayName")} />
              <div className="avatar-picker">
                <ProfileAvatar src={newAgentAvatar?.previewUrl ?? null} size="card" fallback="robot" />
                <div><strong>{t("agentsPage.agentImage")}</strong><small>{t("agentsPage.agentImageHint")}</small><span className="avatar-picker__actions"><label className="hc-button hc-button--secondary hc-button--sm">{t(newAgentAvatar ? "agentsPage.changeImage" : "agentsPage.chooseImage")}<input type="file" accept="image/jpeg,image/png,image/webp" onChange={(event) => { void selectAvatarFile(event.currentTarget.files?.[0], setNewAgentAvatar, setCreateAvatarError); event.currentTarget.value = ""; }} /></label>{newAgentAvatar ? <Button type="button" size="sm" variant="ghost" onClick={() => { setNewAgentAvatar(null); setCreateAvatarError(""); }}>{t("agentsPage.removeImage")}</Button> : null}</span>{createAvatarError ? <span className="hc-field__error" role="alert">{createAvatarError}</span> : null}</div>
              </div>
              <label className="hc-field">
                <span className="hc-field__label">{t("agentsPage.agentDescription")}</span>
                <textarea rows={6} aria-label={t("agentsPage.agentDescription")} placeholder={t("agentsPage.agentDescriptionPlaceholder")} aria-invalid={Boolean(errors.description)} {...register("description")} />
                {errors.description?.message ? <span className="hc-field__error">{errors.description.message}</span> : <span className="hc-field__hint">{t("agentsPage.agentDescriptionHint")}</span>}
              </label>
              <div><Button type="button" variant="ghost" onClick={closeCreator}>{t("agentsPage.cancel")}</Button><Button type="submit" variant="primary" disabled={isSubmitting}>{isSubmitting ? t("agentsPage.creating") : t("agentsPage.createAndUse")}</Button></div>
            </form>
          </> : <div aria-live="polite" aria-busy={creationInProgress || undefined}>
            <header className="agent-creation-progress__header">
              <span className="agent-creation-progress__icon"><Robot size={30} weight="duotone" /></span>
              <span><span className="eyebrow">{t("agentsPage.progressEyebrow")}</span><h2 id="agent-creator-title">{t("agentsPage.progressTitle", { name: creationValues?.displayName ?? setupProfile?.displayName ?? "" })}</h2></span>
            </header>
            <p id="agent-creator-description">{t("agentsPage.progressDescription")}</p>
            <ol className="agent-creation-steps">
              {agentCreationSteps.map((step, index) => {
                const completed = index < progressIndex;
                const active = index === progressIndex;
                const failed = active && creationPhase === "error";
                return <li key={step} className={cx(completed && "is-complete", active && "is-active", failed && "is-error")}>
                  <span>{completed ? <CheckCircle weight="fill" /> : failed ? <WarningCircle weight="fill" /> : active ? <SpinnerGap className="is-spinning" /> : <Clock />}</span>
                  <div><strong>{t(`agentsPage.progressSteps.${step}.title`)}</strong><small>{t(`agentsPage.progressSteps.${step}.description`)}</small></div>
                </li>;
              })}
            </ol>
            {setupApprovals.length || setupClarifications.length ? <div className="agent-creation-progress__attention">
              <p><WarningCircle weight="fill" /> {t("agentsPage.progressNeedsAttention")}</p>
              <InteractionCards approvals={[...setupApprovals]} clarifications={[...setupClarifications]} offline={offline} canApprove={canApproveSetup} canClarify={canClarifySetup} profileOverride={setupProfile} />
            </div> : null}
            {creationPhase === "error" ? <div className="agent-creation-progress__error">
              <p className="form-error" role="alert"><WarningCircle /> {createError}</p>
              <div><Button type="button" variant="ghost" onClick={closeCreator}>{t("agentsPage.cancel")}</Button><Button type="button" variant="primary" onClick={() => { if (creationValues) void runAgentCreation(creationValues); }}>{t("agentsPage.progressRetry")}</Button></div>
            </div> : <p className="agent-creation-progress__waiting"><SpinnerGap className="is-spinning" /> {t("agentsPage.progressWaiting")}</p>}
          </div>}
        </Panel>
      </div> : null}
      {avatarEditorProfile ? <div ref={avatarDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="avatar-editor-title" aria-describedby="avatar-editor-description">
        <button className="modal-scrim" aria-label={t("agentsPage.closeImageEditor")} onClick={closeAvatarEditor} />
        <Panel className="form-modal avatar-editor">
          <span className="eyebrow">{t("agentsPage.imageEditorEyebrow")}</span>
          <h2 id="avatar-editor-title">{t("agentsPage.editAgentImage", { name: avatarEditorProfile.displayName })}</h2>
          <p id="avatar-editor-description">{t("agentsPage.editAgentImageDescription")}</p>
          <div className="avatar-editor__preview"><ProfileAvatar profile={avatarEditorProfile} src={avatarDraft?.previewUrl ?? (avatarRemoved ? null : undefined)} size="lg" fallback="robot" /></div>
          <div className="avatar-editor__actions"><label className="hc-button hc-button--secondary hc-button--md">{t(avatarEditorProfile.avatarUrl || avatarDraft ? "agentsPage.changeImage" : "agentsPage.chooseImage")}<input type="file" accept="image/jpeg,image/png,image/webp" onChange={(event) => { void selectAvatarFile(event.currentTarget.files?.[0], (selection) => { setAvatarDraft(selection); setAvatarRemoved(false); }, setAvatarError); event.currentTarget.value = ""; }} /></label>{avatarEditorProfile.avatarUrl || avatarDraft ? <Button type="button" variant="ghost" onClick={() => { setAvatarDraft(null); setAvatarRemoved(true); setAvatarError(""); }}>{t("agentsPage.useDefaultImage")}</Button> : null}</div>
          {avatarError ? <p className="form-error" role="alert"><WarningCircle /> {avatarError}</p> : null}
          <div><Button type="button" variant="ghost" onClick={closeAvatarEditor}>{t("agentsPage.cancel")}</Button><Button type="button" variant="primary" disabled={avatarBusy || (!avatarDraft && !avatarRemoved)} onClick={() => void saveAvatar()}>{t(avatarBusy ? "agentsPage.savingImage" : "agentsPage.saveImage")}</Button></div>
        </Panel>
      </div> : null}
    </div>
  );
}

type AutomationValues = { name: string; schedule: string; timezone: string; prompt: string; profileId: string; workspaceId: string };

const emptyAutomationValues: AutomationValues = {
  name: "",
  schedule: "30 8 * * FRI",
  timezone: "Hermes local",
  prompt: "",
  profileId: "",
  workspaceId: "",
};

const automationTemplates = [
  {
    id: "morning-brief",
    translationKey: "morning",
    schedule: "30 8 * * MON-FRI",
  },
  {
    id: "weekly-review",
    translationKey: "weekly",
    schedule: "0 16 * * FRI",
  },
  {
    id: "daily-monitor",
    translationKey: "monitor",
    schedule: "0 18 * * *",
  },
] as const;

export function describeCron(schedule: string, timezone: string, translate: (key: string, options?: Record<string, string>) => string = (key, options) => String(i18n.t(key, options))): string {
  const [minute, hour, dayOfMonth, month, dayOfWeek] = schedule.trim().split(/\s+/);
  if (![minute, hour, dayOfMonth, month, dayOfWeek].every(Boolean)) return translate("automationsPage.cronIncomplete");
  const time = /^\d+$/.test(hour) && /^\d+$/.test(minute)
    ? `${String(Number(hour)).padStart(2, "0")}:${String(Number(minute)).padStart(2, "0")}`
    : `${minute} ${hour}`;
  const zone = timezone === "Hermes local"
    ? translate("automationsPage.hermesLocalTimezone")
    : timezone || "UTC";
  if (dayOfMonth === "*" && month === "*" && dayOfWeek === "MON-FRI") return translate("automationsPage.cronWeekdays", { time, zone });
  if (dayOfMonth === "*" && month === "*" && dayOfWeek === "*") return translate("automationsPage.cronDaily", { time, zone });
  if (dayOfMonth === "*" && month === "*" && /^(MON|TUE|WED|THU|FRI|SAT|SUN)$/.test(dayOfWeek)) {
    return translate("automationsPage.cronDay", { day: translate(`automationsPage.days.${dayOfWeek}`), time, zone });
  }
  return translate("automationsPage.cronAdvanced", { schedule: schedule.trim(), zone });
}

function uiAutomation(raw: Automation, profileId: string, previous?: Automation): Automation {
  const nextRuns = raw.nextRuns ?? (raw.nextRun ? [raw.nextRun] : []);
  return {
    ...previous,
    ...raw,
    profileId,
    nextRuns,
    nextRun: nextRuns[0] ?? "",
    lastStatus: previous?.lastStatus ?? raw.lastStatus ?? "idle",
  };
}

type AutomationReadFilter = "all" | "unread" | "read";

export function AutomationsScreen() {
  const { t } = useTranslation();
  const automationSchema = useMemo(() => z.object({
    name: z.string().trim().min(3, t("automationsPage.validationName")),
    schedule: z.string().trim().refine((value) => value.split(/\s+/).length === 5, t("automationsPage.validationCron")),
    timezone: z.string().trim().min(3, t("automationsPage.validationTimezone")),
    prompt: z.string().trim().min(3, t("automationsPage.validationPrompt")),
    profileId: z.string().min(1, t("automationsPage.validationProfile")),
    workspaceId: z.string(),
  }), [t]);
  const storeItems = useAppStore((state) => state.automations);
  const profiles = useAppStore((state) => state.profiles);
  const workspaces = useAppStore((state) => state.workspaces);
  const selectedWorkspaceId = useAppStore((state) => state.selectedWorkspaceId);
  const [items, setItems] = useState(storeItems);
  const [editorOpen, setEditorOpen] = useState(false);
  const [advancedEditor, setAdvancedEditor] = useState(false);
  const [editing, setEditing] = useState<Automation | null>(null);
  const [submitError, setSubmitError] = useState("");
  const [actionId, setActionId] = useState("");
  const [runs, setRuns] = useState<Record<string, AutomationRun[]>>({});
  const [runsRefreshVersion, setRunsRefreshVersion] = useState(0);
  const [readFilter, setReadFilter] = useState<AutomationReadFilter>("all");
  const csrfToken = useAppStore((state) => state.csrfToken);
  const demoMode = useAppStore((state) => state.demoMode);
  const offline = useAppStore((state) => state.authState === "offline");
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const selectSession = useAppStore((state) => state.selectSession);
  const navigate = useNavigate();
  const eligibleProfiles = profiles.filter((profile) => profile.mutable && profile.capabilities?.cronCreate);
  useEffect(() => setItems(storeItems), [storeItems]);
  useEffect(() => {
    if (demoMode || items.length === 0) return;
    let active = true;
    let timer: number | undefined;
    let refreshing = false;
    const refresh = async () => {
      if (!active || refreshing) return;
      refreshing = true;
      try {
        const rows = await Promise.all(items.map(async (automation) => [automation.id, await api.automationRuns(automation.id)] as const));
        if (!active) return;
        const next = Object.fromEntries(rows);
        setRuns(next);
        const stillRunning = rows.some(([, automationRuns]) => automationRuns.some((run) => ["accepted", "queued", "running"].includes(run.status)));
        if (stillRunning) timer = window.setTimeout(() => { void refresh(); }, 2_000);
      } catch {
        if (active) timer = window.setTimeout(() => { void refresh(); }, 5_000);
      } finally {
        refreshing = false;
      }
    };
    const onAutomationEvent = () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => { void refresh(); }, 200);
    };
    void refresh();
    window.addEventListener("hermes-control:automation-event", onAutomationEvent);
    return () => {
      active = false;
      window.clearTimeout(timer);
      window.removeEventListener("hermes-control:automation-event", onAutomationEvent);
    };
  }, [demoMode, items, runsRefreshVersion]);
  const closeEditor = () => { setEditorOpen(false); setEditing(null); setSubmitError(""); reset(emptyAutomationValues); };
  const editorDialog = useOverlayDialog<HTMLDivElement>({ open: editorOpen, onClose: closeEditor, mediaQuery: "(min-width: 0px)" });
  const { register, handleSubmit, reset, setValue, watch, formState: { errors, isSubmitting } } = useForm<AutomationValues>({ resolver: zodResolver(automationSchema), defaultValues: emptyAutomationValues });
  const watchedSchedule = watch("schedule");
  const watchedTimezone = watch("timezone");
  const openCreate = () => {
    setEditing(null);
    setAdvancedEditor(false);
    setSubmitError("");
    reset({ ...emptyAutomationValues, profileId: eligibleProfiles[0]?.id ?? "", workspaceId: selectedWorkspaceId });
    setEditorOpen(true);
  };
  const openEdit = (automation: Automation) => {
    setEditing(automation);
    setAdvancedEditor(true);
    setSubmitError("");
    reset({ name: automation.name, schedule: automation.schedule, timezone: automation.timezone, prompt: automation.prompt ?? "", profileId: automation.profileId, workspaceId: automation.workspaceId ?? "" });
    setEditorOpen(true);
  };
  const toggle = async (automation: Automation) => {
    if (actionId) return;
    setActionId(automation.id);
    setItems((current) => current.map((item) => item.id === automation.id ? { ...item, enabled: !item.enabled } : item));
    try {
      if (!demoMode) {
        const raw = await api.setAutomationEnabled(automation.id, !automation.enabled, csrfToken);
        setItems((current) => current.map((item) => item.id === automation.id ? uiAutomation(raw, automation.profileId, automation) : item));
      }
    } catch {
      setItems((current) => current.map((item) => item.id === automation.id ? automation : item));
    } finally {
      setActionId("");
    }
  };
  const save = handleSubmit(async (values) => {
    const profile = profiles.find((item) => item.id === values.profileId);
    const requiredCapability = editing ? profile?.capabilities?.cronUpdate : profile?.capabilities?.cronCreate;
    if (!profile?.mutable || !requiredCapability) {
      setSubmitError(t("automationsPage.unsupported"));
      return;
    }
    setSubmitError("");
    try {
      if (editing) {
        const changes: AutomationUpdateInput = { workspaceId: values.workspaceId || null };
        if (values.name !== editing.name) changes.name = values.name;
        if (values.schedule !== editing.schedule) changes.schedule = values.schedule;
        if (values.timezone !== editing.timezone) changes.timezone = values.timezone;
        if (values.prompt !== (editing.prompt ?? "")) changes.prompt = values.prompt;
        const raw = await api.updateAutomation(editing.id, changes, csrfToken);
        setItems((current) => current.map((item) => item.id === editing.id ? uiAutomation(raw, profile.id, editing) : item));
      } else {
        const raw = await api.createAutomation({
          gatewayId: profile.gatewayId,
          profileName: profile.technicalName,
          workspaceId: values.workspaceId || null,
          name: values.name,
          schedule: values.schedule,
          timezone: values.timezone,
          prompt: values.prompt,
          enabled: false,
        }, csrfToken);
        setItems((current) => [uiAutomation(raw, profile.id), ...current]);
      }
      closeEditor();
    } catch (error) {
      setSubmitError(error instanceof Error ? error.message : t("automationsPage.saveError"));
    }
  });
  const trigger = async (automation: Automation) => {
    if (actionId) return;
    setActionId(automation.id);
    try {
      await api.triggerAutomation(automation.id, csrfToken);
      const [nextRuns, bootstrap] = await Promise.all([api.automationRuns(automation.id), api.bootstrap()]);
      setRuns((current) => ({ ...current, [automation.id]: nextRuns }));
      // The immediate response is commonly queued/running. Restart the bounded
      // polling loop even when no realtime event can yet be routed to a Control
      // session (for example, before the cron run creates its session link).
      setRunsRefreshVersion((version) => version + 1);
      hydrateBootstrap(bootstrap);
      setItems((current) => current.map((item) => item.id === automation.id ? { ...item, lastStatus: nextRuns[0]?.status === "completed" ? "success" : nextRuns[0]?.status === "failed" ? "failed" : "idle" } : item));
    } finally { setActionId(""); }
  };
  const remove = async (automation: Automation) => {
    if (!window.confirm(t("automationsPage.removeConfirm", { name: automation.name }))) return;
    setActionId(automation.id);
    try {
      await api.deleteAutomation(automation.id, csrfToken);
      setItems((current) => current.filter((item) => item.id !== automation.id));
    } finally { setActionId(""); }
  };
  const latestResult = (automationId: string) => runs[automationId]?.find((run) => Boolean(run.sessionLinkId));
  const unreadCount = items.filter((automation) => {
    const result = latestResult(automation.id);
    return result && !result.readAt;
  }).length;
  const readCount = items.filter((automation) => Boolean(latestResult(automation.id)?.readAt)).length;
  const visibleItems = items.filter((automation) => {
    if (readFilter === "all") return true;
    const result = latestResult(automation.id);
    return readFilter === "unread" ? Boolean(result && !result.readAt) : Boolean(result?.readAt);
  });
  const openRunSession = async (run: AutomationRun) => {
    if (!run.sessionLinkId) return;
    if (!run.readAt && !demoMode && !offline) {
      try {
        const updated = await api.markAutomationRunRead(run.id, csrfToken);
        setRuns((current) => ({
          ...current,
          [run.automationId]: (current[run.automationId] ?? []).map((item) => item.id === updated.id ? updated : item),
        }));
      } catch {
        // Reading the result is more important than its indicator. A later
        // visit will retry once the connection is available again.
      }
    }
    selectSession(run.sessionLinkId);
    void navigate({ to: "/chats" });
  };
  return (
    <div className="page-wrap">
      <PageHeader eyebrow={t("automationsPage.eyebrow")} title={t("automationsPage.title")} description={t("automationsPage.description")} action={<Button variant="primary" leadingIcon={<Plus />} onClick={openCreate} disabled={!eligibleProfiles.length || offline}>{t("automationsPage.new")}</Button>} />
      {!eligibleProfiles.length ? <Panel className="safety-callout"><ShieldCheck size={24} /><div><strong>{t("automationsPage.unavailableTitle")}</strong><p>{t("automationsPage.unavailableBody")}</p></div></Panel> : null}
      <div className="next-runs"><span className="eyebrow">{t("automationsPage.nextFive")}</span><div>{items.filter((item) => item.enabled).flatMap((item) => (item.nextRuns?.length ? item.nextRuns : [item.nextRun]).map((run) => ({ item, run }))).slice(0, 5).map(({ item, run }, index) => <span key={`${item.id}-${run}-${index}`}><Clock /><strong>{run || t("automationsPage.pending")}</strong><small>{item.name}</small></span>)}</div></div>
      <div className="automation-filter-tabs" role="tablist" aria-label={t("automationsPage.filterLabel")}>
        {([
          ["all", t("automationsPage.all"), items.length],
          ["unread", t("automationsPage.unread"), unreadCount],
          ["read", t("automationsPage.read"), readCount],
        ] as const).map(([filter, label, count]) => <button key={filter} type="button" role="tab" aria-selected={readFilter === filter} className={readFilter === filter ? "is-active" : ""} onClick={() => setReadFilter(filter)}><span>{label}</span><small>{count}</small></button>)}
      </div>
      <div className="automation-list">
        {visibleItems.map((automation) => {
          const profile = profiles.find((item) => item.id === automation.profileId);
          const canUpdate = !offline && profile?.mutable === true && Boolean(profile.capabilities?.cronUpdate);
          const canTrigger = !offline && profile?.mutable === true && Boolean(profile.capabilities?.cronTrigger);
          const canDelete = !offline && profile?.mutable === true && Boolean(profile.capabilities?.cronDelete);
          const latestRun = runs[automation.id]?.[0];
          const result = latestResult(automation.id);
          const unread = Boolean(result && !result.readAt);
          const runLabel = latestRun?.status === "completed" ? t("automationsPage.runSuccess") : latestRun?.status === "failed" ? t("automationsPage.runFailed") : latestRun ? t("automationsPage.runStatus", { status: latestRun.status }) : t("automationsPage.noRuns");
          const workspace = workspaces.find((item) => item.id === automation.workspaceId);
          return <Panel key={automation.id} className="automation-row"><span className="automation-row__icon"><Lightning weight="duotone" /></span><div><span className="automation-row__title"><strong>{automation.name}</strong>{unread ? <span className="automation-unread-dot" role="img" aria-label={t("automationsPage.unreadResult")} title={t("automationsPage.unreadResult")} /> : null}</span><p>{describeCron(automation.schedule, automation.timezone, t)}</p><span><Badge>{profile?.displayName ?? automation.profileName ?? t("automationsPage.profile")}</Badge><Badge>{workspace?.name ?? t("automationsPage.noWorkspace")}</Badge><Badge tone={latestRun?.status === "completed" ? "positive" : latestRun?.status === "failed" ? "warning" : "neutral"}>{runLabel}</Badge></span><details className="cron-details"><summary>{t("automationsPage.viewCron")}</summary><code>{automation.schedule}</code></details></div><div className="automation-row__end">{canUpdate ? <Switch checked={automation.enabled} disabled={actionId === automation.id} onChange={() => void toggle(automation)} label={automation.enabled ? t("automationsPage.enabled") : t("automationsPage.paused")} /> : null}<span className="automation-actions">{result?.sessionLinkId ? <Button size="sm" variant="ghost" onClick={() => void openRunSession(result)}>{t("automationsPage.openSession")}</Button> : null}{canTrigger ? <Button size="sm" variant="ghost" leadingIcon={<Play />} disabled={actionId === automation.id} onClick={() => void trigger(automation)}>{t("automationsPage.run")}</Button> : null}{canUpdate ? <Button size="sm" variant="ghost" leadingIcon={<PencilSimple />} onClick={() => openEdit(automation)}>{t("automationsPage.edit")}</Button> : null}{canDelete ? <Button size="sm" variant="ghost" leadingIcon={<Trash />} disabled={actionId === automation.id} onClick={() => void remove(automation)}>{t("automationsPage.remove")}</Button> : null}</span></div></Panel>;
        })}
        {visibleItems.length === 0 ? <Panel className="automation-empty"><strong>{readFilter === "unread" ? t("automationsPage.emptyUnread") : readFilter === "read" ? t("automationsPage.emptyRead") : t("automationsPage.emptyAll")}</strong></Panel> : null}
      </div>
      {editorOpen ? <div ref={editorDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="automation-editor-title"><button className="modal-scrim" aria-label={t("automationsPage.closeEditor")} onClick={closeEditor} /><Panel className="form-modal automation-editor"><span className="eyebrow">{t("automationsPage.editor", { mode: advancedEditor ? t("automationsPage.advanced") : t("automationsPage.simple") })}</span><h2 id="automation-editor-title">{editing ? t("automationsPage.editTitle") : t("automationsPage.newTitle")}</h2><p>{editing ? t("automationsPage.editDescription") : t("automationsPage.newDescription")}</p><div className="editor-mode" role="group" aria-label={t("automationsPage.editorMode")}><button type="button" className={!advancedEditor ? "is-active" : ""} aria-pressed={!advancedEditor} onClick={() => setAdvancedEditor(false)}>{t("automationsPage.simple")}</button><button type="button" className={advancedEditor ? "is-active" : ""} aria-pressed={advancedEditor} onClick={() => setAdvancedEditor(true)}>{t("automationsPage.advancedCron")}</button></div><form onSubmit={save}>{!editing ? <fieldset className="automation-templates"><legend>{t("automationsPage.startTemplate")}</legend><div>{automationTemplates.map((template) => { const label = t(`automationsPage.templates.${template.translationKey}.label`); const prompt = t(`automationsPage.templates.${template.translationKey}.prompt`); return <button key={template.id} type="button" onClick={() => { setValue("name", label, { shouldValidate: true }); setValue("schedule", template.schedule, { shouldValidate: true }); setValue("prompt", prompt, { shouldValidate: true }); }}>{label}</button>; })}</div></fieldset> : null}<Field label={t("automationsPage.name")} error={errors.name?.message} {...register("name")} /><label className="hc-field"><span>{t("automationsPage.profile")}</span><select {...register("profileId")} disabled={Boolean(editing)}>{eligibleProfiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.displayName} · {profile.technicalName}</option>)}</select>{errors.profileId?.message ? <small className="hc-field__error">{errors.profileId.message}</small> : null}</label><label className="hc-field"><span>{t("automationsPage.workspace")}</span><select {...register("workspaceId")}><option value="">{t("automationsPage.noWorkspace")}</option>{workspaces.map((workspace) => <option key={workspace.id} value={workspace.id}>{workspace.name}</option>)}</select><small className="hc-field__hint">{t("automationsPage.workspaceHint")}</small></label>{advancedEditor ? <Field label={t("automationsPage.cronExpression")} placeholder="30 8 * * FRI" error={errors.schedule?.message} {...register("schedule")} /> : <label className="hc-field"><span>{t("automationsPage.frequency")}</span><select value={watchedSchedule} onChange={(event) => setValue("schedule", event.target.value, { shouldValidate: true })}><option value="30 8 * * MON-FRI">{t("automationsPage.weekdays")}</option><option value="0 9 * * *">{t("automationsPage.daily")}</option><option value="0 16 * * FRI">{t("automationsPage.friday")}</option>{!automationTemplates.some((template) => template.schedule === watchedSchedule) && watchedSchedule !== "0 9 * * *" ? <option value={watchedSchedule}>{t("automationsPage.customSchedule")}</option> : null}</select>{errors.schedule?.message ? <small className="hc-field__error">{errors.schedule.message}</small> : null}</label>}<label className="hc-field"><span>{t("automationsPage.timezone")}</span><select {...register("timezone")}><option value="Hermes local">{t("automationsPage.hermesLocalTimezone")}</option>{watchedTimezone && watchedTimezone !== "Hermes local" ? <option value={watchedTimezone}>{watchedTimezone}</option> : null}</select>{errors.timezone?.message ? <small className="hc-field__error">{errors.timezone.message}</small> : null}</label><output className="schedule-explanation" aria-live="polite"><Clock weight="duotone" /><span><strong>{t("automationsPage.explanation")}</strong>{describeCron(watchedSchedule, watchedTimezone, t)}</span></output><label className="hc-field"><span>{t("automationsPage.prompt")}</span><textarea rows={5} {...register("prompt")} />{errors.prompt?.message ? <small className="hc-field__error">{errors.prompt.message}</small> : null}</label>{submitError ? <p className="form-error" role="alert"><WarningCircle /> {submitError}</p> : null}<div><Button type="button" variant="ghost" onClick={closeEditor}>{t("automationsPage.cancel")}</Button><Button type="submit" variant="primary" disabled={isSubmitting}>{isSubmitting ? t("automationsPage.saving") : editing ? t("automationsPage.save") : t("automationsPage.createPaused")}</Button></div></form></Panel></div> : null}
    </div>
  );
}

type GatewayValues = { name: string; restUrl: string; wsUrl: string; apiUrl: string; dashboardToken: string; apiKey: string; trustedSourceSha: string };
type GatewayTrustValues = { trustedSourceSha: string };

const emptyGatewayValues: GatewayValues = {
  name: "",
  restUrl: "",
  wsUrl: "",
  apiUrl: "",
  dashboardToken: "",
  apiKey: "",
  trustedSourceSha: "",
};

export function GatewaysScreen() {
  const { t } = useTranslation();
  const gatewaySchema = useMemo(() => z.object({
    name: z.string().min(2, t("gatewaysPage.validationName")),
    restUrl: z.string().url(t("gatewaysPage.validationHttp")).refine((value) => value.startsWith("http://") || value.startsWith("https://"), t("gatewaysPage.validationHttpPrefix")),
    wsUrl: z.string().url(t("gatewaysPage.validationWs")).refine((value) => value.startsWith("ws://") || value.startsWith("wss://"), t("gatewaysPage.validationWsPrefix")),
    apiUrl: z.string().url(t("gatewaysPage.validationUrl")).refine((value) => value.startsWith("http://") || value.startsWith("https://"), t("gatewaysPage.validationHttpPrefix")).optional().or(z.literal("")),
    dashboardToken: z.string().min(12, t("gatewaysPage.validationCredential")).optional().or(z.literal("")),
    apiKey: z.string().min(12, t("gatewaysPage.validationCredential")).optional().or(z.literal("")),
    trustedSourceSha: z.string().regex(/^[0-9a-fA-F]{40}$/, t("gatewaysPage.validationSha")).or(z.literal("")),
  }), [t]);
  const gatewayTrustSchema = useMemo(() => z.object({
    trustedSourceSha: z.string().regex(/^[0-9a-fA-F]{40}$/, t("gatewaysPage.validationSha")),
  }), [t]);
  const [adding, setAdding] = useState(false);
  const [trustGateway, setTrustGateway] = useState<Gateway | null>(null);
  const [trustBusy, setTrustBusy] = useState(false);
  const [trustError, setTrustError] = useState("");
  const [saved, setSaved] = useState<"connected" | "degraded" | null>(null);
  const [submitError, setSubmitError] = useState("");
  const gateways = useAppStore((state) => state.gateways);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const hydrateBootstrap = useAppStore((state) => state.hydrateBootstrap);
  const selectGateway = useAppStore((state) => state.selectGateway);
  const setConnection = useAppStore((state) => state.setConnection);
  const offline = useAppStore((state) => state.authState === "offline");
  const { register, handleSubmit, reset, resetField, formState: { errors, isSubmitting } } = useForm<GatewayValues>({
    resolver: zodResolver(gatewaySchema),
    defaultValues: emptyGatewayValues,
  });
  const {
    register: registerTrust,
    handleSubmit: handleTrustSubmit,
    reset: resetTrust,
    resetField: resetTrustField,
    formState: { errors: trustErrors },
  } = useForm<GatewayTrustValues>({
    resolver: zodResolver(gatewayTrustSchema),
    defaultValues: { trustedSourceSha: "" },
  });
  const openForm = () => {
    reset(emptyGatewayValues);
    setSubmitError("");
    setAdding(true);
  };
  const closeForm = () => {
    reset(emptyGatewayValues);
    setSubmitError("");
    setAdding(false);
  };
  const openTrustForm = (gateway: Gateway) => {
    resetTrust({ trustedSourceSha: "" });
    setTrustError("");
    setTrustGateway(gateway);
  };
  const closeTrustForm = () => {
    resetTrust({ trustedSourceSha: "" });
    setTrustError("");
    setTrustGateway(null);
  };
  const gatewayDialog = useOverlayDialog<HTMLDivElement>({ open: adding, onClose: closeForm, mediaQuery: "(min-width: 0px)" });
  const trustDialog = useOverlayDialog<HTMLDivElement>({ open: Boolean(trustGateway), onClose: closeTrustForm, mediaQuery: "(min-width: 0px)" });
  const submit = handleSubmit(async (values) => {
    let completed = false;
    setSubmitError("");
    try {
      const result = await createAndProvisionGateway({
        name: values.name,
        restUrl: values.restUrl,
        wsUrl: values.wsUrl,
        apiUrl: values.apiUrl || undefined,
        dashboardToken: values.dashboardToken || undefined,
        apiKey: values.apiKey || undefined,
        trustedSourceSha: values.trustedSourceSha ? values.trustedSourceSha.toLowerCase() : undefined,
        connectionMode: "private",
      }, csrfToken);
      hydrateBootstrap(result.bootstrap);
      selectGateway(result.gatewayId);
      setConnection(result.degraded ? "degraded" : "connected");
      setSaved(result.degraded ? "degraded" : "connected");
      completed = true;
    } catch {
      setConnection("degraded");
      setSubmitError(t("gatewaysPage.saveError"));
    } finally {
      // React Hook Form must not retain write-only credentials after any
      // attempt, including validation-safe server failures.
      resetField("dashboardToken", { defaultValue: "" });
      resetField("apiKey", { defaultValue: "" });
      resetField("trustedSourceSha", { defaultValue: "" });
      if (completed) {
        reset(emptyGatewayValues);
        setAdding(false);
      }
    }
  });

  const applyTrust = async (trustedSourceSha: string | null) => {
    if (!trustGateway) return;
    let updated = false;
    let probeFailed = false;
    setTrustBusy(true);
    setTrustError("");
    try {
      await api.updateGateway(trustGateway.id, { trustedSourceSha }, csrfToken);
      updated = true;
      try {
        // Profile refresh performs the real read-only probes and persists the
        // resulting exact CapabilitySet used by every subsequent UI gate.
        await api.refreshProfiles(trustGateway.id, csrfToken);
      } catch {
        probeFailed = true;
      }
      const bootstrap = await api.bootstrap();
      hydrateBootstrap(bootstrap);
      setSaved(probeFailed ? "degraded" : "connected");
      closeTrustForm();
    } catch {
      setTrustError(updated
        ? t("gatewaysPage.trustRefreshError")
        : t("gatewaysPage.trustError"));
    } finally {
      // The operator assertion is write-only just like the gateway secrets.
      resetTrustField("trustedSourceSha", { defaultValue: "" });
      setTrustBusy(false);
    }
  };
  const saveTrust = handleTrustSubmit(async (values) => {
    await applyTrust(values.trustedSourceSha.toLowerCase());
  });

  return (
    <div className="page-wrap">
      <PageHeader eyebrow={t("gatewaysPage.eyebrow")} title={t("gatewaysPage.title")} description={t("gatewaysPage.description")} action={<Button variant="primary" leadingIcon={<Plus />} onClick={openForm} disabled={offline}>{t("gatewaysPage.add")}</Button>} />
      {saved === "connected" ? <div className="success-banner" role="status"><CheckCircle weight="fill" /> {t("gatewaysPage.synced")}</div> : null}
      {saved === "degraded" ? <div className="success-banner success-banner--warning" role="status"><WarningCircle weight="fill" /> {t("gatewaysPage.degradedSaved")}</div> : null}
      <div className="gateway-grid">{gateways.map((gateway) => <Panel key={gateway.id} className="gateway-card"><header><span><HardDrives weight="duotone" /></span><div><strong>{gateway.name}</strong><small>{gateway.location}</small></div><Badge tone={gateway.status === "connected" ? "positive" : "warning"}>{gateway.status === "connected" ? t("gatewaysPage.connected") : t("gatewaysPage.degraded")}</Badge></header><div className="gateway-metrics"><span><strong>{gateway.latencyMs ?? "—"} ms</strong><small>{t("gatewaysPage.latency")}</small></span><span><strong>{gateway.version}</strong><small>Hermes</small></span><span><strong>{Object.values(gateway.capabilities).filter(Boolean).length}</strong><small>{t("gatewaysPage.capabilities")}</small></span></div><p className="gateway-contract"><ShieldCheck weight="duotone" /><span><strong>{gateway.hasTrustedSourceSha ? t("gatewaysPage.trustedSha") : t("gatewaysPage.readOnly")}</strong><small>{gateway.hasTrustedSourceSha ? t("gatewaysPage.contractMatches") : t("gatewaysPage.addAuditedSha")}</small></span></p><footer><code>{gateway.sha ?? t("gatewaysPage.shaMissing")}</code><span>{gateway.envManaged ? <small>{t("gatewaysPage.backendTrust")}</small> : <Button size="sm" variant="ghost" leadingIcon={<PencilSimple />} onClick={() => openTrustForm(gateway)} disabled={offline}>{gateway.hasTrustedSourceSha ? t("gatewaysPage.editTrust") : t("gatewaysPage.configureTrust")}</Button>}<Link to="/diagnostics">{t("gatewaysPage.diagnostics")} <ArrowRight /></Link></span></footer></Panel>)}</div>
      {adding ? <div ref={gatewayDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="gateway-form-title"><button className="modal-scrim" aria-label={t("gatewaysPage.closeForm")} onClick={closeForm} /><Panel className="form-modal"><span className="eyebrow">{t("gatewaysPage.privateConnection")}</span><h2 id="gateway-form-title">{t("gatewaysPage.addTitle")}</h2><p>{t("gatewaysPage.addDescription")}</p><form onSubmit={submit}><Field label={t("gatewaysPage.name")} placeholder={t("gatewaysPage.privateServer")} error={errors.name?.message} {...register("name")} /><Field label={t("gatewaysPage.restUrl")} type="url" placeholder={t("gatewaysPage.privateRest")} error={errors.restUrl?.message} {...register("restUrl")} /><Field label={t("gatewaysPage.wsUrl")} type="url" placeholder={t("gatewaysPage.privateWs")} error={errors.wsUrl?.message} {...register("wsUrl")} /><Field label={t("gatewaysPage.apiFallback")} type="url" placeholder={t("gatewaysPage.privateApi")} error={errors.apiUrl?.message} {...register("apiUrl")} /><Field label={t("gatewaysPage.dashboardToken")} type="password" autoComplete="new-password" error={errors.dashboardToken?.message} {...register("dashboardToken")} /><Field label={t("gatewaysPage.apiKey")} type="password" autoComplete="new-password" error={errors.apiKey?.message} {...register("apiKey")} /><Field label={t("gatewaysPage.sourceSha")} type="password" autoComplete="new-password" placeholder={t("gatewaysPage.shaPlaceholder")} error={errors.trustedSourceSha?.message} {...register("trustedSourceSha")} /><p className="form-hint"><ShieldCheck /> {t("gatewaysPage.safeHint")}</p>{submitError ? <p className="form-error" role="alert"><WarningCircle /> {submitError}</p> : null}<div><Button type="button" variant="ghost" onClick={closeForm}>{t("gatewaysPage.cancel")}</Button><Button type="submit" variant="primary" disabled={isSubmitting}>{t("gatewaysPage.saveEncrypted")}</Button></div></form></Panel></div> : null}
      {trustGateway ? <div ref={trustDialog.containerRef} tabIndex={-1} className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="gateway-trust-title"><button className="modal-scrim" aria-label={t("gatewaysPage.closeTrust")} onClick={closeTrustForm} /><Panel className="form-modal"><span className="eyebrow">{t("gatewaysPage.contract")}</span><h2 id="gateway-trust-title">{t("gatewaysPage.trustTitle", { name: trustGateway.name })}</h2><p>{t("gatewaysPage.trustDescription")}</p><div className="form-hint" role="status"><ShieldCheck /><span><strong>{trustGateway.hasTrustedSourceSha ? t("gatewaysPage.trustConfigured") : t("gatewaysPage.gatewayReadOnly")}</strong><br />{t("gatewaysPage.trustHidden")}</span></div><form onSubmit={saveTrust}><Field label={t("gatewaysPage.newTrustedSha")} type="password" autoComplete="new-password" placeholder={t("gatewaysPage.shaPlaceholder")} error={trustErrors.trustedSourceSha?.message} {...registerTrust("trustedSourceSha")} />{trustError ? <p className="form-error" role="alert"><WarningCircle /> {trustError}</p> : null}<div><Button type="button" variant="ghost" onClick={closeTrustForm} disabled={trustBusy}>{t("gatewaysPage.cancel")}</Button>{trustGateway.hasTrustedSourceSha ? <Button type="button" variant="danger" onClick={() => void applyTrust(null)} disabled={trustBusy}>{t("gatewaysPage.resetReadOnly")}</Button> : null}<Button type="submit" variant="primary" disabled={trustBusy}>{trustBusy ? t("gatewaysPage.verifying") : t("gatewaysPage.saveVerify")}</Button></div></form></Panel></div> : null}
    </div>
  );
}

export function ConfigScreen() {
  const { t } = useTranslation();
  return <AdminConfigScreen header={<PageHeader eyebrow={t("configPage.eyebrow")} title={t("configPage.title")} description={t("configPage.description")} />} />;
}

export function diagnosticIsOperational(
  connection: string,
  gatewayStatus: Gateway["status"] | undefined,
  readiness: Pick<ReadinessView, "status" | "upstream"> | null,
) {
  return connection === "connected"
    && gatewayStatus === "connected"
    && readiness?.status === "ready"
    && readiness.upstream === "online";
}

export function DiagnosticsScreen() {
  const { t } = useTranslation();
  const gatewayId = useAppStore((state) => state.selectedGatewayId);
  const profileId = useAppStore((state) => state.selectedProfileId);
  const connection = useAppStore((state) => state.connection);
  const gateway = useAppStore((state) => state.gateways.find((item) => item.id === gatewayId));
  const profile = useAppStore((state) => state.profiles.find((item) => item.id === profileId));
  const [readiness, setReadiness] = useState<ReadinessView | null>(null);
  useEffect(() => {
    let active = true;
    const refresh = () => { void api.readiness().then((next) => { if (active) setReadiness(next); }).catch(() => { if (active) setReadiness({ status: "not_ready", database: "unavailable", upstream: "unknown", time: new Date().toISOString() }); }); };
    refresh();
    const interval = window.setInterval(refresh, 15_000);
    return () => { active = false; window.clearInterval(interval); };
  }, []);
  const capabilityLabels: Array<[keyof NonNullable<typeof gateway>["capabilities"], string]> = [["realtime", "Realtime RPC"], ["sessions", t("diagnosticsPage.sessions")], ["interrupt", t("diagnosticsPage.interrupt")], ["cron", "Cron"], ["profiles", t("diagnosticsPage.profiles")], ["config", t("diagnosticsPage.configuration")], ["memory", t("diagnosticsPage.memory")]];
  const capabilities = profile?.capabilities ?? gateway?.capabilities;
  const healthy = diagnosticIsOperational(connection, gateway?.status, readiness);
  const statusText = (value: string | undefined) => {
    if (!value) return t("diagnosticsPage.checking");
    if (value === "ready") return t("diagnosticsPage.ready");
    if (value === "unavailable") return t("diagnosticsPage.unavailable");
    if (value === "connected") return t("connected");
    if (value === "online") return t("diagnosticsPage.operationalBadge");
    if (value === "offline") return t("offline");
    if (value === "degraded" || value === "not_ready") return t("diagnosticsPage.degraded");
    if (value === "unknown") return t("diagnosticsPage.unknown");
    if (value === "reconnecting") return t("reconnecting");
    return value;
  };
  const exportReport = () => {
    const payload = {
      generatedAt: new Date().toISOString(),
      connection,
      readiness,
      gateway: gateway ? { id: gateway.id, name: gateway.name, status: gateway.status, version: gateway.version, sha: gateway.sha, capabilities: gateway.capabilities } : null,
      profile: profile ? { id: profile.id, displayName: profile.displayName, technicalName: profile.technicalName, status: profile.status, mutable: profile.mutable, capabilities: profile.capabilities } : null,
    };
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = `agent-control-diagnostics-${new Date().toISOString().slice(0, 10)}.json`;
    link.click();
    URL.revokeObjectURL(url);
  };
  return <div className="page-wrap"><PageHeader eyebrow={t("diagnosticsPage.eyebrow")} title={t("diagnosticsPage.title")} description={t("diagnosticsPage.description")} action={<Button leadingIcon={<DownloadSimple />} onClick={exportReport}>{t("diagnosticsPage.export")}</Button>} /><div className="health-hero"><span className="health-hero__icon"><CloudCheck weight="duotone" /></span><div><span className="eyebrow">{t("diagnosticsPage.global")}</span><h2>{healthy ? t("diagnosticsPage.operational") : t("diagnosticsPage.degraded")}</h2><p>{gateway?.name ?? t("diagnosticsPage.noGateway")} · {profile?.displayName ?? t("diagnosticsPage.noProfile")}</p></div><Badge tone={healthy ? "positive" : "warning"}>{healthy ? t("diagnosticsPage.operationalBadge") : t("diagnosticsPage.review")}</Badge></div><div className="diagnostic-grid"><Panel><header><Pulse /><strong>{t("diagnosticsPage.connectivity")}</strong></header><dl><div><dt>Control API</dt><dd><StatusDot tone={readiness?.status === "ready" ? "positive" : "warning"} /> {readiness?.status === "ready" ? t("diagnosticsPage.ready") : readiness ? t("diagnosticsPage.unavailable") : t("diagnosticsPage.checking")}</dd></div><div><dt>{t("diagnosticsPage.localDatabase")}</dt><dd><StatusDot tone={readiness?.database === "ready" ? "positive" : "warning"} /> {statusText(readiness?.database)}</dd></div><div><dt>{t("diagnosticsPage.lastProbe")}</dt><dd><StatusDot tone={readiness?.upstream === "online" ? "positive" : "warning"} /> {statusText(readiness?.upstream)}</dd></div><div><dt>Gateway</dt><dd><StatusDot tone={gateway?.status === "connected" ? "positive" : "warning"} /> {statusText(gateway?.status)}</dd></div><div><dt>Realtime</dt><dd><StatusDot tone={connection === "connected" ? "positive" : "warning"} /> {statusText(connection)}</dd></div></dl></Panel><Panel><header><Code /><strong>{t("diagnosticsPage.compatibility")}</strong></header><dl><div><dt>{t("diagnosticsPage.detectedVersion")}</dt><dd>{gateway?.version ?? t("diagnosticsPage.unknown")}</dd></div><div><dt>SHA</dt><dd><code>{gateway?.sha ?? t("diagnosticsPage.unknown")}</code></dd></div><div><dt>{t("diagnosticsPage.contract")}</dt><dd>{capabilities?.realtime ? "dashboard-jsonrpc" : t("diagnosticsPage.unverified")}</dd></div></dl></Panel></div><Panel className="capability-table"><header><Gauge /><strong>{t("diagnosticsPage.matrix")}</strong></header><div>{capabilityLabels.map(([key, label]) => <span key={key}>{capabilities?.[key] ? <CheckCircle weight="fill" /> : <WarningCircle />}<strong>{label}</strong><small>{capabilities?.[key] ? t("diagnosticsPage.verified") : t("diagnosticsPage.unannounced")}</small></span>)}</div></Panel><Panel className="log-preview"><header><TerminalWindow /><strong>{t("diagnosticsPage.sanitized")}</strong><Badge>{t("diagnosticsPage.noSecrets")}</Badge></header><pre><code>control={readiness?.status ?? "checking"}{"\n"}database={readiness?.database ?? "checking"}{"\n"}upstream={readiness?.upstream ?? "checking"}{"\n"}gateway={gateway?.name ?? "none"}{"\n"}profile={profile?.technicalName ?? "none"}{"\n"}version={gateway?.version ?? "unknown"}{"\n"}sha={gateway?.sha ?? "unknown"}</code></pre></Panel></div>;
}

export function SearchScreen() {
  const { t, i18n: translation } = useTranslation();
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<"all" | SearchResult["kind"]>("all");
  const [remoteResults, setRemoteResults] = useState<SearchResult[]>([]);
  const [partial, setPartial] = useState(false);
  const [loading, setLoading] = useState(false);
  const [searchError, setSearchError] = useState("");
  const viewportRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const authState = useAppStore((state) => state.authState);
  const demoMode = useAppStore((state) => state.demoMode);
  const sessions = useAppStore((state) => state.sessions);
  const workspaces = useAppStore((state) => state.workspaces);
  const automations = useAppStore((state) => state.automations);
  const messages = useAppStore((state) => state.messages);
  const profiles = useAppStore((state) => state.profiles);
  const selectSession = useAppStore((state) => state.selectSession);
  const selectWorkspace = useAppStore((state) => state.selectWorkspace);
  const allResults = useMemo(() => buildSearchResults({ sessions, workspaces, automations, messages, profiles }, t), [automations, messages, profiles, sessions, t, workspaces]);
  const localResults = useMemo(() => {
    const locale = translation.resolvedLanguage ?? translation.language;
    const needle = query.trim().toLocaleLowerCase(locale);
    return allResults.filter((result) => (filter === "all" || result.kind === filter) && (!needle || `${result.title} ${result.excerpt} ${result.meta}`.toLocaleLowerCase(locale).includes(needle)));
  }, [allResults, filter, query, translation.language, translation.resolvedLanguage]);
  const useLocalSearch = demoMode || authState !== "authenticated";
  useEffect(() => {
    const normalized = query.trim();
    if (useLocalSearch || normalized.length < 2) {
      setRemoteResults([]);
      setPartial(false);
      setLoading(false);
      setSearchError("");
      return undefined;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      setLoading(true);
      setSearchError("");
      void api.search(normalized, filter, 100, controller.signal)
        .then((response) => {
          setRemoteResults(response.items);
          setPartial(response.partial);
        })
        .catch((error) => {
          if (controller.signal.aborted) return;
          setRemoteResults([]);
          setPartial(false);
          setSearchError(error instanceof Error ? error.message : t("searchPage.queryError"));
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [filter, query, t, useLocalSearch]);
  const results = useLocalSearch ? localResults : remoteResults;
  const virtualizer = useVirtualizer({ count: results.length, getScrollElement: () => viewportRef.current, estimateSize: () => 84, overscan: 6 });
  const openResult = (result: SearchResult) => {
    if (result.kind === "automation") { void navigate({ to: "/automations" }); return; }
    if (result.kind === "workspace" && result.targetId) selectWorkspace(result.targetId);
    else if (result.targetId) selectSession(result.targetId);
    void navigate({ to: "/chats" });
  };
  const filters: Array<[typeof filter, string]> = [["all", t("searchPage.all")], ["message", t("searchPage.messages")], ["session", t("searchPage.sessions")], ["workspace", t("searchPage.workspaces")], ["automation", t("searchPage.automations")]];
  const emptyCopy = query.trim().length < 2
    ? t("searchPage.minChars")
    : loading
      ? t("searchPage.loading")
      : searchError || t("searchPage.noMatches");
  return <div className="page-wrap search-page"><PageHeader eyebrow={t("searchPage.eyebrow")} title={t("searchPage.title")} description={t("searchPage.description")} /><label className="search-box"><MagnifyingGlass /><input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} placeholder={t("searchPage.placeholder")} /><kbd>⌘ K</kbd></label><div className="search-filters">{filters.map(([value, label]) => <button key={value} type="button" aria-pressed={filter === value} className={filter === value ? "is-active" : ""} onClick={() => setFilter(value)}>{label}</button>)}</div>{partial ? <p className="form-warning" role="status"><WarningCircle /> {t("searchPage.partial")}</p> : null}<div className="virtual-results" ref={viewportRef} aria-busy={loading}><div style={{ height: virtualizer.getTotalSize(), position: "relative" }}>{virtualizer.getVirtualItems().map((row) => { const result = results[row.index]; return <button key={result.id} type="button" className="search-result" style={{ transform: `translateY(${row.start}px)`, height: row.size }} onClick={() => openResult(result)}><span className="search-result__icon">{result.kind === "automation" ? <Lightning /> : result.kind === "workspace" ? <FolderOpen /> : <FileText />}</span><span><strong>{result.title}</strong><small>{result.excerpt}</small></span><span className="search-result__meta">{result.meta}<ArrowRight /></span></button>; })}</div>{results.length === 0 ? <p className="empty-state" role="status">{emptyCopy}</p> : null}</div></div>;
}

export function SettingsScreen() {
  const { t } = useTranslation();
  const { language, changeLanguage, languageOptions } = useLanguagePreference();
  const theme = useAppStore((state) => state.theme);
  const setTheme = useAppStore((state) => state.setTheme);
  const cacheEnabled = useAppStore((state) => state.offlineCacheEnabled);
  const setCacheEnabled = useAppStore((state) => state.setOfflineCacheEnabled);
  const csrfToken = useAppStore((state) => state.csrfToken);
  const userName = useAppStore((state) => state.userName);
  const resetPrivateState = useAppStore((state) => state.resetPrivateState);
  const updateStatus = usePwaUpdateStore((state) => state.status);
  const updateDeferred = usePwaUpdateStore((state) => state.deferred);
  const updateBlockers = usePwaUpdateStore((state) => state.blockers);
  const [loggingOut, setLoggingOut] = useState(false);
  const setThemePreference = (next: ThemePreference) => { setTheme(next); void savePreference("theme", next); };
  const changeCache = (enabled: boolean) => {
    setCacheEnabled(enabled);
    void savePreference("offline-cache", String(enabled));
    if (!enabled) void clearTranscriptCache();
  };
  const logout = async () => {
    setLoggingOut(true);
    try { await api.logout(csrfToken); } catch { /* Local credentials are cleared even if the API is unreachable. */ }
    await clearPrivateCache().catch(() => undefined);
    resetPrivateState();
  };
  const updateAvailable = updateStatus === "available";
  const updateBlocked = hasPwaUpdateBlockers(updateBlockers);
  const updateMessage = updateDeferred
    ? "updates.deferred"
    : updateStatus === "checking"
      ? "updates.checking"
      : updateStatus === "current"
        ? "updates.current"
        : updateStatus === "available"
          ? "updates.available"
          : updateStatus === "applying"
            ? "updates.applying"
            : updateStatus === "error"
              ? "updates.error"
              : "updates.idle";
  const updateButton = updateStatus === "checking"
    ? "updates.checking"
    : updateStatus === "applying"
      ? "updates.applying"
      : updateAvailable
        ? updateBlocked ? "updates.updateWhenReady" : "updates.updateNow"
        : "updates.checkNow";
  const handleUpdate = () => updateAvailable ? requestPwaUpdate() : checkForPwaUpdate();

  return <div className="page-wrap">
    <PageHeader eyebrow={t("settingsPage.eyebrow")} title={t("settingsPage.title")} description={t("settingsPage.description")} />
    <div className="settings-layout">
      <Panel className="settings-section">
        <header><Translate /><div><strong>{t("settingsPage.language")}</strong><p>{t("settingsPage.languageDescription")}</p></div></header>
        <label className="hc-field"><span>{t("settingsPage.languageLabel")}</span><select value={language} onChange={(event) => void changeLanguage(event.target.value as typeof language)}>{languageOptions.map((option) => <option key={option.code} value={option.code}>{option.nativeName}</option>)}</select></label>
      </Panel>
      <Panel className="settings-section">
        <header><SlidersHorizontal /><div><strong>{t("settingsPage.appearance")}</strong><p>{t("settingsPage.appearanceDescription")}</p></div></header>
        <div className="theme-grid">{(["dark", "light", "auto"] as ThemePreference[]).map((option) => <button type="button" key={option} className={theme === option ? "is-active" : ""} onClick={() => setThemePreference(option)}><span className={`theme-preview theme-preview--${option}`}><i /><i /><i /></span><strong>{option === "dark" ? t("settingsPage.dark") : option === "light" ? t("settingsPage.light") : t("settingsPage.auto")}</strong></button>)}</div>
      </Panel>
      <Panel className="settings-section update-settings">
        <header><ArrowClockwise /><div><strong>{t("updates.settingsTitle")}</strong><p>{t("updates.settingsDescription")}</p></div></header>
        <div className="update-settings__row">
          <span><strong>{t("updates.installedVersion")}</strong><Badge>v{APP_VERSION}</Badge><small role="status" aria-live="polite">{t(updateMessage)}</small></span>
          <Button
            disabled={updateStatus === "checking" || updateStatus === "applying" || updateDeferred}
            leadingIcon={<ArrowClockwise className={updateStatus === "checking" || updateStatus === "applying" ? "spin" : undefined} />}
            onClick={() => void handleUpdate()}
          >{t(updateButton)}</Button>
        </div>
      </Panel>
      <ElevenLabsIntegration />
      <Panel className="settings-section"><header><Database /><div><strong>{t("settingsPage.offline")}</strong><p>{t("settingsPage.offlineDescription")}</p></div></header><Switch checked={cacheEnabled} onChange={changeCache} label={t("settingsPage.encryptedCache")} description={t("settingsPage.cacheLimits")} /><Button variant="ghost" onClick={() => void clearPrivateCache()}>{t("settingsPage.clearLocal")}</Button></Panel>
      <Panel className="settings-section"><header><UserCircle /><div><strong>{t("settingsPage.session")}</strong><p>{t("settingsPage.cookieAuth", { user: userName })}</p></div></header><Button variant="danger" disabled={loggingOut} onClick={() => void logout()}>{loggingOut ? t("settingsPage.loggingOut") : t("settingsPage.logout")}</Button></Panel>
    </div>
  </div>;
}

export function MoreScreen() {
  const { t } = useTranslation();
  const moreItems = [
    { to: "/search", title: t("morePage.search"), description: t("morePage.searchDescription"), icon: MagnifyingGlass },
    { to: "/gateways", title: t("morePage.gateways"), description: t("morePage.gatewaysDescription"), icon: HardDrives },
    { to: "/config", title: t("morePage.config"), description: t("morePage.configDescription"), icon: GearSix },
    { to: "/diagnostics", title: t("morePage.diagnostics"), description: t("morePage.diagnosticsDescription"), icon: Pulse },
    { to: "/admin", title: t("morePage.security"), description: t("morePage.securityDescription"), icon: ShieldCheck },
    { to: "/settings", title: t("morePage.preferences"), description: t("morePage.preferencesDescription"), icon: SlidersHorizontal },
  ] as const;
  return <div className="page-wrap"><PageHeader eyebrow={t("morePage.eyebrow")} title={t("morePage.title")} description={t("morePage.description")} /><div className="more-grid">{moreItems.map(({ to, title, description, icon: Icon }) => <Link key={to} to={to}><span><Icon weight="duotone" /></span><div><strong>{title}</strong><small>{description}</small></div><ArrowRight /></Link>)}</div><Panel className="about-panel"><BrandMark size="md" /><div><strong>Agent Control</strong><p>{t("morePage.about")}</p></div><Badge>v{APP_VERSION}</Badge></Panel></div>;
}

export function AdminScreen() {
  const { t, i18n: translation } = useTranslation();
  const offline = useAppStore((state) => state.authState === "offline");
  const [auditEvents, setAuditEvents] = useState<Awaited<ReturnType<typeof api.audit>>>([]);
  const [auditOpen, setAuditOpen] = useState(false);
  const [auditBusy, setAuditBusy] = useState(false);
  const [auditError, setAuditError] = useState("");

  const toggleAudit = async () => {
    if (auditOpen) {
      setAuditOpen(false);
      return;
    }
    setAuditOpen(true);
    if (auditEvents.length || offline) return;
    setAuditBusy(true);
    setAuditError("");
    try {
      setAuditEvents(await api.audit());
    } catch {
      setAuditError(t("adminPage.loadError"));
    } finally {
      setAuditBusy(false);
    }
  };

  return (
    <div className="page-wrap">
      <PageHeader eyebrow={t("adminPage.eyebrow")} title={t("adminPage.title")} description={t("adminPage.description")} />
      <div className="admin-grid">
        <Panel><ShieldCheck weight="duotone" /><h2>{t("adminPage.protectedTitle")}</h2><p>{t("adminPage.protectedBody")}</p><a href="/chats" className="hc-button hc-button--ghost hc-button--md">{t("adminPage.openChats")}</a></Panel>
        <Panel><Key weight="duotone" /><h2>{t("adminPage.vaultTitle")}</h2><p>{t("adminPage.vaultBody")}</p><Badge tone="positive">{t("adminPage.backendConfigured")}</Badge></Panel>
        <Panel><Database weight="duotone" /><h2>{t("adminPage.backupTitle")}</h2><p>{t("adminPage.backupBody")}</p><code className="runbook-reference">docs/operations/backup-restore.md</code></Panel>
        <Panel><FileText weight="duotone" /><h2>{t("adminPage.auditTitle")}</h2><p>{t("adminPage.auditBody")}</p><Button variant="ghost" onClick={() => void toggleAudit()} disabled={offline}>{auditOpen ? t("adminPage.closeEvents") : t("adminPage.openEvents")}</Button></Panel>
      </div>
      {auditOpen ? (
        <Panel className="audit-panel" aria-live="polite">
          <header><div><span className="eyebrow">{t("adminPage.localLog")}</span><h2>{t("adminPage.recentEvents")}</h2></div><Badge>{auditEvents.length}</Badge></header>
          {auditBusy ? <p>{t("adminPage.loading")}</p> : auditError ? <p className="form-error" role="alert"><WarningCircle /> {auditError}</p> : auditEvents.length ? (
            <ol>{auditEvents.map((event) => <li key={event.id}><span><strong>{event.action}</strong><small>{event.targetType ?? "control"}{event.targetId ? ` · ${event.targetId}` : ""}</small></span><span><Badge tone={event.outcome === "success" ? "positive" : "warning"}>{event.outcome}</Badge><time dateTime={event.createdAt}>{new Date(event.createdAt).toLocaleString(translation.resolvedLanguage ?? translation.language)}</time></span></li>)}</ol>
          ) : <p>{t("adminPage.empty")}</p>}
        </Panel>
      ) : null}
    </div>
  );
}
