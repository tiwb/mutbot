import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import { readFileSync, writeFileSync } from "fs";
import { resolve } from "path";

/**
 * Extract content hash from the main JS bundle and inject as
 * window.__BUILD_HASH__ into the built index.html.
 */
function buildHashPlugin(): Plugin {
  return {
    name: "build-hash",
    apply: "build",
    closeBundle() {
      const outDir = resolve(__dirname, "../src/mutbot/web/frontend_dist");
      const indexPath = resolve(outDir, "index.html");
      let html: string;
      try {
        html = readFileSync(indexPath, "utf-8");
      } catch {
        return;
      }
      const m = html.match(/assets\/index-([A-Za-z0-9_-]+)\.js/);
      const hash = m ? m[1] : "unknown";
      // Inject script before the first <script> tag
      const script = `<script>window.__BUILD_HASH__="${hash}";</script>`;
      html = html.replace("<head>", `<head>\n    ${script}`);
      writeFileSync(indexPath, html);
    },
  };
}

export default defineConfig({
  plugins: [react(), buildHashPlugin()],
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
