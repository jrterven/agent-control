import { Check, Copy, WarningCircle } from "@phosphor-icons/react";
import { IconButton } from "@hermes-control/ui";
import { useEffect, useState, type RefObject } from "react";
import { useTranslation } from "react-i18next";

function responseHtml(body: HTMLDivElement) {
  const copy = body.cloneNode(true) as HTMLDivElement;
  // Export the rendered, sanitized Markdown without the chat's interactive media.
  copy.querySelectorAll(".message-image-gallery, svg, [role='alert'], [aria-hidden='true'], [data-footnote-backref]")
    .forEach((element) => element.remove());
  copy.querySelectorAll<HTMLInputElement>('input[type="checkbox"]').forEach((checkbox) => {
    checkbox.replaceWith(checkbox.checked ? "☑" : "☐");
  });
  copy.querySelectorAll("a[download]").forEach((anchor) => anchor.replaceWith(...anchor.childNodes));
  return copy.innerHTML;
}

export function useResponseCopy(body: RefObject<HTMLDivElement | null>, markdown: string) {
  const [status, setStatus] = useState<"idle" | "copying" | "copied" | "error">("idle");

  useEffect(() => {
    if (status !== "copied") return;
    const timeout = window.setTimeout(() => setStatus("idle"), 2400);
    return () => window.clearTimeout(timeout);
  }, [status]);

  async function copyResponse() {
    if (status === "copying" || !body.current) return;
    setStatus("copying");
    try {
      const clipboard = navigator.clipboard;
      if (typeof clipboard?.write === "function" && typeof ClipboardItem === "function"
        && (typeof ClipboardItem.supports !== "function" || ClipboardItem.supports("text/html"))) {
        await clipboard.write([new ClipboardItem({
          "text/html": new Blob([responseHtml(body.current)], { type: "text/html" }),
          "text/plain": new Blob([markdown], { type: "text/plain" }),
        })]);
      } else {
        await clipboard.writeText(markdown);
      }
      setStatus("copied");
    } catch {
      setStatus("error");
    }
  }

  return { status, copyResponse };
}

export function CopyResponseButton({ status, copyResponse }: ReturnType<typeof useResponseCopy>) {
  const { t } = useTranslation();
  const Icon = status === "copied" ? Check : status === "error" ? WarningCircle : Copy;

  return <IconButton
    type="button"
    className="message-copy-button"
    label={t("chat.copyResponse.label")}
    title={undefined}
    aria-disabled={status === "copying"}
    icon={<Icon size={18} aria-hidden="true" />}
    onClick={() => void copyResponse()}
  />;
}
