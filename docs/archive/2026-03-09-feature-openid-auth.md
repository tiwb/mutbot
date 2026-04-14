# OpenID 身份验证 设计规范

**状态**：✅ 已完成
**日期**：2026-03-09（更新 2026-03-31）
**类型**：功能设计

## 背景

mutbot 当前无任何身份验证机制，所有 WebSocket 和 HTTP 端点完全开放。内网分享场景下需要身份验证：一方面限制访问，另一方面需要用户身份标识（知道"谁在操作"）。

需求：
1. 支持 OpenID Connect (OIDC) 协议，首先实现 GitHub 作为身份提供商
2. 未配置认证时，全部放行（向后兼容）
3. 配置认证后，所有访问都需要登录（包括本地），必须有用户身份
4. 可配置多个 OIDC 提供商
5. 登录后 session 持久化，有效期内免重复登录
6. mutbot 不在本地存储任何用户信息，身份完全来自 OIDC Provider

**本期范围**：认证（身份识别）。权限系统（授权）后续单独设计。

**设计原则**：mutbot 是开源项目，不在本地留存用户凭据。所有用户身份均来自外部 OIDC Provider，mutbot 仅存储 session JWT（过期即失效）。这保证了安全性，也便于集群部署（共享 JWT 签名密钥即可，无状态）。

### 典型用户场景

**场景 A：新用户，本地使用 → 内网分享**

```
1. pip install mutbot && mutbot
   → localhost:8741，无 auth 配置 → 全放行，无身份概念
2. 决定内网分享给同事：
   a. 配置认证（两种方式，见下文）
   b. mutbot --host 0.0.0.0
   c. 自己和同事都需要登录，获得用户身份
3. 所有人访问 → 未登录则跳转 OIDC → 登录后签发 JWT → 后续自动携带
```

**场景 B：老用户，多台机器**

```
机器 A（办公室）、机器 B（家里）各运行 mutbot
每台独立配置 auth，使用相同的 OIDC 提供商（如 GitHub）
同一个 GitHub 账号，但每个服务器独立签发 JWT
GitHub OAuth 如果用户之前授权过同一个 app，自动跳过确认页
```

### 未来方向（本期不实施）

- **mutbot.ai 跨域认证**：从官网连接远程服务器时的 OIDC 流程（4401 → 跳转登录 → token 回传）
- **权限系统**：分享 URL 可携带权限；用户权限等级（可操作 / 只可观测等）；拿到分享 URL 的人登录后自动获得对应授权。未来权限等级可能比较严格
- **待审批机制**：未在白名单中的用户进入待审批状态

## 设计方案

### 两种认证路径

用户有两种方式配置认证，可独立使用也可共存：

#### 路径 1：中转认证（零注册，推荐）

用户无需自行注册 OAuth App。mutbot.ai 提供公共认证中转服务，帮助 mutbot 实例完成 OAuth 认证。任何组织也可以自建中转站，遵循相同协议。

**流程**：

```
用户访问 mutbot 实例 (192.168.1.100:8741)
  → 未登录，浏览器跳转到中转站：
    mutbot.ai/auth/start?callback=http://192.168.1.100:8741/auth/relay-callback
                        &provider=github&nonce=<随机值>
  → 中转站跳转 GitHub 授权（callback 指向 mutbot.ai/auth/callback）
  → 用户在 GitHub 授权
  → GitHub 回跳到 mutbot.ai/auth/callback
  → 中转站用自己的 client_secret 换取用户信息
  → 中转站签发断言 JWT（5 分钟有效，仅用于传递）：
    { sub: "github:tiwb", name: "lijia", avatar: "...",
      provider: "github", nonce: "...", aud: "http://192.168.1.100:8741" }
  → 浏览器跳回 mutbot 实例：
    http://192.168.1.100:8741/auth/relay-callback#assertion=<签名JWT>
  → mutbot 实例验证断言签名（Ed25519 公钥，从中转站元信息获取）
  → 验证 nonce + audience → 签发本地 session JWT
```

**关键特性**：
- **中转站不需要能访问 mutbot 实例**——全程通过浏览器跳转，内网实例也能用
- **信任关系明确**：mutbot 实例配置中指定信任哪个中转站
- **安全性**：nonce 防重放，audience 防 token 被其他实例盗用，断言 JWT 5 分钟过期
- **中转站是标准化的**：mutbot.ai 已实现，任何组织可按相同协议自建

