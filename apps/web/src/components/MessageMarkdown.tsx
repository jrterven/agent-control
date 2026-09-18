import { useMemo } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { useTranslation } from "react-i18next";
import { remarkMessageImages, safeImageSource, type ImageReference } from "../lib/messageImages";
import { MessageImageGallery } from "./MessageImageGallery";
import { useAppStore } from "../store/appStore";

const imageSchema = {
  ...defaultSchema,
  attributes: { ...defaultSchema.attributes, div: [...(defaultSchema.attributes?.div ?? []), "dataImageRefs"] },
};

export function MessageMarkdown({ content, sessionId, streaming = false }: { content: string; sessionId: string; streaming?: boolean }) {
  const { t } = useTranslation();
  const limits = useAppStore((state) => state.features?.images);
  const components = useMemo<Components>(() => ({
      div: ({ node, children }) => {
        const serialized = node?.properties.dataImageRefs;
        if (typeof serialized === "string") {
          const references = JSON.parse(serialized) as ImageReference[];
          return <MessageImageGallery sessionId={sessionId} references={references} />;
        }
        return <div>{children}</div>;
      },
      // No arbitrary image URL, local path, data URL or HTML can trigger a fetch.
      img: ({ src, alt }) => {
        const href = safeImageSource(src);
        return href ? <a href={href} target="_blank" rel="noopener noreferrer">{t("images.external", { alt: alt || t("images.untitled") })}</a>
          : <span>{alt || t("images.untitled")}</span>;
      },
  }), [sessionId, t]);
  return <ReactMarkdown
    remarkPlugins={[remarkGfm, [remarkMessageImages, { streaming, ...limits }]]}
    rehypePlugins={[[rehypeSanitize, imageSchema]]}
    components={components}
  >{content}</ReactMarkdown>;
}
