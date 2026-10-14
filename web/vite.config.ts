/// <reference types="vitest" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The built app is served by Flask from hpgbooks/static/app at /app/.
// `npm run dev` proxies API calls to a local `bin/hpgbooks web` on 8765.
const backend = "http://127.0.0.1:8765";

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    sourcemap: false,
    // No inline scripts or data: URIs, so the strict CSP holds.
    assetsInlineLimit: 0,
    modulePreload: { polyfill: false },
    chunkSizeWarningLimit: 600,
  },
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: Object.fromEntries(
      ["/api", "/export", "/healthz"].map((path) => [
        path,
        {
          target: backend,
          changeOrigin: true,
          // Dev only: the browser Origin is the Vite port, which Flask would reject.
          configure: (proxy) => {
            proxy.on("proxyReq", (req) => req.removeHeader("origin"));
          },
        },
      ]),
    ),
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
  },
});