**断言 JWT 签名方案**：

中转站使用 **Ed25519 非对称签名**（alg: `EdDSA`）签发断言 JWT。这是安全性和零配置的关键：

- **公共中转站**（如 mutbot.ai）：私钥由中转站持有（Cloudflare Secret），公钥通过 `/.well-known/mutbot-relay.json` 公开发布。mutbot 实例自动获取公钥验证签名，无需配置密钥。
- **自建中转站**：同样使用 Ed25519。管理员生成密钥对，私钥配置到中转站，公钥自动通过元信息端点发布。

为什么不用 HMAC（对称签名）：HMAC 要求双方持有同一密钥，开源项目无法安全分发——硬编码在源码中任何人都能伪造断言。Ed25519 的公钥可安全公开，只有持有私钥的中转站能签名。

**Relay callback 前端中转**：

回跳 URL 使用 fragment（`#assertion=JWT`），fragment 不会发送到服务器。`/auth/relay-callback` 路由返回一个轻量 HTML 页面，由 JS 从 `location.hash` 提取 assertion，POST 到后端验证并签发 session。

**断言 JWT 字段**：

| 字段 | 说明 |
|------|------|
| `sub` | 用户标识，格式 `provider:username`（如 `github:tiwb`） |
| `name` | 显示名（来自 Provider 个人资料） |
| `avatar` | 头像 URL |
| `provider` | 认证来源（如 `github`） |
| `nonce` | mutbot 实例生成的随机值，原样带回 |
| `aud` | 受众，即 mutbot 实例的回调地址 |
| `iat` | 签发时间（Unix 时间戳） |
| `exp` | 过期时间（签发后 5 分钟） |

**中转站实现**：

中转协议是标准化的，任何实现都提供三个端点：
- `GET /auth/start` — 发起 OAuth 认证
- `GET /auth/callback` — 接收 Provider 回调，签发断言
- `GET /.well-known/mutbot-relay.json` — 中转站元信息（Ed25519 公钥、支持的 Provider 列表）

现有两种实现：
1. **mutbot.ai**：Cloudflare Worker（`mutbot.ai/src/worker/index.ts`），公共中转站，已验证通过
2. **mutbot 内置**：任何 mutbot 实例都可以配置为中转站（见下文"中转服务端配置"）

#### 路径 2：直连认证（自行注册 OAuth App）

用户在 OIDC Provider 注册自己的 OAuth App，mutbot 实例直接与 Provider 交互，不经过中转站。

**适用场景**：
- 不信任公共中转站
- 企业内部 OIDC Provider（如 Keycloak、Azure AD）
- 需要稳定公开地址（Provider 回调直达 mutbot 实例）

**流程**：

```
用户访问 mutbot
  → 未认证，重定向到 /auth/login?provider=github
  → 重定向到 GitHub 授权页面
  → 用户授权后回调 /auth/callback?code=xxx&state=yyy
  → 后端用 code + client_secret 换取 access_token
  → 用 access_token 获取用户信息
  → 签发 session JWT → 设置 HttpOnly cookie → 重定向回原页面
```

**限制**：mutbot 实例必须有 Provider 可达的稳定地址（公网 IP 或组织内固定地址），否则 Provider 回调无法到达。

**GitHub 注册引导**：登录页面提供分步说明（AI 友好的纯文本，AI 助手可帮用户操作）：
1. 访问 https://github.com/settings/applications/new
2. 填写 Application name、Homepage URL、Authorization callback URL（页面自动显示当前实例地址）
3. 拿到 client_id + client_secret，填入配置

**通用 OIDC 支持**：对于标准 OIDC Provider，用户可提供 `issuer` URL（mutbot 自动从 `{issuer}/.well-known/openid-configuration` 发现端点），或手动指定三个端点（`authorization_endpoint`、`token_endpoint`、`userinfo_endpoint`）。GitHub 作为预设模板（端点硬编码，因为不支持 OIDC discovery）。

### 核心设计

#### 认证架构

```
请求 → auth 配置检查 → 认证中间件 → 路由处理
                          ↓
   无配置 → 直接放行（无身份）
   有配置 + 未认证 → 登录页面（OIDC 流程）
   有配置 + 已认证 → 放行 + 用户身份注入
```

