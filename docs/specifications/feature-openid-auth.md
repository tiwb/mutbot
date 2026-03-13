# OpenID 身份验证 设计规范

**状态**：📝 设计中
**日期**：2026-03-09
**类型**：功能设计

## 背景

mutbot 当前无任何身份验证机制，所有 WebSocket 和 HTTP 端点完全开放。当 mutbot 部署到非本机环境（如团队服务器、公网）时，需要身份验证来限制访问。

需求：
1. 支持 OpenID Connect (OIDC) 协议，首先实现 GitHub 作为身份提供商
2. 本地访问（127.0.0.1 / localhost）自动跳过验证
3. 从未配置过验证方式时，也不验证（向后兼容）
4. 可配置多个 OIDC 提供商，可配置允许的用户列表
5. 支持预授权和先登录后审批两种权限模式

### 典型用户场景

**场景 A：新用户，本地使用 → 分享给同事**

```
1. pip install mutbot && mutbot
   → localhost:8741，无 auth 配置 → 全放行，不需要登录
2. 决定分享给同事：
   a. 配置 OIDC 提供商（GitHub 或组织 OpenID）
   b. mutbot --host 0.0.0.0（暴露到局域网）
   c. 配置权限（两种模式，见下文）
3. 同事访问 server_url → 4401 → OIDC 登录 → 权限检查
```

**场景 B：老用户，多台机器统一管理**

```
机器 A（办公室）、机器 B（家里）、机器 C（云服务器）各运行 mutbot
每台独立配置 auth，使用相同的 OIDC 提供商（如 GitHub）
用户通过 mutbot.ai 添加多个服务器，每个服务器独立认证
同一个 GitHub 账号，但每个服务器独立签发 JWT
GitHub OAuth 如果用户之前授权过同一个 app，会自动跳过确认页（接近"一键登录"）
```

**场景 C：从 mutbot.ai 连接启用 auth 的远程服务器**

```
mutbot.ai WebSocket 连接远程服务器 → 收到 4401 关闭码
  → 弹窗/跳转到 server_url/auth/login?return_to=https://mutbot.ai
  → 用户在远程服务器完成 OIDC
  → 服务器签发 JWT，重定向回 mutbot.ai 并通过 URL fragment 传递 token
  → mutbot.ai 提取 token，存入 localStorage（按服务器分存）
  → 重新连接 WebSocket 时带上 ?token=xxx
```

注意：mutbot.ai 是纯前端静态站，无法使用 HttpOnly cookie（跨域）。token 必须通过 URL fragment 或 postMessage 回传，由前端 JS 存储在 localStorage 中。

## 设计方案

### 核心设计

#### 认证架构（三层）

```
请求 → 本地豁免判断 → 认证中间件 → 路由处理
                        ↓
                  未认证 → 登录页面（OIDC 流程）
                  已认证 → 放行 + 用户身份注入
```

**三种访问模式**：
1. **无配置模式**：`auth` 配置节不存在 → 所有请求直接放行（完全向后兼容）
2. **本地豁免模式**：请求来自 127.0.0.1 / ::1 → 自动放行，不需要登录
3. **认证模式**：远程请求 + 有 auth 配置 → 必须通过 OIDC 认证

#### OIDC 登录流程（Authorization Code Flow）

```
用户访问 mutbot
  → 未认证，重定向到 /auth/login?provider=github[&return_to=<url>]
  → 重定向到 GitHub 授权页面
  → 用户授权后回调 /auth/callback?code=xxx&state=yyy
  → 后端用 code 换取 access_token
  → 用 access_token 获取用户信息（user_id, username, email）
  → 检查用户权限（预授权 / 待审批 / 信任模式）
  → 签发 session token（JWT）
  → 回跳：
     a. 无 return_to → 设置 HttpOnly cookie，重定向回原页面（同源访问）
     b. 有 return_to（如 mutbot.ai） → 重定向到 return_to#token=xxx（跨域访问）
```

**`return_to` 参数**：支持外部站点（如 mutbot.ai）发起的认证流程。token 通过 URL fragment 传递（非 query string，避免被服务器日志/Referer 头泄露）。`return_to` 应验证为合法来源（白名单或同一 OIDC 回调域）。

