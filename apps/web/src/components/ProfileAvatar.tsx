import { Robot } from "@phosphor-icons/react";
import { useEffect, useState } from "react";
import { cx } from "@hermes-control/ui";
import type { Profile } from "../types";
import { BrandMark } from "./BrandMark";

type AvatarSize = "compact" | "activity" | "sm" | "card" | "lg";
type AvatarFallback = "brand" | "robot" | "initial";

export function ProfileAvatar({
  profile,
  src,
  size = "sm",
  fallback = "brand",
  className,
}: {
  profile?: Pick<Profile, "displayName" | "avatarUrl">;
  src?: string | null;
  size?: AvatarSize;
  fallback?: AvatarFallback;
  className?: string;
}) {
  const source = src === undefined ? profile?.avatarUrl : src;
  const [failedSource, setFailedSource] = useState<string | null>(null);
  useEffect(() => setFailedSource(null), [source]);

  if (source && failedSource !== source) {
    return <span className={cx("profile-avatar", `profile-avatar--${size}`, className)} aria-hidden="true"><img src={source} alt="" onError={() => setFailedSource(source)} /></span>;
  }
  if (fallback === "brand") {
    const brandSize = size === "lg" ? "lg" : size === "card" ? "md" : "sm";
    return <BrandMark size={brandSize} />;
  }
  return (
    <span className={cx("profile-avatar", `profile-avatar--${size}`, "profile-avatar--fallback", className)} aria-hidden="true">
      {fallback === "robot" ? <Robot weight="duotone" /> : profile?.displayName.trim().slice(0, 1).toUpperCase() || "?"}
    </span>
  );
}
