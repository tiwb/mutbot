/**
 * 后端连接工具 — 根据运行环境决定连接目标。
 *
 * 本地运行时从 location 推导；mutbot.ai 通过 __MUTBOT_CONTEXT__ 注入配置。
 */

interface MutbotContext {
  remote: boolean;
  wsBase: string; // e.g. "ws://localhost:8741"
}

const ctx = (window as any).__MUTBOT_CONTEXT__ as
  | MutbotContext
  | undefined;

/**
 * 构建 WebSocket URL。
 * 有注入时使用注入的 wsBase；否则从当前页面 location 推导。
 */
export function getWsUrl(path: string): string {
  if (ctx) {
    return `${ctx.wsBase}${path}`;
  }
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}${path}`;
}

/** 是否从 mutbot.ai 远程加载 */
export function isRemote(): boolean {
  return !!ctx?.remote;
}