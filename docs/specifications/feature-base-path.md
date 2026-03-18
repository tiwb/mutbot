# Base Path 支持 设计规范

**状态**：🔄 实施中
**日期**：2026-03-18
**类型**：功能设计

## 背景

mutbot 目前所有路由硬编码在根路径（`/`）下，无法部署在子路径（如 `https://mutbot.cn/local`）下。实际场景：同一域名下，根路径提供定制的静态落地页，子路径 `/local` 运行 mutbot 应用，由 nginx 反向代理分发。

## 设计方案

### 核心思路

后端：在 mutagent Server 路由层统一 strip base_path 前缀，路由匹配和业务逻辑无感知；需要生成完整 URL 时（重定向、OAuth 回调）加回前缀。

前端：从 `location.pathname` 推导 basePath（因为前端是 hash 路由，pathname 始终等于 basePath），无需后端注入。

### 配置方式

在 `~/.mutbot/config.json` 中新增 `base_path` 字段：

```json
{
  "base_path": "/local"
}
```

- 默认值为 `""` （空字符串，等同于根路径部署）
- 值必须以 `/` 开头，不以 `/` 结尾（如 `/local`、`/app`）
- 空字符串表示根路径，保持向后兼容

### nginx 部署方式

nginx 不 strip 前缀，原样转发给后端（`proxy_pass http://127.0.0.1:8741/local/`）。后端收到的请求路径带 `/local` 前缀，因此后端必须自己 strip。本地通过 `http://localhost:8741/local/` 也能直接访问，不依赖 nginx。

### 后端设计

**mutagent Server 路由层（strip prefix）**

修改 `_server_impl.py` 的 ASGI `route()` 入口：
- Server Declaration 新增 `base_path: str = ""` 属性（mutagent 提供机制）
- `base_path` 为空时跳过 strip 逻辑，所有路径直接通过（向后兼容）
- `base_path` 非空时，检查 `scope["path"]` 是否以 `base_path` 开头
  - 匹配：strip 前缀后传递给路由匹配（path 恰好等于 base_path 时，strip 后为 `/`，即首页）
  - 不匹配：返回 404
- HTTP 和 WebSocket 共用同一个 `scope["path"]`，strip 逻辑写一次即可覆盖
- 路由匹配、View handler、middleware 看到的始终是不含 base_path 的相对路径

**mutbot 配置加载**

`mutbot/web/server.py` 从 `~/.mutbot/config.json` 读取 `base_path`，传递给 Server 实例。

**URL 生成（加回 prefix）**

需要生成完整 URL 的后端场景：
- `auth/views.py` 的 `_get_callback_url()` — OAuth 回调地址
- `auth/middleware.py` 的 302 重定向目标

通过 `base_path + path` 拼接。auth 模块通过 `mutbot.web.server.config` 全局变量读取 `base_path`（与 `_get_auth_config()` 读取 auth 配置的路径一致，无需引入新依赖）。

**Supervisor 路径处理**

Supervisor 在 TCP 层 peek 原始 HTTP 请求行，管理路径硬编码为 `_MANAGEMENT_PATHS = (b"/api/restart", b"/api/eval", b"/health")`。外部请求带 base_path 前缀（如 `/local/api/restart`），需要在 `_is_management_path` 中也做一次 strip 再匹配。同理，`/internal/` 前缀拒绝逻辑（line 302-304）也需要 strip 后再匹配。

Supervisor 从 `~/.mutbot/config.json` 直接读取 `base_path`（Supervisor 已有独立的配置加载逻辑，复用即可）。Supervisor 构造的健康检查直接发给 Worker 的 ASGI handler，需要带 base_path 前缀（Worker 的 strip 逻辑统一适用，不做特例豁免）。

**auth middleware 无需改动**

base_path 在 Server 路由层已 strip，`auth/middleware.py` 的 `before_route` 收到的 `path` 已是相对路径，`_PUBLIC_PREFIXES` 匹配逻辑不变。唯一改动：302 重定向的目标 URL 从 `"/"` 改为 `base_path + "/"`（让浏览器加载部署在子路径下的前端 SPA）。

### 前端设计

**两种模式，统一 helper**：

| 模式 | basePath 来源 |
|------|-------------|
| 本地部署 | `location.pathname`（hash 路由，pathname 始终等于部署路径） |
| 远程 (mutbot.ai) | `__MUTBOT_CONTEXT__.basePath`（launcher.ts 从服务器 URL 提取） |

```typescript
// lib/base-path.ts
const ctx = (window as any).__MUTBOT_CONTEXT__;
const basePath: string = ctx?.basePath ?? location.pathname.replace(/\/$/, "");

export function apiPath(path: string): string {
  return `${basePath}${path}`;
}
```

**mutbot.ai launcher 改动**：

