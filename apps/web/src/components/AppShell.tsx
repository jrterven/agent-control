import { useEffect, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { hasPwaUpdateBlockers, requestPwaUpdate, usePwaUpdateStore } from "../lib/pwaUpdate";
import { useAppStore } from "../store/appStore";
import { ActivityPanel } from "./ActivityPanel";
import { BottomNav } from "./BottomNav";
import { CommandPalette } from "./CommandPalette";
import { ConnectionBanner } from "./ConnectionBanner";
import { LeftSidebar } from "./LeftSidebar";
import { NotificationMenu } from "./NotificationMenu";
import { TopBar } from "./TopBar";

export function AppShell({ children, conversation = false }: { children: ReactNode; conversation?: boolean }) {
  const { t } = useTranslation();
  const hasStreamingResponse = useAppStore((state) => Object.keys(state.streamingBySession).length > 0);
  const updateStatus = usePwaUpdateStore((state) => state.status);
  const updateDeferred = usePwaUpdateStore((state) => state.deferred);
  const updateBlockers = usePwaUpdateStore((state) => state.blockers);
  const setUpdateBlocker = usePwaUpdateStore((state) => state.setBlocker);

  useEffect(() => {
    setUpdateBlocker("streaming", hasStreamingResponse);
    return () => setUpdateBlocker("streaming", false);
  }, [hasStreamingResponse, setUpdateBlocker]);

  useEffect(() => {
    if (updateDeferred && updateStatus === "available" && !hasPwaUpdateBlockers(updateBlockers)) {
      void requestPwaUpdate();
    }
  }, [updateBlockers, updateDeferred, updateStatus]);

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">{t("nav.skipToContent")}</a>
      <LeftSidebar />
      <div className="app-center">
        <TopBar />
        <ConnectionBanner />
        <main id="main-content" className={conversation ? "main-content main-content--conversation" : "main-content"}>{children}</main>
      </div>
      <ActivityPanel />
      <NotificationMenu />
      <BottomNav />
      <CommandPalette />
    </div>
  );
}
