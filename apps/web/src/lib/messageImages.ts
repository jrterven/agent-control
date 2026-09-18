/** Only these opaque references may load an image inside an agent response. */
export type ImageReference = { id: string; alt: string };
const referencePattern = /^ac-media:([0-9a-f]{32})$/;

type MarkdownNode = {
  type: string;
  value?: string;
  url?: string;
  alt?: string;
  children?: MarkdownNode[];
  data?: { hName: string; hProperties: Record<string, string> };
};

function gallery(references: ImageReference[]): MarkdownNode {
  return { type: "imageGallery", data: { hName: "div", hProperties: { dataImageRefs: JSON.stringify(references) } }, children: [] };
}

/** Transform parsed Markdown, so code samples and escaped syntax stay literal. */
export function remarkMessageImages({ streaming = false, maxImagesPerGallery = 6, maxImagesPerResponse = 24 }: {
  streaming?: boolean; maxImagesPerGallery?: number; maxImagesPerResponse?: number;
} = {}) {
  const gallerySize = Number.isInteger(maxImagesPerGallery) ? Math.max(1, Math.min(6, maxImagesPerGallery)) : 6;
  const responseSize = Number.isInteger(maxImagesPerResponse) ? Math.max(1, Math.min(24, maxImagesPerResponse)) : 24;
  return (tree: MarkdownNode) => {
    let imageCount = 0;
    const transform = (parent: MarkdownNode) => {
      if (!parent.children) return;
      const output: MarkdownNode[] = [];
      for (const node of parent.children) {
        if (node.type !== "paragraph") {
          transform(node);
          output.push(node);
          continue;
        }
        let prose: MarkdownNode[] = [];
        let images: ImageReference[] = [];
        const flushProse = () => {
          if (prose.some((child) => child.type !== "text" || child.value?.trim())) output.push({ ...node, children: prose });
          prose = [];
        };
        const flushImages = () => {
          if (!images.length) return;
          const previous = output.at(-1);
          const previousImages = previous?.type === "imageGallery"
            ? JSON.parse(previous.data!.hProperties.dataImageRefs) as ImageReference[] : [];
          if (previousImages.length && previousImages.length < gallerySize) {
            const joined = [...previousImages, ...images.splice(0, gallerySize - previousImages.length)];
            output[output.length - 1] = gallery(joined);
          }
          while (images.length) output.push(gallery(images.splice(0, gallerySize)));
        };
        for (const child of node.children ?? []) {
          const match = child.type === "image" ? referencePattern.exec(child.url ?? "") : null;
          if (match && imageCount < responseSize) {
            flushProse();
            images.push({ id: match[1], alt: child.alt ?? "" });
            imageCount += 1;
          } else if (images.length && child.type === "text" && !child.value?.trim()) {
            // Whitespace, including a blank Markdown paragraph, does not break a gallery.
          } else {
            flushImages();
            if (streaming && child.type === "text") {
              child.value = child.value?.replace(/!\[[^\]\n]*(?:\](?:\([^\)\n]*)?)?$/, "");
            }
            prose.push(child);
          }
        }
        flushImages();
        flushProse();
      }
      parent.children = output;
    };
    transform(tree);
  };
}

export function safeImageSource(value: string | undefined): string | undefined {
  if (!value) return undefined;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password ? url.href : undefined;
  } catch { return undefined; }
}
