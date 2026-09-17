import { ArrowClockwise, Copy, DownloadSimple } from "@phosphor-icons/react";
import { Button, Panel } from "@hermes-control/ui";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { loadManagedDownloads, managedInstallCommand, type ManagedDownloads } from "../lib/managedDownloads";

export function InstallationCommand({ command }: { command: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState(false);
  useEffect(() => { setCopied(false); setError(false); }, [command]);
  const copy = async () => {
    setError(false);
    try { await navigator.clipboard.writeText(command); setCopied(true); }
    catch { setError(true); }
  };
  return <>
    <pre className="connector-command"><code>{command}</code></pre>
    <Button leadingIcon={<Copy aria-hidden="true" />} onClick={() => void copy()}>{t(copied ? "cloud.copied" : "cloud.copy")}</Button>
    {error ? <p role="status">{t("cloud.copyError")}</p> : null}
  </>;
}

export function CloudInstallerOptions({ existingCommand, loading, initialExisting = false }: { existingCommand?: string; loading: boolean; initialExisting?: boolean }) {
  const { t } = useTranslation();
  const [kind, setKind] = useState<"managed" | "existing">(initialExisting ? "existing" : "managed");
  const [downloads, setDownloads] = useState<ManagedDownloads | null>(null);
  const [checking, setChecking] = useState(true);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    setChecking(true);
    void loadManagedDownloads(controller.signal)
      .then((result) => { if (!controller.signal.aborted) setDownloads(result); })
      .catch(() => { if (!controller.signal.aborted) setDownloads(null); })
      .finally(() => { if (!controller.signal.aborted) setChecking(false); });
    return () => controller.abort();
  }, [revision]);
  const linuxCommand = downloads ? managedInstallCommand(downloads) : undefined;
  return <Panel className="settings-section connector-installation">
    <fieldset className="connector-installation-choices">
      <legend>{t("onboarding.installChoice")}</legend>
      {(["managed", "existing"] as const).map((option) => <label className={kind === option ? "is-selected" : ""} key={option}>
        <input type="radio" name="installation-kind" value={option} checked={kind === option} onChange={() => setKind(option)} />
        <span><strong>{t(`onboarding.${option}Title`)}</strong><small>{t(`onboarding.${option}Description`)}</small></span>
      </label>)}
    </fieldset>
    {kind === "existing" ? <div>
      <h2>{t("cloud.installTitle")}</h2><p>{t("cloud.installDescription")}</p>
      {existingCommand ? <InstallationCommand command={existingCommand} /> : !loading ? <p>{t("cloud.installUnavailable")}</p> : null}
    </div> : <div>
      <h2>{t("onboarding.managedTitle")}</h2><p>{t("onboarding.managedExplanation")}</p>
      {checking ? <p role="status">{t("onboarding.checkingDownloads")}</p> : null}
      <div className="connector-downloads">
        <section><h3>{t("onboarding.macTitle")}</h3><p>{t("onboarding.macRequirement")}</p>
          {!checking && downloads?.macosArm64 ? <><a className="hc-button hc-button--primary hc-button--md" href={downloads.macosArm64.url} download><DownloadSimple aria-hidden="true" />{t("onboarding.macDownload")}</a><p>{t("onboarding.macSteps")}</p></> : !checking ? <p>{t("onboarding.downloadPending")}</p> : null}
        </section>
        <section><h3>Linux</h3><p>{t("onboarding.linuxRequirement")}</p>
          {!checking && linuxCommand ? <><InstallationCommand command={linuxCommand} /><p>{t("onboarding.linuxSteps")}</p></> : !checking ? <p>{t("onboarding.downloadPending")}</p> : null}
        </section>
      </div>
      {!checking && (!downloads?.macosArm64 || !downloads.linux) ? <Button size="sm" variant="ghost" leadingIcon={<ArrowClockwise aria-hidden="true" />} onClick={() => setRevision((value) => value + 1)}>{t("onboarding.checkAgain")}</Button> : null}
    </div>}
    <div className="connector-provider-info">
      <h3>{t("onboarding.providerTitle")}</h3>
      <p>{t("onboarding.googleIdentity")}</p>
      <ul><li>{t("onboarding.chatgptProvider")}</li><li>{t("onboarding.apiProviders")}</li></ul>
      <p>{t("onboarding.localCredentials")}</p>
      <p>{t("onboarding.webChat")}</p>
    </div>
  </Panel>;
}
