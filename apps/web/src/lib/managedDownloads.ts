export interface ManagedDownloads {
  version: string;
  macosArm64?: { url: string; sha256: string; minOsVersion: "13" };
  linux?: { installerUrl: string };
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : undefined;
}

function downloadUrl(value: unknown, origin: string, extension: string): string | undefined {
  if (typeof value !== "string") return undefined;
  try {
    const url = new URL(value, origin);
    if (url.origin !== origin || url.username || url.password || url.search || url.hash
      || !url.pathname.startsWith("/downloads/agent-control/") || !url.pathname.endsWith(extension)) return undefined;
    return url.href;
  } catch { return undefined; }
}

export function parseManagedDownloads(value: unknown, origin: string): ManagedDownloads | null {
  const manifest = record(value);
  if (manifest?.schemaVersion !== 1 || typeof manifest.version !== "string" || !/^[a-f0-9]{40}$/.test(manifest.version)) return null;
  const downloads = record(manifest.downloads);
  if (!downloads) return null;
  const result: ManagedDownloads = { version: manifest.version };
  const mac = record(downloads.macosArm64);
  const macUrl = downloadUrl(mac?.url, origin, ".dmg");
  if (macUrl && mac?.minOsVersion === "13" && typeof mac.sha256 === "string" && /^[a-f0-9]{64}$/.test(mac.sha256)) {
    result.macosArm64 = { url: macUrl, sha256: mac.sha256, minOsVersion: "13" };
  }
  const linuxUrl = downloadUrl(record(downloads.linux)?.installerUrl, origin, "/install.sh");
  if (linuxUrl && new URL(linuxUrl).pathname === "/downloads/agent-control/install.sh") result.linux = { installerUrl: linuxUrl };
  return result;
}

export async function loadManagedDownloads(signal: AbortSignal): Promise<ManagedDownloads | null> {
  const response = await fetch("/downloads/agent-control/latest.json", { signal, cache: "no-store", credentials: "omit", redirect: "error" });
  if (!response.ok) return null;
  return parseManagedDownloads(await response.json(), window.location.origin);
}

export function managedInstallCommand(downloads: ManagedDownloads): string | undefined {
  if (!downloads.linux) return undefined;
  const origin = new URL(downloads.linux.installerUrl).origin;
  return `curl --proto '=https' --tlsv1.2 -fsSL ${downloads.linux.installerUrl} | sh -s -- --server ${origin}`;
}
