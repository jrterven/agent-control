import { ArrowClockwise, CheckCircle, Desktop } from "@phosphor-icons/react";
import { Button, Panel } from "@hermes-control/ui";
import { Link, useNavigate } from "@tanstack/react-router";
import type { ConnectorView } from "@hermes-control/shared-types";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../lib/api";
import { useAppStore } from "../store/appStore";
import type { Profile } from "../types";

type Readiness = "waitingConnector" | "waitingHermes" | "needsSetup" | "ready" | "readinessError" | "revoked";

export function ConnectorReadiness({ computer }: { computer: ConnectorView }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const userId = useAppStore((state) => state.userId);
  const authGeneration = useAppStore((state) => state.authGeneration);
  const offline = useAppStore((state) => state.authState !== "authenticated");
  const [readiness, setReadiness] = useState<Readiness>("waitingConnector");
  const [availableProfiles, setAvailableProfiles] = useState<Profile[]>([]);
  const [profileId, setProfileId] = useState("");
  const [revision, setRevision] = useState(0);
  const [creating, setCreating] = useState(false);
  const creatingRef = useRef(false);
  const [chatError, setChatError] = useState(false);

  useEffect(() => {
    if (offline) { setReadiness("readinessError"); return; }
    let active = true;
    let inFlight = false;
    const load = async () => {
      if (inFlight) return;
      inFlight = true;
      try {
        const [computers, projection] = await Promise.all([api.connectors(), api.bootstrap()]);
        const current = useAppStore.getState();
        if (!active || current.authState !== "authenticated" || current.userId !== userId || current.authGeneration !== authGeneration) return;
        const linked = computers.items.find((item) => item.id === computer.id);
        current.hydrateBootstrap(projection);
        if (linked?.status === "revoked") { setReadiness("revoked"); setAvailableProfiles([]); return; }
        if (linked?.status !== "online") { setReadiness("waitingConnector"); setAvailableProfiles([]); return; }
        const gatewayId = linked.gatewayId ?? computer.gatewayId;
        const gateway = projection.gateways.find((item) => item.id === gatewayId);
        const profiles = projection.profiles.filter((profile) => profile.gatewayId === gatewayId && linked.profiles.includes(profile.technicalName) && profile.status !== "offline");
        if (gateway?.status !== "connected" || !profiles.length) { setReadiness("waitingHermes"); setAvailableProfiles([]); return; }
        const ready = profiles.filter((profile) => profile.mutable && profile.capabilities?.sessions && profile.capabilities?.prompts);
        setAvailableProfiles(ready);
        setProfileId((selected) => ready.some((profile) => profile.id === selected) ? selected : ready[0]?.id ?? "");
        setReadiness(ready.length ? "ready" : "needsSetup");
      } catch { if (active) { setReadiness("readinessError"); setAvailableProfiles([]); } }
      finally { inFlight = false; }
    };
    void load();
    const timer = window.setInterval(() => { if (document.visibilityState !== "hidden") void load(); }, 3_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [computer.id, computer.gatewayId, userId, authGeneration, offline, revision]);

  const openChat = async () => {
    if (creatingRef.current || offline || readiness !== "ready" || !availableProfiles.some((profile) => profile.id === profileId)) return;
    creatingRef.current = true;
    setCreating(true); setChatError(false);
    try {
      useAppStore.getState().selectProfile(profileId);
      const current = useAppStore.getState();
      if (current.authState !== "authenticated" || current.userId !== userId || current.authGeneration !== authGeneration) return;
      current.prepareChat();
      await navigate({ to: "/chats" });
    } catch { setChatError(true); }
    finally { creatingRef.current = false; setCreating(false); }
  };

  return <Panel className="settings-section connector-readiness">
    {readiness === "ready" ? <CheckCircle size={30} aria-hidden="true" /> : <Desktop size={30} aria-hidden="true" />}
    <h2>{computer.name}</h2>
    <p role="status" aria-live="polite">{t(`onboarding.${readiness}`)}</p>
    {readiness === "ready" ? <>
      <label className="hc-field"><span>{t("onboarding.chooseAgent")}</span><select value={profileId} disabled={creating} onChange={(event) => setProfileId(event.target.value)}>
        {availableProfiles.map((profile) => <option value={profile.id} key={profile.id}>{profile.displayName}</option>)}
      </select></label>
      <Button variant="primary" disabled={creating || offline} aria-busy={creating || undefined} onClick={() => void openChat()}>{t(creating ? "onboarding.creatingChat" : "onboarding.openNewChat")}</Button>
      <p>{t("onboarding.explicitChat")}</p>
    </> : readiness !== "revoked" ? <><p>{t("cloud.offlineHint")}</p><Button variant="ghost" disabled={offline} leadingIcon={<ArrowClockwise aria-hidden="true" />} onClick={() => setRevision((value) => value + 1)}>{t("cloud.refresh")}</Button></> : null}
    {chatError ? <p className="form-error" role="alert">{t("sidebar.createChatError")}</p> : null}
    <Link to="/computers" className="hc-button hc-button--ghost hc-button--md">{t("cloud.title")}</Link>
  </Panel>;
}
