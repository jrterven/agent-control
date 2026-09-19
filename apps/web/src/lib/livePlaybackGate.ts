const SAMPLE_INTERVAL_MS = 50;
const MINIMUM_HOLD_MS = 250;
const QUIET_SECONDS = 0.3;
const QUIET_RMS = 0.01;
const RELEASE_TIMEOUT_MS = 3_000;

/**
 * Drops unverified remote speech while a camera request is being checked.
 * Provider acknowledgements do not prove that already queued audio has ended;
 * release only opens playback after observing silence in the remote stream.
 */
export class LivePlaybackGate {
  private context?: AudioContext;
  private stream?: MediaStream;
  private source?: MediaStreamAudioSourceNode;
  private analyser?: AnalyserNode;
  private sink?: GainNode;
  private samples?: Float32Array<ArrayBuffer>;
  private held = false;
  private heldAt = 0;
  private generation = 0;
  private disposed = false;
  private pending?: (released: boolean) => void;

  constructor(private audio: HTMLAudioElement) {}

  /** Call synchronously from the Live user gesture, before awaiting permission. */
  async start(): Promise<boolean> {
    if (this.disposed) return false;
    try {
      if (!this.context) {
        const Constructor = globalThis.AudioContext
          ?? (globalThis as typeof globalThis & { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
        if (!Constructor) return false;
        this.context = new Constructor();
        this.connect();
      }
      await this.context.resume();
      return !this.disposed && this.context?.state === "running";
    } catch { return false; }
  }

  /** The caller retains ownership of the remote stream and its tracks. */
  attach(stream: MediaStream): void {
    if (this.disposed) return;
    this.generation += 1;
    this.pending?.(false);
    this.disconnect();
    this.stream = stream;
    this.connect();
  }

  hold(): void {
    if (this.disposed) return;
    this.audio.muted = true;
    this.held = true;
    this.heldAt = performance.now();
    this.generation += 1;
    this.pending?.(false);
  }

  /** False means playback remains muted; callers must not deliver stale speech. */
  release(signal?: AbortSignal): Promise<boolean> {
    this.audio.muted = true;
    this.pending?.(false);
    if (!this.held || this.disposed || signal?.aborted || !this.observable()) return Promise.resolve(false);
    const generation = this.generation;
    const startedAt = performance.now();
    let quietSince: number | undefined;

    return new Promise((resolve) => {
      let timer: ReturnType<typeof setTimeout> | undefined;
      let settled = false;
      const finish = (released: boolean) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        signal?.removeEventListener("abort", cancelled);
        if (this.pending === finish) this.pending = undefined;
        if (released) {
          this.held = false;
          this.audio.muted = false;
        }
        resolve(released);
      };
      const cancelled = () => finish(false);
      const sample = () => {
        if (this.disposed || generation !== this.generation || signal?.aborted
          || !this.observable() || performance.now() - startedAt >= RELEASE_TIMEOUT_MS) {
          finish(false);
          return;
        }
        try {
          const contextTime = this.context!.currentTime;
          this.analyser!.getFloatTimeDomainData(this.samples!);
          let squares = 0;
          for (const value of this.samples!) squares += value * value;
          const rms = Math.sqrt(squares / this.samples!.length);
          if (rms < QUIET_RMS) {
            quietSince ??= contextTime;
            // Use the audio clock as well as elapsed wall time: a stalled audio
            // graph returning zeros must never be mistaken for drained speech.
            if (contextTime - quietSince >= QUIET_SECONDS
              && performance.now() - this.heldAt >= MINIMUM_HOLD_MS) {
              finish(true);
              return;
            }
          } else quietSince = undefined;
        } catch {
          finish(false);
          return;
        }
        timer = setTimeout(sample, SAMPLE_INTERVAL_MS);
      };
      this.pending = finish;
      signal?.addEventListener("abort", cancelled, { once: true });
      sample();
    });
  }

  dispose(): void {
    if (this.disposed) return;
    this.disposed = true;
    this.generation += 1;
    this.audio.muted = true;
    this.pending?.(false);
    this.disconnect();
    this.stream = undefined;
    const context = this.context;
    this.context = undefined;
    if (context) void context.close().catch(() => {});
  }

  private observable(): boolean {
    return this.context?.state === "running" && !!this.analyser && !!this.samples
      && !!this.stream?.getAudioTracks().some((track) => track.readyState === "live" && track.enabled && !track.muted);
  }

  private connect(): void {
    const context = this.context;
    if (!context || context.state === "closed" || !this.stream?.getAudioTracks().length) return;
    try {
      this.source = context.createMediaStreamSource(this.stream);
      this.analyser = context.createAnalyser();
      this.analyser.fftSize = 2048;
      this.samples = new Float32Array(this.analyser.fftSize);
      this.sink = context.createGain();
      this.sink.gain.value = 0;
      // Keep the observer graph processing without playing the stream twice.
      this.source.connect(this.analyser);
      this.analyser.connect(this.sink);
      this.sink.connect(context.destination);
    } catch { this.disconnect(); }
  }

  private disconnect(): void {
    for (const node of [this.source, this.analyser, this.sink]) {
      try { node?.disconnect(); } catch { /* A closing graph may already be detached. */ }
    }
    this.source = undefined;
    this.analyser = undefined;
    this.sink = undefined;
    this.samples = undefined;
  }
}
