import { useEffect } from "react";
import { create } from "zustand";
import type { AuthMethods } from "@hermes-control/shared-types";
import { api } from "./api";
import type { ConnectionState, Gateway, Profile } from "../types";

type CloudConfiguration = {
  methods?: AuthMethods;
  loading: boolean;
  error: boolean;
  load: () => Promise<void>;
};

// Deployment configuration is public and never persisted with account data.
export const useCloudConfigurationStore = create<CloudConfiguration>((set, get) => ({
  loading: false,
  error: false,
  load: async () => {
    if (get().loading) return;
    set({ loading: true, error: false });
    try {
      const methods = await api.authMethods();
      if (!methods || !["cloud", "private"].includes(methods.mode) || typeof methods.googleEnabled !== "boolean") throw new Error("Invalid authentication configuration");
      set({ methods, loading: false });
    } catch {
      set({ error: true, loading: false });
    }
  },
}));

export function useCloudConfiguration() {
  const config = useCloudConfigurationStore();
  useEffect(() => {
    if (!config.methods && !config.loading && !config.error) void config.load();
  }, [config.methods, config.loading, config.error, config.load]);
  return config;
}

export function pairingCodeFromReturnTo(returnTo: string | null): string | undefined {
  if (!returnTo || !returnTo.startsWith("/connect?")) return undefined;
  const code = new URLSearchParams(returnTo.slice("/connect?".length)).get("code");
  return code?.slice(0, 64) || undefined;
}

export function googleLoginUrl(search: string): string {
  const code = pairingCodeFromReturnTo(new URLSearchParams(search).get("returnTo"));
  const returnTo = code ? `/connect?${new URLSearchParams({ code })}` : "/chats";
  return `/api/v1/auth/google/start?${new URLSearchParams({ returnTo })}`;
}

export function isCloudProfileOffline(profile: Profile | undefined, gateways: Gateway[], connection?: ConnectionState): boolean {
  return useCloudConfigurationStore.getState().methods?.mode === "cloud"
    && ((connection !== undefined && connection !== "connected") || !profile || profile.status === "offline" || gateways.find((gateway) => gateway.id === profile.gatewayId)?.status !== "connected");
}
