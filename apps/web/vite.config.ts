/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import packageJson from "./package.json" with { type: "json" };

// The Web App talks to the Backend on its own origin only (Decision 0044): in
// development Vite forwards /api to the Backend on the loopback and keeps the
// browser's Host (changeOrigin: false), so the session cookie and the Backend's
// Origin check see one origin, exactly as in production. The Backend's default
// listener (PAW_HOST / PAW_PORT) is 127.0.0.1:8000.
const backend = "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  // Shown in the user menu and on the sign-in page (the design's "host · v0.1.0").
  define: { __APP_VERSION__: JSON.stringify(packageJson.version) },
  server: {
    host: "localhost",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: backend, changeOrigin: false, ws: true },
    },
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    // No inline scripts / styles: the Backend's CSP for the app is script-src 'self'.
    assetsInlineLimit: 0,
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    restoreMocks: true,
  },
});
