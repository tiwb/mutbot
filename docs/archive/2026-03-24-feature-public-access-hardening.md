# 公网访问安全加固 设计规范

**状态**：✅ 已完成
**日期**：2026-03-24
**类型**：功能设计

## 背景

mutbot 已有完整的 OIDC 认证系统（`src/mutbot/auth/`），但认证是**可选的**——不配置 `auth` 时完全开放。这是为了本地开发便利性的设计（backward compatible）。

实际部署中发现的问题：
1. **无鉴权暴露公网**：`listen: ["0.0.0.0:8741"]` + 无 `auth` 配置 = 任何人可访问
2. **无启动警告**：用户可能不知道自己的服务对外开放且无保护
3. **MCP 端点仅 IP 校验**：`/mcp` 路径只检查 `127.0.0.1`/`::1`，经过反向代理时 client IP 可能不准

## 安全风险分析

mutbot 无鉴权时等同于**完整的远程代码执行（RCE）平台**：

- **Terminal 会话**：通过 WebSocket RPC 创建交互式 Shell，执行任意命令
- **文件读取**：`FileOps.read(path)` 可读取任意路径文件
- **MCP exec_python**：完整的 Python REPL，访问所有内部 API
- **Agent 创建**：创建 AI 代理执行 Web 请求、修改配置

攻击者连接 WebSocket 后数秒即可获得完整 shell 权限。**这不是"可能有风险"，而是确定的 RCE。**

## 设计方案

### 核心思路

**默认安全**：非 loopback 监听 + 无 auth = 服务正常启动，但所有非本地请求被拦截，重定向到 auth 配置引导。不提供跳过选项（`--no-auth`）——后续功能依赖用户身份，无鉴权的公网访问不只是安全问题，也是功能缺失。

### 两种访问模式（自动推断）

**本地模式**（listen 全部为 loopback，或请求来自 loopback）：
- 无需认证，行为与当前完全一致
- auth setup wizard 可通过设置页面主动访问

**网络模式**（listen 包含非 loopback 地址，且请求来自非 loopback）：
- 已配置 auth → 正常 OIDC 登录流程
- 未配置 auth → 拦截所有请求，重定向到 `/auth/setup`

### Setup Token 机制

非 loopback 监听 + 无 auth 时，启动阶段生成一次性 setup token 并打印到控制台：

```
mutbot 已启动: http://0.0.0.0:8741
⚠️  未配置认证，远程访问需输入 setup token 完成配置：

    Setup Token: a3f8c2e1-7b4d-4f9a-b5c6-8e2d1a0f3b7c

```

Token 用途：验证访问者能看到服务器终端 = 拥有本地系统权限。

**Token 生命周期**：auth 配置完成后 token 失效，后续访问走正常 OIDC 流程。

选择 token 而非本地系统密码验证（PAM/LogonUser），因为后者需要 root 权限（Linux）或平台特定依赖（Windows），且无跨平台统一方案。控制台 token 在所有平台上零依赖、免 root。

### Auth Setup Wizard 入口

`/auth/setup` 路由始终存在，是设置页面中的常驻功能，不是一次性页面。

不同场景的访问方式：

| 场景 | 触发方式 | 需要 token |
|------|----------|-----------|
| 远程 + 无 auth（首次配置） | 自动重定向 | 是 |
| 本地（测试/预配置） | 设置页面主动进入 | 否 |
| 远程 + 已登录（修改配置） | 设置页面主动进入 | 否（已认证） |

设置页面中的认证配置：
- 未配置 auth → 显示 setup wizard 引导
- 已配置 auth → 显示当前配置 + 修改选项

### MCP 端点反向代理安全

`/mcp` 当前通过 `scope["client"][0]` 检查来源 IP，经过 nginx 等反向代理时 client IP 变为代理地址，导致校验失效。

**改进**：支持 `X-Forwarded-For` / `X-Real-IP` header，可配置 trusted proxy。

```json
{
  "security": {
    "trusted_proxies": ["127.0.0.1", "10.0.0.0/8"]
  }
}
```

