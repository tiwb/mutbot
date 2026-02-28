/**
 * 后端连接工具 — 根据运行环境决定连接目标。
 *
 * 本地运行时使用当前 host，远程（mutbot.ai）时连接 localhost:8741。
 */

/**
 * 获取 mutbot 后端 host。
 * 本地运行时用当前 host，远程（mutbot.ai）时连 localhost:8741。
 */
export function getMutbotHost(): string {
  const h = location.hostname;
  if (h === "localhost" || h === "127.0.0.1" || h === "::1") {
    return location.host;
  }
  return "localhost:8741";
}

/**
 * 构建 WebSocket URL。
 * 连接目标始终是本地 mutbot，使用 ws://（非 TLS）。
 */
export function getWsUrl(path: string): string {
  const host = getMutbotHost();
  return `ws://${host}${path}`;
}

/** 是否从远程（非 localhost）访问 */
export function isRemote(): boolean {
  const h = location.hostname;
  return h !== "localhost" && h !== "127.0.0.1" && h !== "::1";
}
