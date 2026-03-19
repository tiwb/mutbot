# 身份验证设计

**日期**：2026-03-09（更新 2026-03-19）

## 概述

MutBot 通过 OpenID Connect (OIDC) 协议实现身份验证。核心原则：

- **零状态**：不在本地存储任何用户凭据，身份完全来自外部 OIDC Provider
- **可选启用**：未配置 `auth` 时全部放行（向后兼容），配置后所有访问都需要登录
- **无状态 Session**：JWT 签发后自包含，集群部署只需共享签名密钥

**当前范围**：认证（身份识别）。权限系统（授权）后续单独设计。

## 两种认证路径

```
路径 1: 中转认证（零注册）          路径 2: 直连认证（自行注册 OAuth App）
┌──────────┐                       ┌──────────┐
│  浏览器   │                       │  浏览器   │
└────┬─────┘                       └────┬─────┘
     │ 1. 跳转中转站                    │ 1. 跳转 Provider
     ▼                                  ▼
┌──────────┐   2. OAuth    ┌────┐  ┌──────────┐   2. OAuth   ┌────┐
│ 中转站    │ ──────────── │ OP │  │ mutbot   │ ──────────── │ OP │
│(mutbot.ai│   3. 断言JWT  │    │  │          │   3. token   │    │
│ 或自建)  │ ◄──────────── │    │  │          │ ◄──────────── │    │
└────┬─────┘               └────┘  └────┬─────┘              └────┘
     │ 4. 断言回传(fragment)            │ 4. 签发 session JWT
     ▼                                  ▼
┌──────────┐                       ┌──────────┐
│ mutbot   │ 5. 验签→session JWT   │  浏览器   │
└──────────┘                       └──────────┘
```

### 路径 1：中转认证

用户无需注册 OAuth App。中转站（mutbot.ai 或自建）持有 OAuth 凭据，完成认证后签发 Ed25519 断言 JWT 回传给 mutbot 实例。

**关键特性**：
- 全程通过浏览器跳转，中转站无需能访问 mutbot 实例（内网实例可用）
- Ed25519 非对称签名：公钥通过 `/.well-known/mutbot-relay.json` 公开，任何实例可验证
- 断言 JWT 5 分钟过期，含 nonce 防重放、audience 防盗用

**中转站协议**（三个端点）：
- `GET /auth/start` — 发起 OAuth
- `GET /auth/relay/callback` — 接收 Provider 回调，签发断言
- `GET /.well-known/mutbot-relay.json` — 元信息（Ed25519 公钥、Provider 列表）

**现有实现**：
1. mutbot.ai — Cloudflare Worker（GitHub OAuth）
2. mutbot 内置 — 任何实例可配置为中转站（支持任意 OIDC Provider）

### 路径 2：直连认证

用户自行在 OIDC Provider 注册 OAuth App，mutbot 实例直接与 Provider 交互。适用于企业内部 OIDC（如 Keycloak、Azure AD 或自建 OpenID Provider）。

**限制**：mutbot 实例需有 Provider 可达的地址（回调 URL 必须可达）。

## Session Token

- **格式**：JWT（HMAC-SHA256 签名）
- **签名密钥**：首次启动自动生成，持久化到 `~/.mutbot/auth_secret`
- **有效期**：默认 7 天（`session_ttl` 可配置）
- **传输**：cookie `mutbot_token`（`Path=/; SameSite=Lax`，HTTPS 时加 `Secure`）
- **WebSocket**：同源场景浏览器自动携带 cookie；跨域场景通过 query param `token=xxx`

**JWT 载荷**：`sub`（`provider:username`）、`name`、`avatar`、`provider`、`exp`

## 请求拦截

通过 mutagent `Server.before_route()` 钩子实现，mutbot 通过 `@impl` 注入认证逻辑。

```
请求 → auth 配置检查 → before_route 钩子 → 路由处理
                          ↓
   无配置 → 直接放行（无身份）
   有配置 + 未认证:
     HTTP → 302 到登录页
     WebSocket → close(4401)
   有配置 + 已认证 → 放行 + scope["user"] 注入身份
```

