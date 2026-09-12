let activePreview: { owner: object; stop: () => void } | undefined;

/** A settings page can show several providers; their samples share one player. */
export function claimVoicePreview(owner: object, stop: () => void) {
  const previous = activePreview;
  activePreview = { owner, stop };
  if (previous && previous.owner !== owner) previous.stop();
}

export function releaseVoicePreview(owner: object) {
  if (activePreview?.owner === owner) activePreview = undefined;
}