当请求来自 trusted proxy 时，从 `X-Forwarded-For` 取真实 client IP。从右往左扫描 XFF，跳过 trusted IP，第一个非 trusted 的即为 real_ip。无 XFF header 时 fallback 到 direct_ip。

默认值 `["127.0.0.1", "::1"]`（覆盖本地反向代理最常见场景，外部直连无法利用此信任）。

### 实施注意事项

- 拦截逻辑复用现有中间件白名单（`/auth/`、`/.well-known/`、`/api/health`），不另建白名单
- auth wizard 路由在 `/auth/` 前缀下，天然被白名单放行
- setup token 仅在"非 loopback 监听 + 无 auth"时生成，本地模式不生成
- `/auth/setup` 页面采用独立的服务端渲染 HTML（不依赖 React 前端），确保即使前端有问题也能完成 auth 配置。后端核心逻辑（nonce 存储、config 保存、relay OAuth）复用现有 `auth/setup.py` 和 `auth/views.py`。未来 UI 框架完善后可考虑迁移

## 实施步骤清单

- [x] 新增 `is_loopback_only()` 工具函数，判断 listen 地址列表是否全部为 loopback
- [x] 新增 setup token 模块：启动时生成、存储、验证、失效（auth 配置完成后清除）
- [x] 启动流程集成：非 loopback + 无 auth 时生成 token 并打印到控制台
- [x] 修改 `middleware.py`：无 auth + 非 loopback 请求 → 重定向到 `/auth/setup`（复用现有白名单）
- [x] 新增 `/auth/setup` 服务端渲染页面：token 输入 → relay 配置 → OAuth 跳转（复用现有后端逻辑，不依赖前端）
- [x] trusted_proxies 配置支持：新增 `security.trusted_proxies` 配置项，IP 解析逻辑（XFF 从右往左扫描）
- [x] 修改 MCP 端点 IP 检查：使用新的 IP 解析逻辑替代当前直接读 `scope["client"]`
- [x] 设置页面中增加 auth 配置常驻入口（前端）

### Code Review 修复记录（2026-03-31）

- [x] **C1 setup_token 时序攻击**：`setup_token.verify()` 改用 `hmac.compare_digest()` 常量时间比较，防止时序侧信道
- [x] **C2 Supervisor 模式下 token 跨进程失效**：`setup_token.generate()` 同时写入环境变量 `MUTBOT_SETUP_TOKEN`，模块加载时从环境变量继承，`invalidate()` 同时清理环境变量。Worker 子进程通过 `subprocess.Popen` 自动继承父进程环境变量
- [x] **C3 /internal/ 白名单暴露内部端点**：将 `/internal/` 从 `_PUBLIC_PREFIXES` 移到 `_LOCAL_ONLY_PREFIXES`，非本地请求返回 403
- [x] **M1 Token 通过 URL query param 泄露**：移除 GET 中的 token query param 处理，token 验证只走 POST，验证通过后设置短期 httponly cookie（`mutbot_setup_verified=1`，max-age=300），后续请求通过 cookie 判断已验证，configure 表单不再包含 hidden token field
- [x] **M2 relay_url SSRF 风险**：新增 `_validate_relay_url()` 校验，scheme 必须是 https（或 http://localhost 用于开发），拒绝私有 IP 段
- [x] **M3 静态资源白名单过于宽松**：在"无 auth + 非本地"分支中去掉静态资源放行（setup 页面使用内联 CSS 不依赖外部静态资源），静态资源放行只保留在有 auth 配置的分支中

### 手动验证发现的 Bug（2026-03-31）

- [x] **C4 Supervisor token 生成时序错误**：`supervisor._serve()` 中 `_spawn_worker()` 在 `_print_banner()` 之前调用，导致 worker 子进程 spawn 时环境变量中没有 token，middleware 的远程拦截逻辑失效。修复：将 `_print_banner()`（含 token 生成）移到 `_spawn_worker()` 之前。单元测试 `TestSupervisorTokenTiming` 通过源码分析验证调用顺序

### 单元测试（2026-03-31）

新增 `tests/test_public_access_hardening.py`，33 个测试覆盖全部安全模块：

