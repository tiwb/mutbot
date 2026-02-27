# 移除 REST 端点 — 全面迁移至 WebSocket 设计规范

**状态**：✅ 已完成
**日期**：2026-02-27
**类型**：重构

## 1. 背景

mutbot 在之前的重构（`2026-02-24-refactor-runtime-declaration`）中已将大部分 REST API 迁移到 WebSocket RPC，但仍保留了以下 REST 端点：

| REST 端点 | 方法 | 用途 | 状态 |
|-----------|------|------|------|
| `/api/health` | GET | 健康检查、setup_required | 移除 |
| `/api/auth/status` | GET | 检查是否需要认证 | 移除（认证暂不实现） |
| `/api/auth/login` | POST | 登录获取 token | 移除（认证暂不实现） |
| `/api/workspaces` | GET | 列出工作区 | 移除（已有 `workspace.list` RPC） |
| `/api/workspaces` | POST | 创建工作区 | 移除（已有 `workspace.create` RPC） |

`/llm/*` 下的 LLM 代理端点为外部工具提供 OpenAI/Anthropic 兼容 API，**不在本次移除范围内**。

本次重构同时**移除认证系统**（AuthManager、AuthMiddleware、LoginScreen），目前处于免认证阶段，未来再设计认证和账号连接方案。

## 2. 设计方案

### 2.1 核心思路

- 移除所有 `/api/*` REST 端点
- 移除完整的认证系统（后端 AuthManager/AuthMiddleware + 前端 LoginScreen/token 管理）
- `/ws/app` 连接建立后推送 `welcome` 事件，携带应用状态（如 `setup_required`）
- 前端启动流程简化为：连接 `/ws/app` → 收到 welcome → 加载工作区

### 2.2 `/ws/app` welcome 事件

服务端在 `accept()` 后主动推送：

```json
{
  "type": "event",
  "event": "welcome",
  "data": {
    "setup_required": true
  }
}
```

- `setup_required`：providers 未配置时为 true（原 `/api/health` 的职责）

### 2.3 前端启动流程变更

**当前流程**（REST + WS + 认证）：

```
1. fetch /api/auth/status → 是否需要认证
2. 如需认证 → 展示 LoginScreen → POST /api/auth/login
3. 认证通过 → 连接 /ws/app → workspace.list → 选择工作区
```

**新流程**（纯 WS，无认证）：

```
1. 连接 /ws/app
2. 收到 welcome 事件 → 获取 setup_required 等状态
3. workspace.list → 选择工作区
```

移除 `authChecked` / `authRequired` / `authenticated` 三个状态变量，App 组件直接在 mount 时连接 `/ws/app`。

### 2.4 AppRpc 事件监听

当前 `AppRpc.handleMessage()` 只处理 `rpc_result` / `rpc_error`，需增加 `event` 类型处理：

```typescript
// 新增事件回调机制
private listeners = new Map<string, Set<(data: unknown) => void>>();

on(event: string, cb: (data: unknown) => void): () => void { ... }

private handleMessage(msg) {
  if (msg.type === "event") {
    // 分发给注册的监听器
  }
  // ... 原有 rpc_result / rpc_error 处理
}
```

### 2.5 移除清单

**后端移除**：
- `web/auth.py`：整个文件删除（AuthManager）
- `web/routes.py`：删除 5 个 REST handler（`health`、`auth_status`、`auth_login`、`list_workspaces`、`create_workspace`）及 `_get_auth_manager()` 辅助函数
- `web/server.py`：删除 `AuthMiddleware` 类、`_AUTH_SKIP_PATHS`、`_AUTH_SKIP_PREFIXES`、`auth_manager` 全局变量、`app.add_middleware(AuthMiddleware)` 调用、startup 中的 `AuthManager` 初始化

**后端新增**：
- `web/routes.py`：`websocket_app()` 中 accept 后推送 welcome 事件

**前端移除**：
- `lib/api.ts`：整个文件删除（所有函数都不再需要）
- `App.tsx`：删除 `LoginScreen` 组件、auth 相关 state（`authChecked`/`authRequired`/`authenticated`）、`checkAuthStatus()` 调用
- `App.tsx`：移除 `getAuthToken` import，不再传递 `tokenFn` 给 AppRpc / WorkspaceRpc
- `panels/AgentPanel.tsx`：移除 `getAuthToken` import，不再传递 `tokenFn`
- `panels/TerminalPanel.tsx`：移除 `getAuthToken` import，不再在 WS URL 中附加 token

