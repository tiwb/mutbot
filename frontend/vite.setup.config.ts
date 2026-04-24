import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

// 独立的 setup 页构建配置：
//   - 单 entry，输出 setup.js + setup.css 到与主 bundle 同一个 frontend_dist
//   - emptyOutDir: false，避免清空主 bundle
//   - 无 external，依赖（mutgui、antd、react）全部内联到 setup.js
//   - define process.env.NODE_ENV：library 模式 vite 不会自动注入，
//     react/antd 内部依赖此变量，缺它浏览器报 "process is not defined"
export default defineConfig({
  plugins: [react()],
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  build: {
    outDir: "../src/mutbot/web/frontend_dist",
    emptyOutDir: false,
    cssCodeSplit: false,
    lib: {
      entry: resolve(__dirname, "setup/index.tsx"),
      formats: ["iife"],
      name: "MutBotSetup",
      fileName: () => "setup.js",
    },
    rollupOptions: {
      output: {
        assetFileNames: (asset) => {
          if (asset.name && asset.name.endsWith(".css")) return "setup.css";
          return "assets/setup-[name][extname]";
        },
      },
    },
  },
});