- **TestSetupToken**（10 个）：生成、验证、失效、常量时间比较、环境变量传递、跨进程继承
- **TestNetwork**（8 个）：loopback 判断、XFF 解析、trusted proxy CIDR 匹配、untrusted 忽略 XFF
- **TestMiddleware**（9 个）：本地放行、远程拦截重定向、白名单放行、WebSocket 拒绝、/internal/ 和 /mcp 远程 403
- **TestSSRFValidation**（5 个）：https 允许、localhost http 允许、远程 http 拒绝、私有 IP 拒绝、非法 scheme 拒绝
- **TestSupervisorTokenTiming**（1 个）：源码分析验证 token 生成在 worker spawn 之前

## 遗留问题

以下为第二轮 review 中发现的保留意见，不影响本功能正确性，作为后续改进项：

- Session cookie（`auth/token.py`）缺少 HttpOnly 标志，XSS 场景下 token 可被 JS 读取。如果前端不需要直接读取此 cookie，应加 HttpOnly
- `_validate_relay_url()` 的 DNS rebinding 绕过：攻击者注册域名指向内网 IP 可绕过 SSRF 防护。利用难度高（需用户手动填入恶意 URL），当前防护足够

## Supervisor TCP 代理导致安全拦截失效（2026-03-31）

### 问题现象

所有实施步骤已完成、33 个单元测试通过，但通过外部 IP（如 `http://10.219.26.186:8741`）访问时安全拦截未生效，页面正常打开。

### 根因分析

Supervisor 因 Windows 无法将 socket fd 传给子进程，采用 TCP 代理模式：Supervisor 监听端口，为每个连接创建到 Worker 的本地连接（`asyncio.open_connection("127.0.0.1", worker.port)`），双向 pipe 透传字节流。

这导致 Worker 的 ASGI `scope["client"]` 永远是 `("127.0.0.1", port)`（Supervisor→Worker 本地连接的地址），而非真实客户端 IP。

中间件 `resolve_client_ip()` 的处理链路：
1. `direct_ip = "127.0.0.1"`（来自 Supervisor 的本地连接）
2. `127.0.0.1` 在默认 `trusted_proxies` 中 → 检查 `X-Forwarded-For`
3. 无 XFF header（Supervisor 没有注入）→ fallback 返回 `"127.0.0.1"`
4. `is_loopback_ip("127.0.0.1")` = True → 中间件判定为本地请求，放行

Access log 验证（`mutagent.net.access`）：所有请求来源均为 `127.0.0.1`，状态码 `200`（应为 `302` 重定向到 `/auth/setup`）。

### 设计遗漏

设计中考虑了 nginx 反向代理场景（`trusted_proxies` + XFF 从右往左扫描），但 **Supervisor 本身也是 Worker 前面的一层代理**，它和 nginx 一样需要注入 `X-Forwarded-For`，这一点被遗漏。

Supervisor 在管理端点（`/api/restart`、`/api/eval`）中已经用 `client_writer.get_extra_info("peername")` 获取了真实 IP 并做了检查，说明 Supervisor 层面有真实 IP 信息，只是没有传递给 Worker。

### 单元测试为何未覆盖

33 个测试直接构造 ASGI scope 传入中间件，验证的是"给定正确 client IP，中间件逻辑是否正确"。但真实环境中 Supervisor 介入后 scope 中的 client IP 已被替换为 `127.0.0.1`，端到端 IP 传递链路未被测试覆盖。

### 可观测性缺失

中间件 `_mutbot_before_route()` 无日志输出——不记录 client_ip、决策结果。安全模块的关键决策路径无日志，导致问题排查困难。

### XFF 方案尝试与失败（2026-03-31）

曾实施 XFF 注入方案：Supervisor 在 `_proxy_to_worker()` 中读取所有 HTTP header，去掉已有 XFF，注入 `X-Forwarded-For: <peername>`。首次请求的安全拦截生效，但存在根本性缺陷：

**XFF 是请求级 HTTP header，而 Supervisor 的 pipe 架构是连接级的。** Supervisor 只在 TCP 连接建立时读取 header 并注入 XFF，之后进入 `_pipe()` 裸字节透传。HTTP/1.1 keep-alive 后续请求直接走 pipe，不经过 XFF 注入。

