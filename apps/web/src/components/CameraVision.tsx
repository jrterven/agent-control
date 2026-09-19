import { Camera, Eye, EyeSlash, Pause, Play, X } from "@phosphor-icons/react";
import { useEffect, useRef, useState, type CSSProperties } from "react";
import { useTranslation } from "react-i18next";
import type { VisionMode } from "@hermes-control/shared-types";
import type { CameraVisionState } from "../hooks/useCameraVision";
import { visionCopy } from "../lib/visionCopy";
import "./CameraVision.css";

export type CameraVisionProps = {
  camera: CameraVisionState;
  disabled?: boolean;
  lookDisabled?: boolean;
  attachDisabled?: boolean;
  onLook: () => Promise<void>;
  onAttach: (file: File) => void | Promise<void>;
};
export function CameraVision({ camera, disabled = false, lookDisabled = false, attachDisabled = false, onLook, onAttach }: CameraVisionProps) {
  const { i18n } = useTranslation();
  const copy = visionCopy[i18n.language.startsWith("es") ? "es" : "en"];
  const [menu, setMenu] = useState(false);
  const [selectedMode, setSelectedMode] = useState<VisionMode | null>(null);
  const wasActive = useRef(false);
  const [position, setPosition] = useState({ bottom: 105, left: 16 });
  const [working, setWorking] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const active = camera.phase !== "idle";
  const busy = working || camera.analyzing;
  useEffect(() => {
    if (!menu && !active && !selectedMode) return;
    const composer = container.current?.closest(".composer-wrap");
    const update = () => {
      const rect = composer?.getBoundingClientRect();
      const anchor = container.current?.getBoundingClientRect();
      const next = { bottom: rect ? Math.max(16, window.innerHeight - rect.top + 12) : 105, left: Math.max(16, Math.min(anchor?.left ?? 16, window.innerWidth - 376)) };
      setPosition((previous) => previous.bottom === next.bottom && previous.left === next.left ? previous : next);
    };
    update();
    const observer = typeof ResizeObserver === "function" ? new ResizeObserver(update) : null;
    if (composer) observer?.observe(composer);
    window.addEventListener("resize", update); window.visualViewport?.addEventListener("resize", update);
    return () => { observer?.disconnect(); window.removeEventListener("resize", update); window.visualViewport?.removeEventListener("resize", update); };
  }, [menu, active, selectedMode]);
  useEffect(() => {
    if (!menu) return;
    const outside = (event: PointerEvent) => { if (!container.current?.contains(event.target as Node)) setMenu(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { setMenu(false); trigger.current?.focus(); } };
    document.addEventListener("pointerdown", outside); document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape); };
  }, [menu]);
  useEffect(() => {
    if (!active) { setWorking(false); if (wasActive.current) setSelectedMode(null); }
    wasActive.current = active;
  }, [active]);
  const chooseMode = (nextMode: VisionMode) => {
    if (active) { wasActive.current = false; camera.stop(); }
    setMenu(false); setSelectedMode(nextMode);
  };
  const displayedMode = camera.mode ?? selectedMode;
  const close = () => { camera.stop(); setSelectedMode(null); setMenu(false); };
  const attachNew = async () => {
    setWorking(true);
    try { const frame = await camera.takeNewCapture(); if (frame) await onAttach(frame.file); }
    finally { setWorking(false); }
  };
  const look = async () => {
    setWorking(true);
    try { await onLook(); }
    finally { setWorking(false); }
  };
  return <div className={`camera-vision${active ? " is-active" : ""}`} ref={container} style={{ "--camera-panel-bottom": `${position.bottom}px`, "--camera-panel-left": `${position.left}px` } as CSSProperties}>
    <button ref={trigger} type="button" className="camera-vision__trigger" aria-label={copy.title} aria-expanded={menu || active || Boolean(selectedMode)} aria-haspopup="true" disabled={disabled && !active} onClick={() => setMenu((value) => !value)}>
      <Eye size={22} weight={active ? "fill" : "regular"} aria-hidden="true" />
      {active ? <span className="camera-vision__indicator" /> : null}
    </button>
    {menu ? <div className="camera-vision__menu" aria-label={copy.choose}>
      <strong>{copy.choose}</strong>
      <button type="button" aria-pressed={displayedMode === "on_demand"} disabled={disabled || !camera.supported} onClick={() => chooseMode("on_demand")}><Eye /><span><strong>{copy.onDemand}</strong><small>{copy.onDemandHint}</small></span></button>
      <button type="button" aria-pressed={displayedMode === "continuous"} disabled={disabled || !camera.supported} onClick={() => chooseMode("continuous")}><Camera /><span><strong>{copy.continuous}</strong><small>{copy.continuousHint}</small></span></button>
      {!camera.supported ? <p>{copy.errors.unsupported}</p> : !camera.preferences?.configured ? <p>{copy.errors.configuration}</p> : <p>{copy.settings}</p>}
    </div> : null}
    {active || selectedMode ? <section className="camera-vision__panel" aria-label={copy.title}>
      <header><div><span className={`camera-vision__status ${camera.phase === "active" ? "is-capturing" : ""}`} /><strong>{camera.phase === "starting" ? copy.starting : camera.phase === "paused" ? copy.paused : camera.phase === "idle" ? copy.inactive : copy.active}</strong><small>{displayedMode === "continuous" ? copy.continuous : copy.onDemand}</small></div><button type="button" onClick={close} aria-label={active ? copy.stop : copy.close}><X size={20} /></button></header>
      {active ? <div className="camera-vision__preview"><video ref={camera.videoRef} autoPlay muted playsInline aria-label={copy.title} />{camera.phase === "paused" ? <span><EyeSlash size={32} />{copy.paused}</span> : null}</div> : null}
      <div className="camera-vision__controls">
        <small>{copy.model}: <strong>{camera.preferences?.modelId ?? "…"}</strong></small>
        <label>{copy.device}<select value={camera.deviceId} disabled={camera.phase === "starting"} onChange={(event) => void camera.switchDevice(event.target.value)}><option value="">{copy.defaultDevice}</option>{camera.devices.map((device, index) => <option key={device.deviceId} value={device.deviceId}>{device.label || `${copy.cameraNumber} ${index + 1}`}</option>)}</select></label>
        {active ? <div className="camera-vision__buttons"><button type="button" onClick={() => camera.phase === "paused" ? void camera.resume() : camera.pause()}>{camera.phase === "paused" ? <Play /> : <Pause />}{camera.phase === "paused" ? copy.resume : copy.pause}</button><button type="button" onClick={close}><EyeSlash />{copy.stop}</button></div> : <button type="button" className="camera-vision__look" disabled={disabled || !camera.supported || !camera.preferences?.configured || !selectedMode} onClick={() => { if (selectedMode) void camera.start(selectedMode); }}><Camera />{copy.activate}</button>}
        {!active ? <p className="camera-vision__hint">{copy.activationHint}</p> : camera.mode === "on_demand" ? <><button type="button" className="camera-vision__look" disabled={camera.phase !== "active" || busy || disabled || lookDisabled} onClick={() => void look().catch(() => undefined)}><Eye />{busy ? copy.analyzing : copy.look}</button></> : <p className="camera-vision__hint">{camera.analyzing ? copy.analyzing : `${camera.preferences?.intervalSeconds ?? 5} s · ${copy.continuousStatus}`}</p>}
        {camera.latestObservation ? <div className="camera-vision__latest"><small>{copy.captured} {new Date(camera.latestObservation.capturedAt).toLocaleTimeString(i18n.language, { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</small><p>{camera.latestObservation.summary}</p></div> : null}
        {active ? <div className="camera-vision__attachments"><button type="button" disabled={!camera.latestFrame || busy || disabled || attachDisabled} onClick={() => { if (camera.latestFrame) void Promise.resolve(onAttach(camera.latestFrame.file)).catch(() => undefined); }}>{copy.attach}</button><button type="button" disabled={camera.phase !== "active" || busy || disabled || attachDisabled} onClick={() => void attachNew().catch(() => undefined)}>{copy.newCapture}</button></div> : null}
        <p className="camera-vision__hint">{copy.temporary}</p>
      </div>
    </section> : null}
    {camera.error ? <p className="camera-vision__error" role="alert">{copy.errors[camera.error]}</p> : null}
  </div>;
}
