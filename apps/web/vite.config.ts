import { fileURLToPath, URL } from "node:url";
import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { VitePWA } from "vite-plugin-pwa";

export default defineConfig({
  plugins: [
    react(),
    VitePWA({
      registerType: "autoUpdate",
      includeAssets: ["favicon.svg", "apple-touch-icon.png", "icon-192.png", "icon-512.png", "icon-maskable-512.png"],
      manifest: {
        name: "Agent Control",
        short_name: "Agent Control",
        description: "Secure mobile control for multi-agent infrastructure.",
        theme_color: "#071018",
        background_color: "#071018",
        display: "standalone",
        start_url: "/chats",
        scope: "/",
        icons: [
          { src: "/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
          { src: "/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
          { src: "/icon-maskable-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" }
        ]
      },
      workbox: {
        navigateFallback: "/index.html",
        navigateFallbackDenylist: [/^\/api\//, /^\/ws\//],
        cleanupOutdatedCaches: true,
        // Realtime dictation cannot work offline. Keep its large, lazy SDK out
        // of the install-time PWA cache; normal HTTP caching applies on demand.
        globIgnores: ["**/vendor-scribe-*.js"],
        runtimeCaching: []
      }
    })
  ],
  resolve: {
    alias: [
      { find: "@elevenlabs/scribe-core", replacement: fileURLToPath(new URL("../../node_modules/@elevenlabs/client/dist/scribe/index.js", import.meta.url)) },
      { find: "@elevenlabs/scribe-registry", replacement: fileURLToPath(new URL("../../node_modules/@elevenlabs/client/dist/scribe/microphone.js", import.meta.url)) },
      { find: "@elevenlabs/scribe-web-microphone", replacement: fileURLToPath(new URL("../../node_modules/@elevenlabs/client/dist/platform/web/scribeMicrophone.js", import.meta.url)) },
      { find: "@hermes-control/ui/styles.css", replacement: fileURLToPath(new URL("../../packages/ui/src/styles.css", import.meta.url)) },
      { find: "@hermes-control/ui", replacement: fileURLToPath(new URL("../../packages/ui/src/index.tsx", import.meta.url)) },
      { find: "@", replacement: fileURLToPath(new URL("./src", import.meta.url)) }
    ]
  },
  server: {
    host: "0.0.0.0",
    allowedHosts: ["terminal.local"],
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", ws: true, changeOrigin: false },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true, changeOrigin: false }
    }
  },
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          "vendor-scribe": ["./src/lib/elevenlabsScribeClient.ts"],
          "vendor-react": ["react", "react-dom", "zustand"],
          "vendor-router": ["@tanstack/react-router", "@tanstack/react-query", "@tanstack/react-virtual"],
          "vendor-markdown": ["react-markdown", "remark-gfm", "rehype-sanitize"],
          "vendor-forms": ["react-hook-form", "@hookform/resolvers", "zod"],
          "vendor-client": ["dexie", "i18next", "react-i18next"],
          "vendor-icons": ["@phosphor-icons/react"],
        },
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    exclude: [...configDefaults.exclude, "tests/e2e/**"],
    css: true
  }
});
