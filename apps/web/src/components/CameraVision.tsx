import { Eye, EyeSlash, ImageSquare, Play, X } from "@phosphor-icons/react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import type { CameraVisionState } from "../hooks/useCameraVision";
import { visionCopy } from "../lib/visionCopy";
import "./CameraVision.css";

export type CameraVisionProps = { camera: CameraVisionState; disabled?: boolean };

/** The eye is the permission gesture; opening it never submits an analysis. */
export function CameraVision({ camera, disabled = false }: CameraVisionProps) {
  const { i18n } = useTranslation();
  const copy = visionCopy[i18n.language.startsWith("es") ? "es" : "en"];
  const active = camera.phase !== "idle";
  return <div className={`camera-vision${active ? " is-active" : ""}`}>
    <button type="button" className="camera-vision__trigger" aria-label={active ? copy.stop : copy.activate}
      title={active ? copy.stop : copy.activate} aria-pressed={active}
      disabled={!active && (disabled || !camera.preferences)}
      onClick={() => { if (active) camera.stop(); else void camera.start("on_demand"); }}>
      <Eye size={22} weight={active ? "fill" : "regular"} aria-hidden="true" />
      {active ? <span className="camera-vision__indicator" /> : null}
    </button>
  </div>;
}

/** A small, in-flow preview beside the normal composer, with no separate prompt. */
export function CameraPreview({ camera, disabled = false, onAttach }: CameraVisionProps & {
  onAttach: (file: File) => void | Promise<void>;
}) {
  const { i18n } = useTranslation();
  const copy = visionCopy[i18n.language.startsWith("es") ? "es" : "en"];
  const [attaching, setAttaching] = useState(false);
  const active = camera.phase !== "idle";
  const busy = attaching || camera.analyzing;
  const attach = async () => {
    setAttaching(true);
    try {
      const frame = camera.latestFrame ?? await camera.takeNewCapture();
      if (frame) await onAttach(frame.file);
    } finally { setAttaching(false); }
  };
  return <>
    {active ? <section className="camera-vision__inline" aria-label={copy.preview}>
      <div className="camera-vision__preview">
        <video ref={camera.videoRef} autoPlay muted playsInline aria-label={copy.preview} />
        {camera.phase === "paused" ? <span><EyeSlash size={24} aria-hidden="true" /></span> : null}
      </div>
      <div className="camera-vision__details">
        <span className="camera-vision__state" role="status">{camera.phase === "starting" ? copy.starting : camera.phase === "paused" ? copy.paused : camera.analyzing ? copy.analyzing : copy.active}</span>
        <label className="sr-only" htmlFor="chat-camera-device">{copy.device}</label>
        <select id="chat-camera-device" value={camera.deviceId} disabled={camera.phase === "starting" || busy || disabled}
          onChange={(event) => void camera.switchDevice(event.target.value)}>
          <option value="">{copy.defaultDevice}</option>
          {camera.devices.map((device, index) => <option key={device.deviceId} value={device.deviceId}>{device.label || `${copy.cameraNumber} ${index + 1}`}</option>)}
        </select>
        <small>{copy.chatHint}</small>
      </div>
      <div className="camera-vision__quick-actions">
        {camera.phase === "paused" ? <button type="button" aria-label={copy.resume} title={copy.resume} disabled={disabled}
          onClick={() => void camera.resume()}><Play size={18} aria-hidden="true" /></button> : null}
        <button type="button" aria-label={camera.latestFrame ? copy.attach : copy.newCapture} title={camera.latestFrame ? copy.attach : copy.newCapture}
          disabled={camera.phase !== "active" || busy || disabled} onClick={() => void attach().catch(() => undefined)}><ImageSquare size={18} aria-hidden="true" /></button>
        <button type="button" aria-label={copy.stop} title={copy.stop} onClick={camera.stop}><X size={18} aria-hidden="true" /></button>
      </div>
    </section> : null}
    {camera.error ? <p className="camera-vision__error" role="alert">{copy.errors[camera.error]}</p> : null}
  </>;
}
