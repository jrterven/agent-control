import type { ReactNode } from "react";
import { ActivityPanel } from "./ActivityPanel";
import { BottomNav } from "./BottomNav";
import { CommandPalette } from "./CommandPalette";
import { ConnectionBanner } from "./ConnectionBanner";
import { LeftSidebar } from "./LeftSidebar";
import { TopBar } from "./TopBar";

export function AppShell({ children, conversation = false }: { children: ReactNode; conversation?: boolean }) {
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">Saltar al contenido</a>
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