`launcher.ts` 的 `__MUTBOT_CONTEXT__` 注入增加 `basePath` 字段，并让 `wsBase` 包含 pathname：

```typescript
const pathname = url.pathname.replace(/\/$/, "");
(window as any).__MUTBOT_CONTEXT__ = {
  remote: true,
  wsBase: `${wsProtocol}//${url.host}${pathname}`,
  basePath: pathname,
  workspace: ...,
};
```

这样用户在 mutbot.ai 添加 `https://mutbot.cn/local` 时，WebSocket 会正确连到 `wss://mutbot.cn/local/ws/app`。

**需要修改的前端文件**：
- `lib/base-path.ts` — **新增**，basePath 推导 + `apiPath()` helper
- `lib/connection.ts` — `getWsUrl()` 加 basePath 前缀（`app-rpc.ts`、`workspace-rpc.ts` 通过 `getWsUrl` 间接受益，无需直接改动）
- `components/LoginPage.tsx` — `/auth/providers` fetch 路径改用 `apiPath()`
- `App.tsx` — `/auth/logout`、`/auth/userinfo` 等硬编码路径改用 `apiPath()`
- `panels/SessionListPanel.tsx` — `/auth/userinfo` fetch 路径改用 `apiPath()`

> **注意**：远程模式（mutbot.ai）下，`apiPath` 构造的 HTTP 路径仍发往 mutbot.ai origin，不会到达目标服务器。这是已有限制（远程模式的 HTTP API 调用本来就不走目标服务器），不在本次 base_path 的范围内。

### 实施概要

1. mutagent Server Declaration 加 `base_path` 属性，`route()` 入口加 strip 逻辑
2. mutbot config 读取并传递 base_path
3. auth 模块的 URL 生成加 base_path（重定向、OAuth 回调）
4. Supervisor 读取 base_path，`_is_management_path` 和 `/internal/` 拒绝逻辑适配 strip
5. 前端 basePath helper + 各处引用改用 helper
6. mutbot.ai launcher.ts 注入 basePath 和带 pathname 的 wsBase

## 设计决策

- **Q1 StaticView**：strip 在路由匹配之前完成，`_FrontendStatic` 保持 `path = "/"`，无需改动
- **Q2 Supervisor**：外部请求带 base_path（nginx 不 strip），Supervisor 的 `_is_management_path` 和 `/internal/` 拒绝逻辑都需要 strip 后再匹配；Supervisor 从 `~/.mutbot/config.json` 直接读取 base_path；Supervisor 自己构造的健康检查不带前缀，无需改动
- **Q3 配置层级**：mutagent Server Declaration 声明 `base_path` 属性（机制），mutbot 从 config 读取传入（策略）
- **前端 basePath 推导**：前端是 hash 路由，`location.pathname` 始终等于部署路径，直接推导即可，无需后端注入
- **nginx 配置**：nginx 不 strip 前缀（`proxy_pass http://127.0.0.1:8741/local/`），后端自己处理，确保本地 HTTP 也能直接访问

## 关键参考

### 源码

- `mutagent/src/mutagent/net/server.py` — Server/View/WebSocketView Declaration，path 属性声明
- `mutagent/src/mutagent/net/_server_impl.py` — 路由编译 `_compile_path()`、匹配 `_match_route()`、ASGI handler（line 414+）、StaticView 服务（line 171+）；HTTP 和 WebSocket 共用 `scope["path"]`（line 423）
- `mutbot/src/mutbot/web/server.py` — Worker/Supervisor 入口，`_FrontendStatic` 定义（line 443-448）
- `mutbot/src/mutbot/web/supervisor.py` — `_MANAGEMENT_PATHS`（line 25）、TCP peek 路径检测（line 298-315）、健康检查（line 232-253）
- `mutbot/src/mutbot/web/routes.py` — 路由声明：`/api/health`、`/ws/app`、`/ws/workspace/{id}`
- `mutbot/src/mutbot/auth/middleware.py` — `_PUBLIC_PREFIXES`、`before_route` impl
- `mutbot/src/mutbot/auth/views.py` — `_get_callback_url()`（line 98-102）、OAuth 回调路由
- `mutbot/frontend/src/lib/connection.ts` — `getWsUrl()` WebSocket URL 构造（line 20-26）
- `mutbot/frontend/src/lib/app-rpc.ts` — `/ws/app` 引用（line 42）
- `mutbot/frontend/src/lib/workspace-rpc.ts` — `/ws/workspace/{id}` 引用（line 161）
- `mutbot/frontend/src/components/LoginPage.tsx` — `/auth/providers` fetch（line 35）
- `mutbot/frontend/src/main.tsx` — hash 路由入口，`location.pathname` 使用（line 8-13）
- `mutbot/frontend/src/App.tsx` — `/auth/logout` 硬编码路径（line 537）

