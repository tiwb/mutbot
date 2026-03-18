/**
 * Base path 推导 — 支持子路径部署（如 /local）。
 *
 * 本地部署：hash 路由下 pathname 始终等于部署路径。
 * 远程 (mutbot.ai)：从 __MUTBOT_CONTEXT__.basePath 获取。
 */

const ctx = (window as any).__MUTBOT_CONTEXT__ as
  | { basePath?: string }
  | undefined;

export const basePath: string =
  ctx?.basePath ?? location.pathname.replace(/\/$/, "");

/** 为 HTTP API 路径加上 basePath 前缀。 */
export function apiPath(path: string): string {
  return `${basePath}${path}`;
}
