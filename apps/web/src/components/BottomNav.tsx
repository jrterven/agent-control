import { ChatTeardropText, DotsThreeCircle, Lightning, Robot } from "@phosphor-icons/react";
import { Link, useRouterState } from "@tanstack/react-router";
import { useTranslation } from "react-i18next";

const nav = [
  { to: "/chats", labelKey: "chats", icon: ChatTeardropText },
  { to: "/agents", labelKey: "agents", icon: Robot },
  { to: "/automations", labelKey: "automations", icon: Lightning },
  { to: "/more", labelKey: "more", icon: DotsThreeCircle },
] as const;

export function BottomNav() {
  const { t } = useTranslation();
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  return (
    <nav className="bottom-nav" aria-label="Navegación principal">
      {nav.map(({ to, labelKey, icon: Icon }) => {
        const active = pathname === to || (to === "/more" && !["/chats", "/agents", "/automations"].includes(pathname));
        return <Link key={to} to={to} className={active ? "is-active" : ""}><Icon size={24} weight={active ? "fill" : "regular"} /><span>{t(labelKey)}</span></Link>;
      })}
    </nav>
  );
}