#### Session Token

- 后端签发 JWT，包含：`sub`（用户标识）、`name`、`provider`、`exp`
- 签名密钥：首次启动时自动生成，持久化到 `~/.mutbot/auth_secret`
- 有效期：默认 7 天（可配置）
- 传输方式：
  - HTTP 请求：HttpOnly cookie（`mutbot_token`）
  - WebSocket：连接时 query param `token=xxx`（复用现有 `tokenFn` 机制）

#### 配置结构

```json
{
  "auth": {
    "providers": {
      "github": {
        "client_id": "xxx",
        "client_secret": "xxx"
      }
    },
    "allowed_users": ["octocat", "user2"],
    "session_ttl": 604800
  }
}
```

- `auth` 不存在 → 无认证模式
- `auth.providers` 配置了提供商 → 启用认证
- `auth.allowed_users` 为空或不存在 → 任何通过 OIDC 认证的用户都可以访问（小团队信任模式）
- `auth.allowed_users` 有值 → 列表中的用户直接放行（预授权）；不在列表中但通过了 OIDC → 进入待审批（见下文）
- `auth.session_ttl` 可选，默认 604800（7天），单位秒

#### 权限模式：预授权 + 先登录后审批

两种模式共存，由 `allowed_users` 列表决定行为：

**预授权**：管理员事先将用户加入 `allowed_users` → 用户 OIDC 登录后直接获得访问权限。

**先登录后审批**：用户通过 OIDC 验证身份后，如果不在 `allowed_users` 中 → 进入待审批状态（非直接 403），等待管理员批准。

```
OIDC 登录成功
  → 用户在 allowed_users 中？→ 签发 JWT，放行
  → 用户不在 allowed_users 中？
      → allowed_users 为空/不存在？→ 信任模式，签发 JWT，放行
      → allowed_users 非空？→ 记录为 pending，返回"等待管理员批准"页面
```

**待审批机制**：
- 待审批用户信息存储在 `~/.mutbot/auth_pending.json`（或类似持久化）
- 管理员（本地访问用户）在 UI 中看到待审批列表
- 批准 → 自动加入 `allowed_users`；拒绝 → 从 pending 中移除
- 通知方式：管理员打开 mutbot 时 UI 提示有待审批请求

**GitHub OIDC 特殊处理**：GitHub 不完全支持标准 OIDC，但支持 OAuth2 + userinfo API。实现上使用 OAuth2 Authorization Code Flow + `/user` API 获取用户信息，对外仍统一称为"OIDC 提供商"。

#### 后端新增模块

```
src/mutbot/auth/
  __init__.py
  views.py       — View/WebSocketView 子类：login, callback, logout, userinfo
  token.py       — JWT 签发与验证
  providers.py   — OIDC 提供商抽象 + GitHub 实现
```

认证逻辑通过 `View` 子类的 `before_request()` 或 Server 层请求处理钩子实现（当前框架无中间件机制，需要在 mutagent.net 层增加请求拦截点）。

#### 请求拦截逻辑（替代中间件）

当前 mutbot 使用自研 ASGI 服务器（mutagent.net，基于 h11 + wsproto），**没有中间件系统**。路由通过 mutobj `discover_subclasses(View)` 零注册发现。

认证拦截的实现方向：
1. **Server 层请求钩子**：在 `mutagent.net.Server` 的请求分发逻辑中增加 `before_dispatch` 钩子（Declaration 方法），mutbot 通过 `@impl` 注入认证检查
2. **View 基类方法**：在 `View.handle()` 前增加 `before_request()` 钩子，auth 模块提供 `@impl` 检查 token

#### WebSocket 认证

WebSocket 不能用 HTTP 重定向，处理方式：
- 连接时通过 query param 传递 `token`（现有 `tokenFn` 机制已支持）
- token 无效 → `websocket.close(code=4401, reason="Unauthorized")`
- 前端收到 4401 关闭码 → 跳转登录页面

#### 前端变更

- 新增登录页面组件（显示可用的提供商按钮）
- `tokenFn` 从 cookie 读取 token 传递给 WebSocket
- WebSocket 收到 4401 时重定向到登录页
- 右上角显示当前用户信息 + 退出按钮（认证模式下）

