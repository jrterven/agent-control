import { Atom } from "@phosphor-icons/react";
import { cx } from "@hermes-control/ui";

export function BrandMark({ size = "md", label }: { size?: "sm" | "md" | "lg"; label?: string }) {
  return (
    <span className={cx("brand-mark", `brand-mark--${size}`)} aria-label={label} role={label ? "img" : undefined} aria-hidden={label ? undefined : true}>
      <Atom weight="duotone" />
    </span>
  );
}
