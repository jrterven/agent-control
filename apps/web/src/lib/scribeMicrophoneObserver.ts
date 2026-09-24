// This registry has no SDK dependency and is safe to import before its lazy chunk.
const observers = new WeakMap<object, (pcm: string) => void>();
export function observeScribeMicrophone(config: object, callback: (pcm: string) => void) {
  observers.set(config, callback);
  return () => { observers.delete(config); };
}
export function copyScribeAudio(config: object, pcm: string) {
  try { observers.get(config)?.(pcm); } catch { /* A pilot failure cannot interrupt dictation. */ }
}
