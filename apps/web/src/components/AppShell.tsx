import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { ActivityPanel } from "./ActivityPanel";
import { BottomNav } from "./BottomNav";
import { CommandPalette } from "./CommandPalette";
import { ConnectionBanner } from "./ConnectionBanner";
import { LeftSidebar } from "./LeftSidebar";
import { TopBar } from "./TopBar";

export function AppShell({ children, conversation = false }: { children: ReactNode; conversation?: boolean }) {
  const { t } = useTranslation();
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
      <BottomNav />
      <CommandPalette />
    </div>
  );
}
