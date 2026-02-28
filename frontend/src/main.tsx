import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./index.css";

// 兼容两种挂载场景：
// 1. 独立 SPA（/v0.1.0/index.html 中 <div id="root">）
// 2. 嵌入 Landing Page（mutbot.ai / 中 <div id="app">）
const container = document.getElementById("root") ?? document.getElementById("app");
if (container) {
  createRoot(container).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}