**白名单路径**（免认证）：`/auth/*`、`/.well-known/`、`/api/health`、`/internal/`、`/mcp`、静态资源

## OIDC Provider 抽象

`OIDCProvider` 基类封装 Authorization Code Flow 三步：

1. `authorize_url()` — 生成授权跳转 URL
2. `exchange_code()` — code 换 access_token
3. `get_userinfo()` — 获取用户信息

### Provider 类型

| 类型 | 说明 | 端点来源 |
|------|------|----------|
| GitHub 预设 | `GitHubProvider` 子类，端点硬编码 | GitHub 不支持 OIDC discovery |
| 手动端点 | 配置中指定三个端点 URL | 适用于非标准或企业内部 OIDC |
| issuer 发现 | 从 `{issuer}/.well-known/openid-configuration` 自动发现 | 标准 OIDC Provider |

### 非标准 OIDC 适配

不同 Provider 的 userinfo 响应字段名可能不同。通过两层机制适配：

1. **`claims` 映射**（优先）：配置中指定字段名映射
2. **Fallback 链**：按常见字段名依次尝试

```
username: claims.username → preferred_username → nickname → sub
name:     claims.name     → name               → fullname
avatar:   claims.avatar   → picture            → avatar_url
```

### Scopes

`scopes` 配置决定向 Provider 请求哪些权限，Provider 根据 scope 决定 userinfo 返回哪些字段。默认 `["openid", "profile"]`，非标准 Provider 需要按其文档配置（如某些企业 OIDC 使用 `nickname`、`fullname`、`email`）。

## 配置结构

```json
{
  "auth": {
    "relay": "https://mutbot.ai",            // 中转站地址（路径 1）
    "providers": {                            // 直连 Provider（路径 2）
      "github": {
        "client_id": "xxx",
        "client_secret": "xxx"
      },
      "corp-sso": {
        "authorization_endpoint": "https://sso.corp.com/authorize",
        "token_endpoint": "https://sso.corp.com/token",
        "userinfo_endpoint": "https://sso.corp.com/userinfo",
        "client_id": "xxx",
        "client_secret": "xxx",
        "scopes": ["openid", "nickname", "fullname", "email"],
        "claims": { "username": "nickname", "name": "fullname" }
      }
    },
    "relay_service": {                        // 中转服务端（让本实例作为中转站）
      "private_key": "-----BEGIN PRIVATE KEY-----\n...",
      "providers": { ... }
    },
    "allowed_users": ["github:tiwb"],         // 白名单（不配则允许所有认证用户）
    "session_ttl": 604800                     // Session 有效期（秒，默认 7 天）
  }
}
```

`relay`（客户端）和 `relay_service`（服务端）是独立的配置节，一个实例可以同时扮演两种角色。

## 前端集成

- **登录页**：React 组件渲染（`LoginPage.tsx`），居中卡片式，暗色主题
- **Provider 列表**：前端请求 `/auth/providers` 获取可用选项（直连 + 中转动态获取）
- **Relay 标注**：中转模式下底部小字显示 `via {relay域名}`
- **WebSocket 4401**：收到 4401 关闭码 → `window.location.reload()` 触发登录
- **用户信息**：侧边栏底部显示头像 + 名字 + 退出按钮

## 后端模块

```
src/mutbot/auth/
  __init__.py
  views.py       — CallbackView, RelayCallbackView, LogoutView, UserinfoView, ProvidersView
  token.py       — JWT 签发与验证（session token + relay 断言验证）
  providers.py   — OIDCProvider 基类 + GitHubProvider 预设 + claims 映射
  relay.py       — 中转服务端（RelayStartView, RelayProviderCallbackView, RelayMetaView）
  middleware.py  — before_route 认证拦截实现
```

所有认证路由作为 `View` 子类，通过 mutobj `discover_subclasses(View)` 零注册发现。

## 未来方向

- **mutbot.ai 跨域认证**：从官网连接远程服务器时的认证流程
- **权限系统**：用户权限等级（可操作 / 只可观测等）
- **待审批机制**：未在白名单中的用户进入待审批状态