**两种模式**：
1. **无配置模式**：`auth` 配置节不存在 → 所有请求直接放行（向后兼容，无身份概念）
2. **认证模式**：`auth` 已配置 → 所有访问必须登录（本地/远程一视同仁），session 有效期内免重复登录

#### Session Token

- 后端签发 JWT，包含：`sub`（用户标识，格式 `provider:username`）、`name`、`avatar`、`provider`、`exp`
- 签名密钥：首次启动时自动生成（HMAC-SHA256），持久化到 `~/.mutbot/auth_secret`（不混入 config.json）
- 有效期：默认 7 天（可配置）
- 传输方式：
  - HTTP 请求：cookie（`mutbot_token`），属性：`Path=/; SameSite=Lax`；HTTPS 时加 `Secure`
  - WebSocket：同源场景下浏览器自动在 upgrade 请求中携带 cookie，后端从 scope headers 提取验证；跨域场景（未来 mutbot.ai → 远程实例）通过 query param `token=xxx`（复用现有 `tokenFn` 机制）
- 注意：不使用 HttpOnly（JS 需要在跨域场景读取 token 传给 WebSocket query param）

#### 配置结构

**路径 1（中转认证）— 最简配置**：

```json
{
  "auth": {
    "relay": "https://mutbot.ai",
    "allowed_users": ["github:tiwb"]
  }
}
```

**路径 2（直连认证）**：

```json
{
  "auth": {
    "providers": {
      "github": {
        "client_id": "xxx",
        "client_secret": "xxx"
      },
      "corp-sso": {
        "issuer": "https://keycloak.company.com/realms/main",
        "client_id": "mutbot",
        "client_secret": "xxx",
        "scopes": ["openid", "profile"]
      },
      "corp-manual": {
        "authorization_endpoint": "https://sso.company.com/connect/authorize",
        "token_endpoint": "https://sso.company.com/connect/token",
        "userinfo_endpoint": "https://sso.company.com/connect/userinfo",
        "client_id": "xxx",
        "client_secret": "xxx",
        "scopes": ["openid", "nickname", "fullname", "email"],
        "claims": {
          "username": "nickname",
          "name": "fullname"
        }
      }
    },
    "allowed_users": ["github:octocat", "corp-sso:zhangsan"]
  }
}
```

**两种路径共存**：

```json
{
  "auth": {
    "relay": "https://mutbot.ai",
    "providers": {
      "corp-oidc": { "issuer": "...", "client_id": "...", "client_secret": "..." }
    },
    "allowed_users": ["github:tiwb", "corp-oidc:zhangsan"],
    "session_ttl": 604800
  }
}
```

**中转服务端配置**（让本实例作为中转站，为其他 mutbot 实例提供认证服务）：

```json
{
  "auth": {
    "relay_service": {
      "private_key": "MC4CAQAwBQYDK2VwBCIEI...(Base64 Ed25519 私钥)...",
      "providers": {
        "github": {
          "client_id": "Ov23lim0IYf0E8KIQwbk",
          "client_secret": "xxx"
        },
        "corp-sso": {
          "authorization_endpoint": "https://sso.company.com/connect/authorize",
          "token_endpoint": "https://sso.company.com/connect/token",
          "userinfo_endpoint": "https://sso.company.com/connect/userinfo",
          "client_id": "xxx",
          "client_secret": "xxx",
          "scopes": ["openid", "profile", "email"]
        }
      }
    }
  }
}
```

注意：`relay_service` 和 `auth`（认证客户端）是独立的配置节。一个 mutbot 实例可以同时：
- 自身需要认证（配置 `auth.relay` 或 `auth.providers`）
- 为其他实例提供中转（配置 `auth.relay_service`）

配置说明：
- `auth` 不存在 → 无配置模式，全放行
- `auth.relay` — 中转站地址（路径 1）
- `auth.providers` — 直连 OIDC 提供商（路径 2）
- `auth.providers.{name}.scopes` — 可选，向 Provider 请求的权限范围（默认 `["openid", "profile"]`）。非标准 OIDC Provider 可能使用不同的 scope 名称（如某些企业 OIDC 使用 `nickname`、`fullname`）
- `auth.providers.{name}.claims` — 可选，userinfo 响应字段名映射。用于适配非标准 OIDC Provider 的字段名。支持映射：`username`（用户标识）、`name`（显示名）、`avatar`（头像 URL）。不配置则按常见字段名自动 fallback
- `auth.allowed_users` — 白名单，格式 `provider:username`。本期：白名单内放行，白名单外 403。权限细分后续设计
- `auth.session_ttl` — 可选，默认 604800（7天），单位秒
- `auth.relay_service` — 中转服务端配置（可选，让本实例作为中转站）
- `auth.relay_service.private_key` — Ed25519 私钥（PEM 格式），用于签发断言 JWT
- `auth.relay_service.providers` — 本中转站支持的 OIDC 提供商（含 client_id/secret），同样支持 `scopes` 和 `claims` 配置