### 部署配置

- `nginx-mutbot.cn.conf`（D:\ai 根目录）— nginx 不 strip 前缀，`proxy_pass http://127.0.0.1:8741/local/`

### mutbot.ai

- `mutbot.ai/src/scripts/launcher.ts` — `openWorkspace()`（line 280）、`loadReactForVersion()` 注入 `__MUTBOT_CONTEXT__`（line 290-318），`wsBase` 当前只取 host 未包含 pathname（line 303）

## 实施步骤清单

### 阶段一：后端核心 — mutagent Server strip 逻辑 [✅ 已完成]

- [x] **Task 1.1**: Server Declaration 新增 `base_path` 属性
  - `mutagent/src/mutagent/net/server.py` — Server 类加 `base_path: str = ""`
  - 状态：✅ 已完成

- [x] **Task 1.2**: `route()` 入口加 strip 逻辑
  - `mutagent/src/mutagent/net/_server_impl.py` — 在 path 取出后、路由匹配前，strip base_path 前缀
  - base_path 为空时跳过；path 恰好等于 base_path 时 strip 后为 `/`；不匹配时返回 404
  - 状态：✅ 已完成

### 阶段二：后端 — mutbot 配置传递与 URL 生成 [✅ 已完成]

- [x] **Task 2.1**: mutbot config 读取并传递 base_path
  - `mutbot/src/mutbot/web/server.py` — worker_main 和 _standalone_main 中，从 config 读取 base_path 传给 MutBotServer 实例
  - 状态：✅ 已完成

- [x] **Task 2.2**: auth middleware 302 重定向加 base_path
  - `mutbot/src/mutbot/auth/middleware.py` — 重定向目标从 `"/"` 改为 `base_path + "/"`，通过 `mutbot.web.server.config` 读取
  - 状态：✅ 已完成

- [x] **Task 2.3**: auth views OAuth 回调 URL 加 base_path
  - `mutbot/src/mutbot/auth/views.py` — `_get_callback_url()` 拼接 base_path，通过 `mutbot.web.server.config` 读取
  - 状态：✅ 已完成

### 阶段三：后端 — Supervisor 适配 [✅ 已完成]

- [x] **Task 3.1**: Supervisor 读取 base_path 并适配路径检测
  - `mutbot/src/mutbot/web/supervisor.py` — 新增 `base_path` 参数和 `_strip_base_path()` 方法，`_is_management_path` 和 `/internal/` 拒绝逻辑 strip 后再匹配
  - `mutbot/src/mutbot/web/server.py` — supervisor_main 中传递 base_path 给 Supervisor
  - 状态：✅ 已完成

### 阶段四：前端 — basePath helper 与路径改造 [✅ 已完成]

- [x] **Task 4.1**: 新增 `lib/base-path.ts`
  - basePath 推导逻辑 + `apiPath()` helper 导出
  - 状态：✅ 已完成

- [x] **Task 4.2**: `connection.ts` 的 `getWsUrl()` 加 basePath
  - 引入 basePath，本地模式 WebSocket 路径加前缀
  - 状态：✅ 已完成

- [x] **Task 4.3**: HTTP fetch 路径改用 `apiPath()`
  - `LoginPage.tsx` — `/auth/providers`
  - `App.tsx` — `/auth/providers`、`/auth/userinfo`、`/auth/logout`
  - `SessionListPanel.tsx` — `/auth/userinfo`、`/auth/logout` (href)
  - 状态：✅ 已完成

### 阶段五：mutbot.ai launcher 适配 [✅ 已完成]

- [x] **Task 5.1**: launcher.ts 注入 basePath 和带 pathname 的 wsBase
  - `mutbot.ai/src/scripts/launcher.ts` — `__MUTBOT_CONTEXT__` 增加 basePath 字段，wsBase 包含 pathname
  - 状态：✅ 已完成

### 阶段六：验证 [进行中]

- [x] **Task 6.0**: 自动化测试
  - mutagent: 697 passed, 5 skipped
  - mutbot: 489 passed
  - 前端构建：成功
  - 状态：✅ 已完成

- [ ] **Task 6.1**: 本地验证（根路径部署，向后兼容）
  - 不配置 base_path，启动 mutbot，确认功能正常
  - 状态：⏸️ 待开始

- [ ] **Task 6.2**: 本地验证（子路径部署）
  - 配置 `base_path: "/local"`，通过 `http://localhost:8741/local/` 访问，验证页面加载、WebSocket 连接、auth 流程
  - 状态：⏸️ 待开始

## 测试验证

- mutagent 全部测试通过（697 passed, 5 skipped）
- mutbot 全部测试通过（489 passed）
- 前端构建成功