### 实施概要

后端：新增 `auth/` 模块（中间件、路由、token、provider），配置系统增加 auth 节，server.py 注册中间件和路由。前端：登录页面、token 传递、用户信息显示。依赖新增 `PyJWT`。

## 待定问题

### QUEST Q1: GitHub OAuth App 还是 GitHub App？
**问题**：GitHub 提供两种应用类型，用哪种创建 OAuth 认证？
**建议**：使用 GitHub OAuth App（更简单，只需 client_id + client_secret，适合第三方登录场景）。GitHub App 功能更强但复杂度更高，不需要。

### QUEST Q2: `return_to` 白名单策略
**问题**：`/auth/login?return_to=<url>` 中的 `return_to` 应限制为哪些域名，以防止 open redirect 攻击？
**建议**：默认允许 `https://mutbot.ai`，可通过配置扩展。或者只允许 URL fragment 方式回传 token（降低风险）。

### QUEST Q3: 多提供商同时启用时的行为
**问题**：如果配置了多个 OIDC 提供商（如 GitHub + Google），登录页面如何展示？allowed_users 如何区分？
**建议**：登录页面显示所有已配置提供商的按钮。`allowed_users` 使用 `provider:username` 格式区分（如 `"github:octocat"`、`"google:user@gmail.com"`）。也支持不带前缀的简写（默认匹配所有提供商的同名用户）。

### QUEST Q4: 签名密钥管理
**问题**：JWT 签名密钥自动生成并持久化到 `~/.mutbot/auth_secret`，还是放在 config.json 中？
**建议**：自动生成到独立文件 `~/.mutbot/auth_secret`（不混入 config.json，避免被意外编辑或导出）。首次启用认证时自动创建。

### QUEST Q5: 前端登录页的实现方式
**问题**：登录页面是前端 React 组件（SPA 内），还是后端渲染的独立 HTML？
**建议**：后端返回简单的独立 HTML 页面（不依赖前端构建）。原因：未认证时前端 JS bundle 不应暴露；独立页面更简单，不需要前端路由配合。

## 关键参考

### 源码
- `mutagent/src/mutagent/net/server.py` — Server/View/WebSocketView Declaration（自研 ASGI 框架，无 FastAPI）
- `mutagent/src/mutagent/net/_server_impl.py` — Server 请求分发实现（认证拦截点需在此层增加）
- `mutagent/src/mutagent/net/_protocol.py` — HTTP/WS 协议处理（h11 + wsproto）
- `src/mutbot/web/server.py` — MutBotServer，继承 mutagent.net.Server
- `src/mutbot/web/routes.py` — View 子类（HealthView、WebSocket 端点等，零注册发现）
- `src/mutbot/runtime/config.py` — MutbotConfig 配置系统（支持 on_change 回调）
- `src/mutbot/copilot/auth.py` — 现有 GitHub Copilot OAuth 设备流实现（仅用于 LLM API 认证，非 Web 访问控制）
- `frontend/src/lib/websocket.ts` — ReconnectingWebSocket，已有 tokenFn 机制（`?token=xxx`）
- `frontend/src/lib/connection.ts` — getWsUrl()，isRemote()
- `frontend/src/lib/app-rpc.ts` — AppRpc 构造函数已有 tokenFn 参数

### 关键发现
- 前端 WebSocket 已预留 token 传递机制（`tokenFn` → query param `token=xxx`），已有完整基础设施但未接入实际 token 源
- 当前 Web 框架为自研 ASGI（mutagent.net），无中间件系统，路由通过 mutobj `discover_subclasses(View)` 零注册发现
- 认证路由（`/auth/*`）可作为 `View` 子类自动注册，无需手动添加
- 默认 host 是 `127.0.0.1`，需用 `--host 0.0.0.0` 才会暴露到网络
- config 支持 `on_change` 回调，auth 配置变更可实时生效
- `copilot/auth.py` 中的 GitHub OAuth 是设备流（CLI 交互式），不适用于 Web 登录场景，但可参考 token 管理模式

### 相关规范
- `mutbot.ai/docs/specifications/feature-remote-server.md` — mutbot.ai 多服务器连接（纯前端），4401 处理和 token 存储与本规范交互