**前端变更**：
- `lib/app-rpc.ts`：增加事件监听机制
- `App.tsx`：启动流程简化，直接连接 `/ws/app`

**保留不动**：
- `lib/websocket.ts`：`tokenFn` 参数保留（基础设施，未来认证可复用）
- `lib/app-rpc.ts`：构造函数 `tokenFn` 参数保留
- `lib/workspace-rpc.ts`：构造函数 `tokenFn` 参数保留

## 3. 待定问题

无（已全部确认）。

- ~~Q1: health 检查~~ → 完全移除，无外部监控依赖
- ~~Q2: `/api/*` 路径~~ → 不做特殊处理，FastAPI 默认 404
- ~~Q3: 认证流程~~ → 完全移除，以后重新设计

## 4. 实施步骤清单

### 阶段一：后端变更 [✅ 已完成]

- [x] **Task 1.1**: `/ws/app` 推送 welcome 事件
  - [x] accept 后推送 `{"type": "event", "event": "welcome", "data": {"setup_required": ...}}`
  - [x] `setup_required` 检查 provider 配置（复用原 health 端点逻辑）
  - 状态：✅ 已完成

- [x] **Task 1.2**: 删除 REST 端点
  - [x] 删除 `routes.py` 中的 `health()`、`auth_status()`、`auth_login()`、`list_workspaces()`、`create_workspace()`
  - [x] 删除 `_get_auth_manager()` 辅助函数
  - 状态：✅ 已完成

- [x] **Task 1.3**: 删除认证系统
  - [x] 删除 `web/auth.py` 文件
  - [x] 删除 `server.py` 中的 `AuthMiddleware`、`_AUTH_SKIP_PATHS`、`_AUTH_SKIP_PREFIXES`、`auth_manager` 全局变量
  - [x] 删除 startup 中的 `AuthManager()` 初始化
  - [x] 删除 `app.add_middleware(AuthMiddleware)`
  - [x] 清理 `from mutbot.web.auth import AuthManager` import
  - 状态：✅ 已完成

### 阶段二：前端变更 [✅ 已完成]

- [x] **Task 2.1**: AppRpc 增加事件监听
  - [x] 新增 `on(event, callback)` 方法，返回 unsubscribe 函数
  - [x] `handleMessage` 中处理 `type: "event"` 消息并分发
  - 状态：✅ 已完成

- [x] **Task 2.2**: App.tsx 启动流程简化
  - [x] 删除 `LoginScreen` 组件
  - [x] 删除 `authChecked` / `authRequired` / `authenticated` state
  - [x] 删除 `checkAuthStatus()` useEffect
  - [x] 移除对 `authenticated` 的条件判断，直接连接 `/ws/app`
  - [x] 移除传给 AppRpc/WorkspaceRpc 的 `tokenFn: getAuthToken`
  - 状态：✅ 已完成

- [x] **Task 2.3**: 清理前端认证相关代码
  - [x] 删除 `lib/api.ts` 整个文件
  - [x] `panels/AgentPanel.tsx`：移除 `getAuthToken` import 和 `tokenFn` 传参
  - [x] `panels/TerminalPanel.tsx`：移除 `getAuthToken` import 和 token 附加逻辑
  - 状态：✅ 已完成

### 阶段三：测试验证 [✅ 已完成]

- [x] **Task 3.1**: 后端验证
  - [x] 后端 import 无报错
  - [x] 无 `/api/*` 路由残留
  - 状态：✅ 已完成

- [x] **Task 3.2**: 前端构建与运行验证
  - [x] `npm run build` 无报错
  - 状态：✅ 已完成

## 5. 测试验证

### 单元测试
- [ ] `/ws/app` welcome 事件包含正确的 `setup_required`
- [ ] `workspace.list` / `workspace.create` RPC 无需认证即可调用

### 集成测试
- [ ] 完整流程：连接 /ws/app → welcome → workspace.list → 选择工作区 → 连接 /ws/workspace/{id}
- [ ] setup wizard 触发与完成
