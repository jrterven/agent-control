type AgentPreference = { gatewayId: string; profileId: string };

const storageKey = (userId: string) => `agent-control.last-agent:${encodeURIComponent(userId)}`;

export function readAgentPreference(userId: string): AgentPreference | undefined {
  try {
    const raw = localStorage.getItem(storageKey(userId));
    if (!raw) return;
    const value = JSON.parse(raw) as Partial<AgentPreference> | null;
    if (typeof value?.gatewayId === "string" && value.gatewayId
      && typeof value.profileId === "string" && value.profileId) {
      return { gatewayId: value.gatewayId, profileId: value.profileId };
    }
  } catch {
    // Browser storage may be unavailable or contain an obsolete preference.
  }
}

export function saveAgentPreference(userId: string, preference: AgentPreference) {
  try {
    localStorage.setItem(storageKey(userId), JSON.stringify(preference));
  } catch {
    // Selecting an agent must still work when browser storage is disabled.
  }
}