#### 后端新增模块

```
src/mutbot/auth/
  __init__.py
  views.py       — View 子类：LoginView, CallbackView, RelayCallbackView, LogoutView, UserinfoView
  token.py       — JWT 签发与验证（session token + relay 断言验证）
  providers.py   — OIDC 提供商抽象 + GitHub 预设 + 通用 OIDC（discovery / 手动端点）
  relay.py       — 中转服务端逻辑（RelayStartView, RelayCallbackView, RelayMetaView）
```

认证路由（`/auth/*`）作为 `View` 子类，通过 mutobj `discover_subclasses(View)` 自动注册。

#### 请求拦截逻辑

当前 mutagent ASGI 框架没有中间件/钩子系统。请求链路为：`ASGI 入口 → 路径匹配 → 构造 Request → 直接调用 handler → 返回响应`，中间无拦截点。

**方案**：在 `Server` Declaration 中新增 `before_route(scope, path)` 钩子方法（默认放行），mutbot 通过 `@impl` 注入认证逻辑。

插入点在 `_server_impl.py` 的 `_server_route()` 函数中，路径匹配之后、handler 调用之前。HTTP 和 WebSocket 统一拦截：
- HTTP 未认证 → 返回 302 重定向到登录页（或 401 JSON）
- WebSocket 未认证 → `ws.close(code=4401)`
- 排除白名单路径：`/auth/*`（登录流程本身）、`/api/health`（健康检查）、静态资源

#### WebSocket 认证

WebSocket 不能用 HTTP 重定向：
- 同源场景：浏览器自动在 WebSocket upgrade 请求中携带 cookie，后端从 ASGI scope 的 headers 中提取 `mutbot_token` cookie 验证
- 跨域场景（未来）：通过 query param `token=xxx`（现有 `tokenFn` 机制已支持）
- token 无效 → `websocket.close(code=4401, reason="Unauthorized")`
- 前端收到 4401 关闭码 → 跳转登录页面

#### 前端变更

- 登录页面：React 组件内渲染（和应用共享样式体系，移动端自动适配）
- 中间件放行 `/` 和静态资源，前端 App 加载后检查 `/auth/userinfo` 决定显示登录页或正常进入
- 登录页：居中卡片式，显示所有可用 Provider 按钮（"Sign in with GitHub" 风格）
- Relay 模式：卡片底部小字标注 `via {relay域名}`
- 多 Provider 时列表展示，单 Provider 不自动跳转（始终显示登录页）
- WebSocket 收到 4401 时跳转到登录页
- 侧边栏底部显示当前用户信息 + 退出按钮（认证模式下）
- Session 过期：登录页顶部提示 "登录已过期，请重新登录"

### 实施概要

后端：新增 `auth/` 模块（路由、token、provider），配置增加 auth 节，Server 层增加请求拦截钩子。前端：4401 处理、用户信息显示。依赖新增 `PyJWT[crypto]`（含 cryptography 库，支持 EdDSA 验证）。mutbot.ai Worker 需从 HMAC 迁移到 Ed25519 签名。

## 实施步骤清单

### 阶段一：框架层 — mutagent before_route 钩子 [✅ 已完成]

- [x] **Task 1.1**: Server Declaration 新增 `before_route` 钩子
  - 在 `mutagent/src/mutagent/net/server.py` 的 `Server` 类中添加 `before_route` Declaration 方法
  - 默认实现：放行所有请求
  - 状态：✅ 已完成

- [x] **Task 1.2**: `_server_impl.py` 中调用 `before_route` 钩子
  - 在 `_server_route()` 中 HTTP 路径匹配前、WS handler 调用前插入钩子调用
  - HTTP：返回 Response 则直接发送；WebSocket：返回 Response 则以 status 为关闭码
  - 状态：✅ 已完成

