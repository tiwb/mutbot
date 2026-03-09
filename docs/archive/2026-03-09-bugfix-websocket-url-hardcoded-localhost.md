# WebSocket 连接地址不应写死 localhost 设计规范

**状态**：✅ 已完成
**日期**：2026-03-09
**类型**：Bug修复

## 背景

mutbot 前端 `connection.ts` 中 `getMutbotHost()` 的逻辑：当页面不是从 localhost 访问时，回退到硬编码的 `"localhost:8741"`。这个逻辑只适用于 mutbot.ai（远程网站连接本地 mutbot），但对 mutbot 自身是错误的。

当用户通过局域网 IP（如 `192.168.1.100:8741`）访问 mutbot 时，WebSocket 会尝试连接 `ws://localhost:8741`，导致连接失败。

## 设计方案

### 核心设计

**问题根源**：mutbot 前端同时被两个场景使用（mutbot 本地服务 + mutbot.ai 动态加载），当前通过 hostname 嗅探区分场景，导致局域网 IP 访问被误判为 mutbot.ai 模式。

**方案：mutbot.ai launcher 注入上下文配置**

由 mutbot.ai 的 launcher 在加载 React 应用前注入全局配置，React 应用根据是否存在注入来决定行为：

- **有注入** → mutbot.ai 加载模式，使用注入的 `wsBase` 构建 WebSocket URL
- **无注入** → mutbot 本地模式，从 `location` 推导（host + 协议）

这样 mutbot 前端零特殊逻辑，mutbot.ai 完全控制远程行为（包括协议），不需要改协议。

### 具体变更

#### 1. mutbot.ai launcher — 注入上下文

在 `mutbot.ai/src/scripts/launcher.ts` 中，加载 React 应用前设置全局变量：

```typescript
// 挂载 React 应用前注入
window.__MUTBOT_CONTEXT__ = {
  remote: true,
  wsBase: "ws://localhost:8741",  // 完整协议+host，launcher 完全控制
};
```

协议放在注入里而非让前端推导，因为 mutbot.ai 页面是 HTTPS 但本地 mutbot 是 WS（非 TLS），前端用 `location.protocol` 会拼出错误的 `wss://localhost:8741`。

#### 2. mutbot 前端 `connection.ts` — 基于注入配置

```typescript
// 改前
export function getMutbotHost(): string {
  const h = location.hostname;
  if (h === "localhost" || h === "127.0.0.1" || h === "::1") {
    return location.host;
  }
  return "localhost:8741";
}

export function getWsUrl(path: string): string {
  const host = getMutbotHost();
  return `ws://${host}${path}`;
}

export function isRemote(): boolean {
  const h = location.hostname;
  return h !== "localhost" && h !== "127.0.0.1" && h !== "::1";
}

// 改后
interface MutbotContext {
  remote: boolean;
  wsBase: string;   // e.g. "ws://localhost:8741"
}

const ctx = (window as any).__MUTBOT_CONTEXT__ as MutbotContext | undefined;

export function getWsUrl(path: string): string {
  if (ctx) {
    return `${ctx.wsBase}${path}`;
  }
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${location.host}${path}`;
}

export function isRemote(): boolean {
  return !!ctx?.remote;
}
```

`getMutbotHost()` 不再需要，删除。所有调用方已经通过 `getWsUrl()` 获取完整 URL。

#### 3. 后端 `routes.py` — 移除 Origin 校验

mutbot 是本地工具，默认允许任何站点连接 WebSocket。删除 `_ALLOWED_ORIGINS`、`_check_ws_origin()` 函数，以及所有调用处的 Origin 检查逻辑。

### 实施概要

mutbot.ai launcher 加 `__MUTBOT_CONTEXT__` 注入；mutbot 前端 `connection.ts` 改为读注入配置，删除 `getMutbotHost()`；后端删除 Origin 校验。

## 关键参考

### 源码
- `frontend/src/lib/connection.ts` — WebSocket URL 构建（问题所在）
- `src/mutbot/web/routes.py:68-82` — Origin 校验逻辑
- `D:\ai\mutbot.ai\src\scripts\launcher.ts:11-12` — mutbot.ai 的 launcher 入口

### 相关
- `frontend/src/App.tsx:37,179,979` — `isRemote()` 的 3 处调用
- `frontend/src/lib/app-rpc.ts:42` — 使用 `getWsUrl("/ws/app")`
- `frontend/src/lib/workspace-rpc.ts:135` — 使用 `getWsUrl("/ws/workspace/...")`
- `frontend/src/panels/LogPanel.tsx:33` — 使用 `getWsUrl("/ws/logs")`

## 实施步骤清单

### 阶段 1：mutbot 前端修复 [✅ 已完成]

- [x] **Task 1.1**: 重写 `connection.ts`
  - [x] 删除 `getMutbotHost()`
  - [x] `getWsUrl()` 改为：有 `__MUTBOT_CONTEXT__` 注入时用 `ctx.wsBase`，否则从 `location` 推导
  - [x] `isRemote()` 改为 `return !!ctx?.remote`
  - [x] 添加 `MutbotContext` 接口定义
  - 状态：✅ 已完成

- [x] **Task 1.2**: 验证 `getMutbotHost` 无其他调用方
  - [x] 确认只在 `connection.ts` 内部使用（仅 `getWsUrl` 调用）
  - 状态：✅ 已完成

### 阶段 2：mutbot 后端移除 Origin 校验 [✅ 已完成]

- [x] **Task 2.1**: 清理 `routes.py` 中的 Origin 校验
  - [x] 删除 `_ALLOWED_ORIGINS` 常量
  - [x] 删除 `_check_ws_origin()` 函数
  - [x] 删除 `websocket_app` 中的 Origin 检查逻辑
  - [x] 删除 `Origin 校验` 注释块
  - 状态：✅ 已完成

### 阶段 3：mutbot.ai launcher 注入 [✅ 已完成]

- [x] **Task 3.1**: 在 `launcher.ts` 的 `loadReactForVersion()` 中，加载 script 前注入 `window.__MUTBOT_CONTEXT__`
  - [x] 设置 `{ remote: true, wsBase: "ws://localhost:8741" }`
  - 状态：✅ 已完成

### 阶段 4：构建验证 [✅ 已完成]

- [x] **Task 4.1**: 前端构建通过 `npm --prefix frontend run build`
  - 状态：✅ 已完成

## 完善程度评估

**覆盖完整**：所有影响点已确认——
- `getMutbotHost()` 仅在 `connection.ts` 内部被 `getWsUrl()` 调用，无外部引用
- `_check_ws_origin()` 仅在 `websocket_app` 一处调用
- `isRemote()` 的 3 处调用（App.tsx）语义不变，只是判断来源改为读注入

**风险评估**：低风险
- 改动集中在 3 个文件，逻辑简单
- mutbot.ai 的注入是新增代码，不影响现有逻辑（`loadReactForVersion` 加载 script 前设置全局变量即可）
- 唯一注意点：mutbot.ai 的改动需要单独在 mutbot.ai 仓库提交，且需要发布新版 mutbot 前端后 mutbot.ai 才能生效（因为 mutbot.ai 加载的是已发布的版本化前端）。过渡期（新前端 + 旧 mutbot.ai）不受影响——没有注入时前端用 `location` 推导，行为正确
