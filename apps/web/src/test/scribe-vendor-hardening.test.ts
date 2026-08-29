import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

const repositoryRoot = resolve(process.cwd(), "../..");
const installedConnection = resolve(repositoryRoot, "node_modules/@elevenlabs/client/dist/scribe/connection.js");
const installedScribe = resolve(repositoryRoot, "node_modules/@elevenlabs/client/dist/scribe/scribe.js");
const rootPackage = resolve(repositoryRoot, "package.json");
const reproduciblePatch = resolve(repositoryRoot, "patches/@elevenlabs+client+1.23.0.patch");

describe("vendored Scribe privacy hardening", () => {
  it("applies the reproducible patch before bundling the exact SDK version", () => {
    const connection = readFileSync(installedConnection, "utf8");
    const scribe = readFileSync(installedScribe, "utf8");
    const root = JSON.parse(readFileSync(rootPackage, "utf8")) as { scripts?: Record<string, string> };
    const patch = readFileSync(reproduciblePatch, "utf8");

    expect(root.scripts?.postinstall).toBe("patch-package --error-on-fail");
    expect(patch).toContain("event.data.length > 65536");
    expect(connection).toContain('typeof event.data !== "string" || event.data.length > 65536');
    expect(connection).toContain('this._emitError(new Error("Invalid WebSocket message"))');
    expect(connection).not.toContain('console.warn("Unknown message type:", data)');
    expect(connection).not.toContain('console.error("Failed to parse WebSocket message:", error, event.data)');
    expect(connection).not.toContain("WebSocket closed: code=");
    expect(scribe).not.toContain('console.error("Failed to start microphone streaming:", error)');
  });
});
