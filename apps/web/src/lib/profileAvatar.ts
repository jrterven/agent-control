const supportedAvatarTypes = new Set(["image/jpeg", "image/png", "image/webp"]);
const AVATAR_EDGE = 512;
const AVATAR_MAX_BYTES = 900_000;

function loadImage(source: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("AVATAR_DECODE_FAILED"));
    image.src = source;
  });
}

function canvasBlob(canvas: HTMLCanvasElement, quality: number): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => blob ? resolve(blob) : reject(new Error("AVATAR_ENCODE_FAILED")), "image/jpeg", quality);
  });
}

/** Center-crop and bound phone photos before they cross the one-megabyte API boundary. */
export async function prepareProfileAvatar(file: File): Promise<Blob> {
  if (!supportedAvatarTypes.has(file.type)) throw new Error("AVATAR_UNSUPPORTED_TYPE");
  const source = URL.createObjectURL(file);
  try {
    const image = await loadImage(source);
    if (!image.naturalWidth || !image.naturalHeight) throw new Error("AVATAR_DECODE_FAILED");
    const canvas = document.createElement("canvas");
    canvas.width = AVATAR_EDGE;
    canvas.height = AVATAR_EDGE;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("AVATAR_ENCODE_FAILED");
    context.fillStyle = "#ffffff";
    context.fillRect(0, 0, AVATAR_EDGE, AVATAR_EDGE);
    context.imageSmoothingEnabled = true;
    context.imageSmoothingQuality = "high";
    const sourceEdge = Math.min(image.naturalWidth, image.naturalHeight);
    const sourceX = (image.naturalWidth - sourceEdge) / 2;
    const sourceY = (image.naturalHeight - sourceEdge) / 2;
    context.drawImage(image, sourceX, sourceY, sourceEdge, sourceEdge, 0, 0, AVATAR_EDGE, AVATAR_EDGE);
    let blob = await canvasBlob(canvas, 0.88);
    if (blob.size > AVATAR_MAX_BYTES) blob = await canvasBlob(canvas, 0.72);
    if (blob.size > AVATAR_MAX_BYTES) throw new Error("AVATAR_TOO_LARGE");
    return blob;
  } finally {
    URL.revokeObjectURL(source);
  }
}
