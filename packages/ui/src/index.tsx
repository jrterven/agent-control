import type {
  ButtonHTMLAttributes,
  HTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
} from "react";

export function cx(...values: Array<string | false | null | undefined>) {
  return values.filter(Boolean).join(" ");
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  size?: "sm" | "md";
  leadingIcon?: ReactNode;
};

export function Button({
  className,
  variant = "secondary",
  size = "md",
  leadingIcon,
  children,
  ...props
}: ButtonProps) {
  return (
    <button
      className={cx("hc-button", `hc-button--${variant}`, `hc-button--${size}`, className)}
      {...props}
    >
      {leadingIcon ? <span className="hc-button__icon">{leadingIcon}</span> : null}
      <span>{children}</span>
    </button>
  );
}

type IconButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  label: string;
  icon: ReactNode;
  selected?: boolean;
};

export function IconButton({ label, icon, selected, className, ...props }: IconButtonProps) {
  return (
    <button
      aria-label={label}
      title={label}
      className={cx("hc-icon-button", selected && "is-selected", className)}
      {...props}
    >
      {icon}
    </button>
  );
}

export function StatusDot({ tone = "positive", pulse = false }: { tone?: "positive" | "warning" | "negative" | "neutral"; pulse?: boolean }) {
  return <span className={cx("hc-status-dot", `hc-status-dot--${tone}`, pulse && "is-pulsing")} aria-hidden="true" />;
}

export function Badge({ children, tone = "neutral" }: { children: ReactNode; tone?: "neutral" | "info" | "positive" | "warning" }) {
  return <span className={cx("hc-badge", `hc-badge--${tone}`)}>{children}</span>;
}

export function Panel({ className, children, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cx("hc-panel", className)} {...props}>{children}</div>;
}

type FieldProps = InputHTMLAttributes<HTMLInputElement> & {
  label: string;
  hint?: string;
  error?: string;
};

export function Field({ label, hint, error, id, className, ...props }: FieldProps) {
  const inputId = id ?? `field-${label.toLowerCase().replace(/\s+/g, "-")}`;
  return (
    <label className="hc-field" htmlFor={inputId}>
      <span className="hc-field__label">{label}</span>
      <input id={inputId} className={cx("hc-input", className)} aria-invalid={Boolean(error)} {...props} />
      {error ? <span className="hc-field__error">{error}</span> : hint ? <span className="hc-field__hint">{hint}</span> : null}
    </label>
  );
}

export function Switch({ checked, onChange, label, description, disabled = false }: { checked: boolean; onChange: (checked: boolean) => void; label: string; description?: string; disabled?: boolean }) {
  return (
    <label className="hc-switch-row">
      <span>
        <strong>{label}</strong>
        {description ? <small>{description}</small> : null}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        aria-label={label}
        disabled={disabled}
        className={cx("hc-switch", checked && "is-on")}
        onClick={() => onChange(!checked)}
      >
        <span />
      </button>
    </label>
  );
}

export function Skeleton({ width = "100%", height = 16 }: { width?: string | number; height?: number }) {
  return <span className="hc-skeleton" style={{ width, height }} aria-hidden="true" />;
}
