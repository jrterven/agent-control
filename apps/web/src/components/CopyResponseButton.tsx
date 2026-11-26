import { Check, Copy } from "@phosphor-icons/react";
import { Button } from "@hermes-control/ui";
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

export function CopyResponseButton({ body, markdown }: { body: RefObject<HTMLDivElement | null>; markdown: string }) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<"idle" | "copying" | "copied" | "error">("idle");
  const feedback = status === "copied" || status === "error" ? status : undefined;

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

  return <>
    <Button
      type="button"
      variant="ghost"
      size="sm"
      className="message-copy-button"
      aria-label={t("chat.copyResponse.label")}
      aria-disabled={status === "copying"}
      leadingIcon={status === "copied" ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      onClick={() => void copyResponse()}
    >{t("chat.copyResponse.copy")}</Button>
    <span role="status" className={feedback ? `message-copy-feedback message-copy-feedback--${feedback}` : "sr-only"}>
      {feedback ? t(`chat.copyResponse.${feedback}`) : ""}
    </span>
  </>;
}
