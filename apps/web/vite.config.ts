import { configDefaults, defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // The API is a separate service in every environment, so the dev server
    // proxies rather than the app hard-coding a host. VITE_API_URL overrides it
    // for a deployed build, where the two are on different origins.
    proxy: {
      // ws:true so the room socket is proxied too -- without it the handshake
      // 404s in development and the room silently never goes live.
      "/api": {
        target: process.env.VITE_PROXY_TARGET ?? "http://localhost:8010",
        changeOrigin: true,
        ws: true,
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test-setup.ts"],
    globals: true,
    // Vitest's default glob also matches `e2e/*.spec.ts`, which are Playwright
    // tests -- loading them here throws "did not expect test.describe()". They
    // run under Playwright, not Vitest.
    exclude: [...configDefaults.exclude, "e2e/**"],
  },
});
