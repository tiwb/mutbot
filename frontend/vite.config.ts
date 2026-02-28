import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: "./",
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://localhost:8741",
        changeOrigin: true,
      },
      "/ws": {
        target: "ws://localhost:8741",
        ws: true,
      },
    },
  },
  build: {
    outDir: "../src/mutbot/web/frontend_dist",
    emptyOutDir: true,
  },
});
