/** Camera frames live only in memory. This module never persists media. */
export type CameraFrame = { file: File; image: string; capturedAt: string; width: number; height: number };
export type CameraDevice = { deviceId: string; label: string };
export const cameraMaxDimension = 1280;
export const cameraMaxBytes = 1024 * 1024;

export function cameraSupported() {
  return typeof navigator !== "undefined" && typeof navigator.mediaDevices?.getUserMedia === "function";
}
const cancelled = () => new DOMException("Camera operation cancelled", "AbortError");
export function stopCameraStream(stream?: MediaStream | null) {
  stream?.getTracks().forEach((track) => track.stop());
}
export async function cameraDevices(): Promise<CameraDevice[]> {
  if (!navigator.mediaDevices?.enumerateDevices) return [];
  return (await navigator.mediaDevices.enumerateDevices()).filter((device) => device.kind === "videoinput")
    .map(({ deviceId, label }) => ({ deviceId, label }));
}
export async function acquireCamera(signal: AbortSignal, deviceId?: string): Promise<MediaStream> {
  if (signal.aborted) throw cancelled();
  if (!cameraSupported()) throw new Error("unsupported");
  const mobile = typeof window.matchMedia === "function" && window.matchMedia("(pointer: coarse)").matches;
  const stream = await navigator.mediaDevices.getUserMedia({ audio: false, video: {
    ...(deviceId ? { deviceId: { exact: deviceId } } : mobile ? { facingMode: { ideal: "environment" } } : {}),
    width: { ideal: cameraMaxDimension }, height: { ideal: 720 },
  } });
  // Browsers cannot cancel the permission sheet; discard any late grant.
  if (signal.aborted) { stopCameraStream(stream); throw cancelled(); }
  return stream;
}

export async function prepareCameraVideo(stream: MediaStream, signal: AbortSignal): Promise<HTMLVideoElement> {
  const video = document.createElement("video");
  video.muted = true;
  video.playsInline = true;
  video.srcObject = stream;
  try {
    await new Promise<void>((resolve, reject) => {
      let playing = false;
      let settled = false;
      const clean = () => { clearTimeout(timer); video.removeEventListener("loadeddata", ready); signal.removeEventListener("abort", abort); };
      const fail = (error: unknown) => { if (settled) return; settled = true; clean(); video.pause(); video.srcObject = null; reject(error); };
      const ready = () => { if (!settled && playing && video.videoWidth && video.videoHeight && video.readyState >= 2) { settled = true; clean(); resolve(); } };
      const abort = () => fail(cancelled());
      const timer = setTimeout(() => fail(new Error("unavailable")), 10_000);
      video.addEventListener("loadeddata", ready);
      signal.addEventListener("abort", abort, { once: true });
      if (signal.aborted) { abort(); return; }
      try { void video.play().then(() => { playing = true; ready(); }, fail); }
      catch (error) { fail(error); }
    });
    if (signal.aborted) throw cancelled();
    return video;
  } catch (error) { video.srcObject = null; throw error; }
}

function jpeg(canvas: HTMLCanvasElement, quality: number) {
  return new Promise<Blob>((resolve, reject) => canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("capture")), "image/jpeg", quality));
}
function dataUrl(blob: Blob, signal: AbortSignal) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    const abort = () => { reader.abort(); reject(cancelled()); };
    const clean = () => signal.removeEventListener("abort", abort);
    reader.onload = () => { clean(); typeof reader.result === "string" ? resolve(reader.result) : reject(new Error("capture")); };
    reader.onerror = () => { clean(); reject(new Error("capture")); };
    reader.onabort = () => { clean(); reject(cancelled()); };
    if (signal.aborted) { reject(cancelled()); return; }
    signal.addEventListener("abort", abort, { once: true });
    reader.readAsDataURL(blob);
  });
}
export async function captureCameraFrame(video: HTMLVideoElement, signal: AbortSignal): Promise<CameraFrame> {
  if (signal.aborted) throw cancelled();
  const tracks = (video.srcObject as MediaStream | null)?.getVideoTracks() ?? [];
  if (!video.videoWidth || !video.videoHeight || video.readyState < 2 || !tracks.length
    || tracks.some((track) => track.readyState === "ended" || track.muted || !track.enabled)) throw new Error("unavailable");
  const canvas = document.createElement("canvas");
  const context = canvas.getContext("2d", { alpha: false });
  if (!context) throw new Error("capture");
  const ratio = Math.min(1, cameraMaxDimension / Math.max(video.videoWidth, video.videoHeight));
  canvas.width = Math.max(1, Math.round(video.videoWidth * ratio));
  canvas.height = Math.max(1, Math.round(video.videoHeight * ratio));
  const capturedAt = new Date().toISOString();
  context.drawImage(video, 0, 0, canvas.width, canvas.height);
  let blob = await jpeg(canvas, 0.82);
  if (signal.aborted) throw cancelled();
  if (blob.size > cameraMaxBytes) blob = await jpeg(canvas, 0.55);
  // Resize the captured canvas itself, preserving the same instant in every retry.
  while (blob.size > cameraMaxBytes && canvas.width > 160 && canvas.height > 160) {
    if (signal.aborted) throw cancelled();
    const resized = document.createElement("canvas");
    resized.width = Math.max(1, Math.round(canvas.width * 0.75));
    resized.height = Math.max(1, Math.round(canvas.height * 0.75));
    const resizedContext = resized.getContext("2d", { alpha: false });
    if (!resizedContext) throw new Error("capture");
    resizedContext.drawImage(canvas, 0, 0, resized.width, resized.height);
    canvas.width = resized.width; canvas.height = resized.height;
    context.drawImage(resized, 0, 0);
    blob = await jpeg(canvas, 0.55);
  }
  if (blob.size > cameraMaxBytes) throw new Error("capture");
  const image = await dataUrl(blob, signal);
  if (signal.aborted) throw cancelled();
  const file = new File([blob], `camera-${capturedAt.replace(/[:.]/g, "-")}.jpg`, { type: "image/jpeg" });
  return { file, image, capturedAt, width: canvas.width, height: canvas.height };
}