日志证据：
```
14:22:54.684 INFO  — redirect to /auth/setup (no auth): 10.219.26.186 /auth    ← 首请求，XFF 生效
14:22:54.688 DEBUG — allow local (no auth): 127.0.0.1 /auth/setup              ← keep-alive 后续，无 XFF
```

曾尝试注入 `Connection: close` 强制每请求新建连接，但破坏了 WebSocket 升级（需要 `Connection: Upgrade`），已回滚。

**根本问题**：用请求级方案（XFF）解决连接级问题（代理后 client IP 丢失）是方向错误。需要连接级方案。

### 修复方案：PROXY Protocol v1（2026-03-31）

问题本质是**连接级**的：Supervisor 创建到 Worker 的新 TCP 连接时，真实客户端身份信息丢失。需要一个连接级的传递机制。

[PROXY protocol v1](https://www.haproxy.org/download/1.8/doc/proxy-protocol.txt)（HAProxy 发明）正是为此设计：TCP 连接建立后、应用数据之前发送一行文本，后端读一次即可得知真实客户端 IP，适用于该连接上所有后续请求。

```
PROXY TCP4 10.219.26.186 192.168.1.1 56789 8741\r\n
<之后所有数据原样透传>
```

**改动**：

1. **Supervisor**（`_proxy_to_worker()`）：连接 Worker 后，先写一行 PROXY protocol header（用 peername），再转发 first_line + 原始 header + 后续数据。去掉现有的 XFF 读取/注入逻辑，Supervisor 恢复为纯 TCP 透传（不再解析 HTTP header）
2. **Worker 侧**（mutagent `HTTPProtocol`）：`data_received()` 首次调用时检测 `PROXY ` 前缀，解析后覆盖 `self.client`，剩余数据交给 h11。详见 `mutagent/docs/specifications/feature-proxy-protocol.md`

**优势**：
- 连接级传递，keep-alive / WebSocket 均不受影响
- Worker 的 `scope["client"]` 从源头就是正确的，中间件、access log、MCP 检查全部不需要改动
- Supervisor 恢复为 L4 代理（只在连接开头加一行，不解析 HTTP header），比 XFF 方案更简单
- 行业标准格式，未来在 Worker 前加 nginx 也能用

### PROXY Protocol 实施中的 h11 EOF Bug（2026-03-31）

PROXY protocol header 和 HTTP 请求可能分两个 TCP segment 到达。首次 `data_received()` 只收到 PROXY 行时，解析后 `rest = b""`。`h11.receive_data(b"")` 将空 bytes 解释为 EOF（连接关闭信号），导致后续 HTTP 数据到达时抛出 `RuntimeError: received close, then received more data?`。

**修复**：`data_received()` 中 `rest` 为空时直接 return，不调用 `h11.receive_data()`。已在 mutagent 侧补充单元测试 `test_proxy_header_alone_no_h11_eof` 覆盖此边界条件。

## 关键参考

### 源码
- `src/mutbot/auth/network.py` — 网络安全工具（loopback 判断、IP 解析、trusted proxy CIDR 匹配）
- `src/mutbot/auth/setup_token.py` — 一次性 setup token（生成、验证、失效，跨进程环境变量传递）
- `src/mutbot/auth/middleware.py` — 认证拦截逻辑，白名单路径，MCP 本地限制
- `src/mutbot/auth/views.py` — AuthSetupView（服务端渲染 setup 页面）+ SSRF 防护 + cookie 验证
- `src/mutbot/auth/setup.py` — save_auth_config 末尾 invalidate token
- `src/mutbot/web/server.py` — 启动时安全检查和 token 打印
- `src/mutbot/web/supervisor.py` — supervisor 模式安全检查集成
- `tests/test_public_access_hardening.py` — 公网访问安全加固单元测试（33 个）

### 相关规范
- `docs/specifications/feature-openid-auth.md` — OIDC 认证完整设计
- `docs/specifications/feature-auth-setup-wizard.md` — Auth 设置向导
- `docs/design/auth.md` — 认证系统设计概览
- `mutagent/docs/specifications/feature-proxy-protocol.md` — HTTPProtocol PROXY protocol v1 支持（Worker 侧改动）
