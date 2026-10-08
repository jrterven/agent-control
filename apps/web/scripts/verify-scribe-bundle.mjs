import { readdir, readFile } from "node:fs/promises";
import { join } from "node:path";

const assetsDirectory = new URL("../dist/assets/", import.meta.url);
const forbiddenRuntimeMessages = [
  "Unknown message type:",
  "Failed to parse WebSocket message:",
  "WebSocket closed: code=",
  "Raw payload:",
  "Failed to start microphone streaming:",
];

const files = (await readdir(assetsDirectory)).filter((name) => name.endsWith(".js"));
const bundles = await Promise.all(files.map(async (name) => ({
  name,
  source: await readFile(join(assetsDirectory.pathname, name), "utf8"),
})));
const scribeBundles = bundles.filter(({ source }) => source.includes("Invalid WebSocket message"));

for (const message of forbiddenRuntimeMessages) {
  const leakingBundle = bundles.find(({ source }) => source.includes(message));
  if (leakingBundle) {
    throw new Error(`Sensitive Scribe logging string found in ${leakingBundle.name}: ${message}`);
  }
}

if (scribeBundles.length !== 1 || !scribeBundles[0].name.startsWith("vendor-scribe-")) {
  throw new Error("The built Scribe chunk does not include Agent Control's bounded-frame hardening.");
}

const serviceWorker = await readFile(new URL("../dist/sw.js", import.meta.url), "utf8");
for (const bundle of bundles) {
  // A shared helper accidentally assigned to the lazy chunk makes startup
  // import the SDK even though its original source uses dynamic import().
  const staticImports = [...bundle.source.matchAll(/\b(?:from\s*|import\s*)["'](\.\/[^"']+)["']/g)];
  if (staticImports.some((match) => match[1] === `./${scribeBundles[0].name}`)) {
    throw new Error(`Offline startup cannot statically import the Scribe SDK from ${bundle.name}.`);
  }
}
if (serviceWorker.includes(scribeBundles[0].name)) {
  throw new Error("The offline service worker must not precache the realtime-only Scribe SDK.");
}

console.log(`Verified: ${scribeBundles[0].name} is lazy, bounded, log-safe, and excluded from PWA precache.`);
