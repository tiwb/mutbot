import { createRoot } from "react-dom/client";
import { useEffect, useRef, useState } from "react";
import { ConfigProvider, theme as antdTheme } from "antd";
import * as antd from "antd";
import {
  MutguiView,
  registerComponents,
  registerCommands,
  resolveCommand,
  ConnectionProvider,
  type MutguiConnection,
  type ViewPath,
  type RenderCallback,
} from "@mutgui/core";
import "@mutgui/core/styles.css";
import "./theme-dark.css";

// 与 mutgui demo 一致的暗色主题
document.body.classList.add("mutgui-dark");

const darkAntdTheme = {
  algorithm: antdTheme.darkAlgorithm,
  token: {
    colorPrimary: "#007acc",
  },
};

// 注册 antd 命名空间（$component 使用 "antd.Button" / "antd.Input" 等）
registerComponents({
  __name__: "antd",
  ...(antd as unknown as Record<string, unknown>),
});

registerCommands({
  __name__: "mutgui",
  redirect: ({ url, replace }: { url: string; replace?: boolean }) => {
    if (replace) {
      window.location.replace(url);
      return;
    }
    window.location.href = url;
  },
});

function createConnection(ws: WebSocket): MutguiConnection {
  const subs = new Map<string, RenderCallback>();
  const cache = new Map<string, unknown[]>();

  ws.addEventListener("message", (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "render") {
      const viewId: ViewPath = msg.viewId || [];
      const key = JSON.stringify(viewId);
      cache.set(key, msg.tree);
      const cb = subs.get(key);
      if (cb) cb(msg.tree);
      return;
    }

    if (msg.type === "command") {
      const viewId: ViewPath = msg.viewId || [];
      const cmd = resolveCommand(msg.name);
      if (!cmd) {
        console.warn(`[mutbot setup] Unknown command: ${String(msg.name)}`, msg);
        return;
      }
      cmd((msg.args || {}) as Record<string, unknown>, { viewId });
    }
  });

  return {
    send: (data: string) => ws.send(data),
    subscribe: (viewId: ViewPath, callback: RenderCallback) => {
      const key = JSON.stringify(viewId);
      subs.set(key, callback);
      const cached = cache.get(key);
      if (cached) callback(cached);
      return () => subs.delete(key);
    },
  };
}

function App({ wsUrl }: { wsUrl: string }) {
  const [status, setStatus] = useState("Connecting...");
  const [conn, setConn] = useState<MutguiConnection | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    const ws = new WebSocket(wsUrl);
    wsRef.current = ws;
    const connection = createConnection(ws);
    ws.onopen = () => {
      setStatus("Connected");
      setConn(connection);
    };
    ws.onclose = () => {
      setStatus("Disconnected — please refresh the page");
      setConn(null);
    };
    ws.onerror = () => setStatus("Connection error");
    return () => ws.close();
  }, [wsUrl]);

  if (!conn) {
    return (
      <div style={{ padding: 24, color: "#888", fontSize: 14 }}>{status}</div>
    );
  }

  return (
    <ConfigProvider theme={darkAntdTheme}>
      <ConnectionProvider value={conn}>
        <MutguiView />
      </ConnectionProvider>
    </ConfigProvider>
  );
}

const el = document.getElementById("app");
if (el) {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  const wsUrl = `${proto}//${location.host}/auth/setup/ws`;
  createRoot(el).render(<App wsUrl={wsUrl} />);
}
