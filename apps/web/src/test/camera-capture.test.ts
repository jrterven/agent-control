import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { acquireCamera, captureCameraFrame, prepareCameraVideo, cameraMaxBytes } from "../lib/cameraCapture";

const deferred = <T,>() => { let resolve!: (value: T) => void; const promise = new Promise<T>((done) => { resolve = done; }); return { promise, resolve }; };
function media() {
  const track = { stop: vi.fn(), enabled: true, muted: false, readyState: "live" };
  return { track, stream: { getTracks: () => [track], getVideoTracks: () => [track] } as unknown as MediaStream };
}
function usableVideo(stream: MediaStream) {
  const video = document.createElement("video"); video.srcObject = stream;
  Object.defineProperties(video, { videoWidth: { configurable: true, value: 2560 }, videoHeight: { configurable: true, value: 1440 }, readyState: { configurable: true, value: 2 } });
  return video;
}
describe("in-memory camera media", () => {
  beforeEach(() => {
    vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
    vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => undefined);
  });
  afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

  it("asks for rear camera on mobile and exact selected camera with no audio", async () => {
    const input = media(); const getUserMedia = vi.fn(async () => input.stream);
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia } });
    Object.defineProperty(window, "matchMedia", { configurable: true, value: () => ({ matches: true }) });
    await acquireCamera(new AbortController().signal);
    expect(getUserMedia).toHaveBeenLastCalledWith({ audio: false, video: { facingMode: { ideal: "environment" }, width: { ideal: 1280 }, height: { ideal: 720 } } });
    await acquireCamera(new AbortController().signal, "usb-camera");
    expect(getUserMedia).toHaveBeenLastCalledWith({ audio: false, video: { deviceId: { exact: "usb-camera" }, width: { ideal: 1280 }, height: { ideal: 720 } } });
  });

  it("stops tracks returned after the browser permission request was cancelled", async () => {
    const grant = deferred<MediaStream>(); const input = media();
    Object.defineProperty(navigator, "mediaDevices", { configurable: true, value: { getUserMedia: vi.fn(() => grant.promise) } });
    const controller = new AbortController(); const pending = acquireCamera(controller.signal);
    controller.abort(); grant.resolve(input.stream);
    await expect(pending).rejects.toMatchObject({ name: "AbortError" }); expect(input.track.stop).toHaveBeenCalledOnce();
  });

  it("bounds a stalled playback promise and detaches the pending video on abort", async () => {
    vi.useFakeTimers(); vi.mocked(HTMLMediaElement.prototype.play).mockReturnValue(new Promise(() => undefined));
    const videos: HTMLVideoElement[] = []; const create = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation(((tag: string) => { const element = create(tag); if (tag === "video") videos.push(element as HTMLVideoElement); return element; }) as typeof document.createElement);
    const controller = new AbortController(); const first = prepareCameraVideo(media().stream, controller.signal); const rejected = expect(first).rejects.toMatchObject({ name: "AbortError" }); controller.abort(); await rejected;
    expect(videos[0].srcObject).toBeNull();
    const timed = prepareCameraVideo(media().stream, new AbortController().signal); const timeout = expect(timed).rejects.toThrow("unavailable");
    await vi.advanceTimersByTimeAsync(10_000); await timeout; expect(videos[1].srcObject).toBeNull();
  });

  it("captures one instant, bounds JPEG dimensions and uses the same bytes for the attached File", async () => {
    const input = media(); const video = usableVideo(input.stream); const drawImage = vi.fn();
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({ drawImage } as unknown as CanvasRenderingContext2D);
    vi.spyOn(HTMLCanvasElement.prototype, "toBlob").mockImplementation((callback) => callback(new Blob(["captured-frame"], { type: "image/jpeg" })));
    const frame = await captureCameraFrame(video, new AbortController().signal);
    expect(frame.width).toBe(1280); expect(frame.height).toBe(720); expect(frame.file.type).toBe("image/jpeg"); expect(frame.file.size).toBeLessThanOrEqual(cameraMaxBytes);
    expect(frame.image).toBe("data:image/jpeg;base64,Y2FwdHVyZWQtZnJhbWU=");
    expect(drawImage).toHaveBeenCalledExactlyOnceWith(video, 0, 0, 1280, 720);
    expect(frame.file.size).toBe("captured-frame".length);
  });

  it("rejects a frozen or unavailable video instead of timestamping it as current", async () => {
    const input = media(); const video = usableVideo(input.stream); input.track.muted = true;
    await expect(captureCameraFrame(video, new AbortController().signal)).rejects.toThrow("unavailable");
    input.track.muted = false; input.track.readyState = "ended";
    await expect(captureCameraFrame(video, new AbortController().signal)).rejects.toThrow("unavailable");
    input.track.readyState = "live"; Object.defineProperty(video, "readyState", { value: 1 });
    await expect(captureCameraFrame(video, new AbortController().signal)).rejects.toThrow("unavailable");
  });
});
