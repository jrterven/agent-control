import { useQuery, useQueryClient } from "@tanstack/react-query";
import { create } from "zustand";
import type { HomeData, HomeLayout, HomeSources, PanelDefinition, PanelKind, PanelSnapshot } from "@hermes-control/shared-types";
import { useAppStore } from "../store/appStore";
import { request } from "./api";

export const emptySources = (): HomeSources => ({ sessionIds: [], workspaceIds: [], automationIds: [], mailAccountIds: [], urls: [] });
export const catalog: Exclude<PanelKind, "custom">[] = ["pending", "prepared", "background", "automations", "pinned", "mail", "reports"];
export function newPanel(kind: PanelKind): PanelDefinition {
  return { id: kind === "custom" ? crypto.randomUUID() : kind, kind, title: "", description: "", size: "compact", pinned: false, sources: emptySources(), profileId: null, refreshMinutes: 1440, display: "summary" };
}

export const useHomeChatContext = create<{ ownerId: string; text: string; set: (ownerId: string, text: string) => void }>((set) => ({ ownerId: "", text: "", set: (ownerId, text) => set({ ownerId, text }) }));

export function homeMutation<T>(path: string, body: unknown, method = "POST"): Promise<T> {
  const state = useAppStore.getState();
  return request<T>(`/home${path}`, { method, headers: { "X-CSRF-Token": state.csrfToken ?? "", "Idempotency-Key": crypto.randomUUID() }, body: JSON.stringify(body) });
}

export function useHome() {
  const owner = useAppStore((s) => s.userId);
  const authenticated = useAppStore((s) => s.authState === "authenticated" && !s.demoMode);
  const cache = useQueryClient();
  const key = ["home", owner];
  const query = useQuery({ queryKey: key, queryFn: () => request<HomeData>("/home"), enabled: authenticated && Boolean(owner), refetchInterval: 60_000, retry: false });
  const save = async (layout: HomeLayout) => {
    const data = await homeMutation<HomeData>("/layout", layout, "PUT");
    if (useAppStore.getState().userId === owner) cache.setQueryData(key, data);
    return data;
  };
  const act = async (id: string, action: string) => {
    await homeMutation(`/suggestions/${id}`, { action });
    await cache.invalidateQueries({ queryKey: key });
  };
  return { ...query, save, act };
}

export type PanelPreview = { definition: PanelDefinition; snapshot: PanelSnapshot };