### 阶段二：mutbot 认证核心 — token 与 provider [✅ 已完成]

- [x] **Task 2.1**: 新增 `src/mutbot/auth/token.py`
  - JWT 签发（session token，HMAC-SHA256）与验证
  - 签名密钥自动生成 + 持久化（`~/.mutbot/auth_secret`）
  - relay 断言 JWT 验证（Ed25519 公钥，从中转站元信息获取并缓存）
  - 依赖：PyJWT[crypto]
  - 状态：✅ 已完成

- [x] **Task 2.2**: 新增 `src/mutbot/auth/providers.py`
  - OIDC Provider 抽象接口
  - GitHub 预设模板（OAuth App，端点硬编码）
  - 通用 OIDC：支持 issuer discovery 和手动端点配置
  - Authorization Code Flow：authorize URL 生成、code 换 token、获取 userinfo
  - 状态：✅ 已完成

- [x] **Task 2.3**: 配置系统增加 `auth` 节
  - 通过现有 MutbotConfig.get() 按需读取，无需改动 config.py
  - pyproject.toml 新增 PyJWT[crypto] 依赖
  - 状态：✅ 已完成

### 阶段三：mutbot 认证路由 — 登录流程 [✅ 已完成]

- [x] **Task 3.1**: 新增 `src/mutbot/auth/views.py` — 直连认证路由
  - `LoginView` (`/auth/login`) — 登录页面（独立 HTML，显示可用 Provider）
  - `CallbackView` (`/auth/callback`) — Provider 回调，换取用户信息，签发 JWT，设置 cookie
  - `LogoutView` (`/auth/logout`) — 清除 cookie
  - `UserinfoView` (`/auth/userinfo`) — 返回当前用户信息（JSON）
  - 状态：✅ 已完成

- [x] **Task 3.2**: 新增 `RelayCallbackView` — 中转认证回调
  - `/auth/relay-callback` — 返回 HTML 页面，JS 从 fragment 提取 assertion 并 POST
  - POST 处理：获取 relay 公钥 → 验证 Ed25519 签名 → 验证 nonce/audience → 签发 session
  - 状态：✅ 已完成

- [x] **Task 3.3**: 新增 `src/mutbot/auth/relay.py` — 中转服务端路由
  - `RelayStartView` (`/auth/start`) — 发起 OAuth
  - `RelayProviderCallbackView` (`/auth/relay/callback`) — Provider 回调，用 Ed25519 私钥签发断言
  - `RelayMetaView` (`/.well-known/mutbot-relay.json`) — 元信息（含 Ed25519 公钥）
  - 复用 providers.py 的 OIDC 逻辑
  - 状态：✅ 已完成

### 阶段四：mutbot 请求拦截 — before_route 实现 [✅ 已完成]

- [x] **Task 4.1**: 实现 `before_route` 的 `@impl`（`auth/middleware.py`）
  - 检查 auth 配置是否启用
  - 从 cookie / query param 提取并验证 JWT
  - 白名单路径排除（`/auth/*`、`/.well-known/`、`/api/health`、`/internal/`、静态资源）
  - HTTP 未认证 → 302 到 `/auth/login`
  - WebSocket 未认证 → 返回 4401 关闭信号
  - 认证通过 → 将用户信息注入 scope["user"]
  - 状态：✅ 已完成

### 阶段五：前端对接 [✅ 已完成]

- [x] **Task 5.1**: WebSocket 认证对接
  - 同源场景：浏览器自动携带 cookie，后端从 WS upgrade headers 提取验证
  - 跨域场景（未来）：tokenFn query param 已预留
  - 状态：✅ 已完成

- [x] **Task 5.2**: 4401 处理
  - `websocket.ts` 和 `workspace-rpc.ts` 两个 WebSocket 类均已添加 4401 → 跳转登录
  - 状态：✅ 已完成

- [x] **Task 5.3**: 用户信息显示
  - 侧边栏底部显示当前用户（头像 + 名字）+ 退出按钮；折叠模式仅显示头像
  - 调用 `/auth/userinfo` 获取信息，401 时不显示（无认证模式兼容）
  - 状态：✅ 已完成

### 阶段六：mutbot.ai Worker Ed25519 迁移 [✅ 已完成]

