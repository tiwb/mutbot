# 公网访问安全加固 设计规范

**状态**：📝 设计中
**日期**：2026-03-24
**类型**：功能设计

## 背景

mutbot 已有完整的 OIDC 认证系统（`src/mutbot/auth/`），但认证是**可选的**——不配置 `auth` 时完全开放。这是为了本地开发便利性的设计（backward compatible）。

实际部署中发现的问题：
1. **无鉴权暴露公网**：`listen: ["0.0.0.0:8741"]` + 无 `auth` 配置 = 任何人可访问
2. **无启动警告**：用户可能不知道自己的服务对外开放且无保护
3. **MCP 端点仅 IP 校验**：`/mcp` 路径只检查 `127.0.0.1`/`::1`，经过反向代理时 client IP 可能不准

## 设计方案

### 核心思路

**不改变现有行为的默认值**（不强制开启 auth），而是在危险配置下提供充分的警告和防护建议。分三层：

### 第一层：启动时安全检查与警告

在 `main()` / `supervisor_main()` 启动时检查配置组合，输出醒目警告：

**检测条件**：listen 地址包含非 loopback（`0.0.0.0`、`::`、或具体外网 IP）且无 `auth` 配置。

**警告行为**：
- 日志输出 WARNING 级别警告，包含具体风险说明和修复建议
- 首次连接时在 terminal 输出中显示安全提示（类似 Jupyter 的 token 提示）

```
⚠️  WARNING: 服务监听非本地地址 0.0.0.0:8741，但未配置认证。
    任何能访问此端口的人都可以完全控制服务。
    建议：配置 auth.relay 或 auth.providers 启用认证。
    详见：https://mutbot.ai/docs/auth
```

### 第二层：`--require-auth` 启动参数

新增 CLI 参数 `--require-auth`，启动时校验必须有有效的 auth 配置，否则拒绝启动。

用途：部署脚本 / systemd unit 中强制启用，防止配置回退导致裸奔。

### 第三层：MCP 端点反向代理安全

`/mcp` 当前通过 `scope["client"][0]` 检查来源 IP，经过 nginx 等反向代理时 client IP 变为代理地址，导致校验失效。

**改进**：支持 `X-Forwarded-For` / `X-Real-IP` header，可配置 trusted proxy。

```json
{
  "security": {
    "trusted_proxies": ["127.0.0.1", "10.0.0.0/8"]
  }
}
```

当请求来自 trusted proxy 时，从 `X-Forwarded-For` 取真实 client IP。

## 待定问题

### QUEST Q1: 非 loopback 监听 + 无 auth 是否应阻止启动？
**问题**：检测到危险配置时，是只警告还是直接拒绝启动？
**建议**：默认只警告（不破坏现有用户体验），`--require-auth` 时拒绝。这样本地开发不受影响，生产部署通过 CLI 参数保障。

### QUEST Q2: 是否需要内置简单的 token 认证？
**问题**：当前认证必须配置 OIDC（relay 或 direct），对于不需要多用户的场景（如个人 VPS），配置门槛较高。是否需要类似 Jupyter notebook 的简单 token/password 方案？
**建议**：暂不做。auth relay 已经足够简单（只需一行 `"relay": "https://mutbot.ai"`）。如果用户反馈配置复杂再考虑。

### QUEST Q3: trusted_proxies 的默认值？
**问题**：`security.trusted_proxies` 默认应该是空（不信任任何代理）还是包含 `127.0.0.1`？
**建议**：默认 `["127.0.0.1", "::1"]`，因为本地反向代理（nginx on localhost）是最常见场景。

## 关键参考

### 源码
- `src/mutbot/auth/middleware.py` — 认证拦截逻辑，白名单路径，MCP 本地限制
- `src/mutbot/web/server.py:282-332` — listen 地址解析与 0.0.0.0 展开
- `src/mutbot/web/server.py:400` — CLI `--listen` 参数定义
- `src/mutbot/web/server.py:522-532` — 启动时 listen 地址收集逻辑
- `src/mutbot/runtime/config.py` — MutbotConfig 配置加载

### 相关规范
- `docs/specifications/feature-openid-auth.md` — OIDC 认证完整设计
- `docs/specifications/feature-auth-setup-wizard.md` — Auth 设置向导
- `docs/design/auth.md` — 认证系统设计概览
