import { api, type GatewayCreateInput } from "./api";
import type { BootstrapData } from "../types";

export type GatewayProvisioningResult = {
  bootstrap: BootstrapData;
  gatewayId: string;
  degraded: boolean;
};

/**
 * Creates a gateway, discovers its profiles and imports each profile's Hermes
 * sessions before returning the final browser projection. Secrets stay inside
 * the create request and are never returned by this helper.
 */
export async function createAndProvisionGateway(
  payload: GatewayCreateInput,
  csrfToken?: string,
): Promise<GatewayProvisioningResult> {
  const created = await api.createGateway(payload, csrfToken);
  let discoveryFailed = false;

  try {
    await api.refreshProfiles(created.id, csrfToken);
    const discovered = await api.bootstrap();
    const profiles = discovered.profiles.filter((profile) => profile.gatewayId === created.id);
    const synchronization = await Promise.allSettled(
      profiles.map((profile) => api.syncSessions(created.id, profile.technicalName, csrfToken)),
    );
    discoveryFailed = synchronization.some((result) => result.status === "rejected");
  } catch {
    discoveryFailed = true;
  }

  const finalBootstrap = await api.bootstrap();
  const gateway = finalBootstrap.gateways.find((item) => item.id === created.id);
  const degraded = discoveryFailed || gateway?.status !== "connected";
  const bootstrap = degraded
    ? {
        ...finalBootstrap,
        gateways: finalBootstrap.gateways.map((item) => (
          item.id === created.id ? { ...item, status: "degraded" as const } : item
        )),
      }
    : finalBootstrap;

  return { bootstrap, gatewayId: created.id, degraded };
}