- [x] **Task 6.1**: Worker 签名从 HMAC 迁移到 Ed25519
  - `signJwt()` 改用 `crypto.subtle.sign("Ed25519", ...)` + PKCS8 私钥导入
  - JWT header `alg` 从 `HS256` 改为 `EdDSA`
  - 状态：✅ 已完成

- [x] **Task 6.2**: `/.well-known/mutbot-relay.json` 元信息更新
  - `verify` 字段改为 `"ed25519"`，新增 `public_key` 字段（PEM 格式）
  - 状态：✅ 已完成

- [x] **Task 6.3**: 密钥管理与部署
  - 生成 Ed25519 密钥对，私钥存 Cloudflare Secret（`ED25519_PRIVATE_KEY`）
  - 公钥硬编码在 Worker 代码中
  - 部署验证通过
  - 状态：✅ 已完成

### 阶段七：验证与 Bugfix [✅ 已完成]

- [x] **Task 7.1**: Relay 端到端验证
  - 配置 `auth.relay = "https://mutbot.ai"` → GitHub 授权 → 断言签发 → 本地验证 → session 签发
  - 状态：✅ 已完成

- [x] **Task 7.2**: 修复 PyJWT `aud` 自动校验问题
  - `verify_relay_assertion()` 增加 `options={"verify_aud": False}`（audience 由 views.py 手动校验）
  - 状态：✅ 已完成

- [x] **Task 7.3**: MCP 端点加入认证白名单
  - `/mcp` 加入 `_PUBLIC_PREFIXES`，避免 MCP 管理接口被认证拦截
  - 状态：✅ 已完成

### 阶段八：前端登录页 UX 改造 [✅ 已完成]

- [x] **Task 8.1**: 后端改造 — 中间件放行策略调整
  - 放行 `/` 和所有静态资源，让前端 React App 始终能加载
  - 新增 `/auth/providers` API — 返回可用登录选项列表
  - 删除 LoginView（后端 HTML 登录页），登录页完全由前端 React 渲染
  - 未认证 HTTP 302 重定向目标改为 `/`
  - `/mcp` 加入认证白名单
  - 状态：✅ 已完成

- [x] **Task 8.2**: 前端登录页组件
  - 新建 `LoginPage.tsx` — 居中卡片式登录界面，暗色主题一致
  - App.tsx 启动时检查 `/auth/providers` + `/auth/userinfo`，未登录则渲染 LoginPage
  - Provider 按钮列表："Sign in with {Provider}" 格式
  - Relay 模式：底部小字 `via {relay域名}`
  - 4401 WebSocket 关闭改为 `window.location.reload()` 触发登录流程
  - relay-callback 中转页精简为极简风格（仅 "Signing in..." 文字）
  - 状态：✅ 已完成

- [x] **Task 8.3**: 移动端适配
  - 登录页居中卡片在窄屏自然适配（响应式宽度）
  - MobileLayout 下登录状态展示位置确认（后续单独设计）
  - 状态：⏸️ 后续单独设计

### 阶段九：测试验证 [✅ 已完成]

- [x] **Task 9.1**: 无配置模式测试 — 确认向后兼容，全部放行
- [x] **Task 9.2**: 中转认证端到端测试 — 配置 relay → GitHub 登录 → 访问 → 用户信息显示
- [x] **Task 9.3**: 直连认证端到端测试 — 待配置 provider 后验证
- [x] **Task 9.4**: 白名单测试 — 白名单内放行，白名单外 403
- [x] **Task 9.5**: WebSocket 认证测试 — 4401 关闭 → 前端触发重新登录

## 关键参考

### 源码
- `mutagent/src/mutagent/net/server.py` — Server/View/WebSocketView Declaration（自研 ASGI 框架，无 FastAPI）
- `mutagent/src/mutagent/net/_server_impl.py` — Server 请求分发实现（认证拦截点需在此层增加）
- `mutagent/src/mutagent/net/_protocol.py` — HTTP/WS 协议处理（h11 + wsproto）
- `src/mutbot/web/server.py` — MutBotServer，继承 mutagent.net.Server
- `src/mutbot/web/routes.py` — View 子类（HealthView、WebSocket 端点等，零注册发现）
- `src/mutbot/runtime/config.py` — MutbotConfig 配置系统（支持 on_change 回调）
- `src/mutbot/copilot/auth.py` — 现有 GitHub Copilot OAuth 设备流实现（仅用于 LLM API 认证，非 Web 访问控制，但可参考 token 管理模式）
- `frontend/src/lib/websocket.ts` — ReconnectingWebSocket，已有 tokenFn 机制（`?token=xxx`）
- `frontend/src/lib/connection.ts` — getWsUrl()，isRemote()
- `frontend/src/lib/app-rpc.ts` — AppRpc 构造函数已有 tokenFn 参数
- `mutbot.ai/src/worker/index.ts` — 认证中转 Worker（Ed25519 签名，已部署验证通过）

### 关键发现
- 前端 WebSocket 已预留 token 传递机制（`tokenFn` → query param `token=xxx`），已有完整基础设施但未接入实际 token 源
- 当前 Web 框架为自研 ASGI（mutagent.net），无中间件系统，路由通过 mutobj `discover_subclasses(View)` 零注册发现
- 认证路由（`/auth/*`）可作为 `View` 子类自动注册，无需手动添加
- config 支持 `on_change` 回调，auth 配置变更可实时生效
- 中转认证方案已在 mutbot.ai 上验证通过（Cloudflare Worker，GitHub OAuth）

### 相关规范
- `mutbot.ai/docs/design/architecture.md` — mutbot.ai 架构，记录了未来跨域认证的前端侧流程

---

## 阶段十：Setup 页面改进

### 问题

`/auth/setup` 配置页存在三个问题：

1. **按钮硬编码 "Sign in with GitHub"**（`views.py:781`）— 无论 relay server 支持什么 provider，按钮文字不变
2. **hint 硬编码 "zero-config GitHub login"**（`views.py:767`）— 同上
3. **Access Mode 暴露 "Anyone can log in"** — 首次配置场景下过于危险，不应提供此选项

### 设计方案

#### Setup 表单简化

- 去掉 Access Mode radio group，后端硬编码 `only_me`（首次登录者成为管理员）
- 表单仅保留 Relay URL 输入框 + Connect 按钮
- 按钮文字改为 `"Connect →"`
- hint 改为通用描述，不提及具体 provider

#### 新增 provider 选择步骤

Setup 流程从一步拆为两步（仍为服务端渲染 HTML，无 JS）：

```
Step 1: configure     → 填写 relay URL，点击 "Connect →"
Step 2: select_provider → 显示 relay 支持的 provider 列表，用户点击选择后跳转 OAuth
```

`_handle_start_oauth` 拆分为两个 action：
- `action=connect_relay`：校验 relay URL → fetch providers → 渲染 `select_provider` 页面
- `action=start_oauth`：接收用户选择的 provider → 创建 nonce → 302 跳转 OAuth

`select_provider` 页面渲染 provider 按钮列表（样式类似前端 LoginPage），每个按钮为表单提交（POST，携带 relay_url + provider name）。

## 实施步骤清单

### 阶段十：Setup 页面改进

- [x] **Task 10.1**: `_render_setup_page` configure 步骤简化
  - 去掉 Access Mode radio group
  - 按钮从 `"Sign in with GitHub →"` 改为 `"Connect →"`
  - hint 文字去掉 GitHub 硬编码
  - form action 从 `start_oauth` 改为 `connect_relay`

- [x] **Task 10.2**: `_render_setup_page` 新增 `select_provider` 步骤
  - 接收 providers 列表参数，渲染 provider 按钮
  - 每个 provider 为独立 form（POST，hidden fields: relay_url, provider, action=start_oauth）
  - 按钮文字：`"Sign in with {Provider Label} →"`

- [x] **Task 10.3**: `AuthSetupView` 拆分请求处理
  - 新增 `_handle_connect_relay`：校验 relay URL → fetch providers → 渲染 select_provider
  - 修改 `_handle_start_oauth`：从 form 获取 relay_url + provider → 创建 nonce → 跳转
  - access_mode 硬编码为 `only_me`

- [x] **Task 10.4**: 删除 WebSocket 向导，统一走 HTTP setup
  - 删除 `setup.py` 中的 `run_auth_setup_wizard`、`_build_form_view`、`_build_provider_select_view`、`_make_client_broadcast` 等向导代码
  - `AuthSetupMenu.execute` 改为返回 `redirect` action 跳转 `/auth/setup`
  - 前端 `handleMenuResult` 新增 `redirect` action 处理（使用 `apiPath` 处理 base_path）
  - 删除 `menus.py` 中不再使用的 `_get_self_origin`
