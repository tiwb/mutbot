# HTTP 客户端增强 设计规范

**状态**：✅ 已完成
**日期**：2026-03-10
**类型**：功能设计

## 背景

mutbot 和 mutagent 的 HTTP 客户端存在以下改进需求：

1. **启动日志缺少本地地址** — 当前只打印 `Open https://mutbot.ai to get started`，缺少实际监听地址
2. **支持多地址监听** — 通过 config 和 CLI 配置多个 listen 地址，支持公网/局域网访问
3. **启动 banner 重复打印** — `_startup_with_banner` 在 Ctrl+C 退出时会再打印一遍
4. **不支持 SOCKS5 代理** — httpx 需要安装 `httpx[socks]`
5. **HTTP 请求缺少工具标识** — mutbot 应在请求头中标识为 `MutBot.ai`
6. **mutbot.ai 不支持自定义后端地址** — 需要 `/connect/#address` 路径让前端连接非默认后端

## 设计方案

### 多地址监听

CLI 参数 `--host`/`--port` 替换为 `--listen`，config 中同名键 `listen`，两者叠加去重。

**CLI**：

```bash
mutbot --listen 0.0.0.0:8742 --listen 127.0.0.1:8741
```

`--listen` 可多次指定。解析规则：
- `0.0.0.0:8742` → 含 `:` 视为完整 host:port
- `8742` → 纯数字视为端口 → `127.0.0.1:8742`
- `0.0.0.0` / `localhost` → 非纯数字且无 `:` 视为 host → 补默认端口 8741

**config.json**：

```jsonc
{
  "listen": ["0.0.0.0:8742"]
}
```

字符串数组，格式与 `--listen` 一致。

**合并逻辑**：

```
最终绑定列表 = CLI --listen 列表 + config listen 列表（按 host:port 去重）
如果两者都为空 → ["127.0.0.1:8741"]
```

用户指定了任何 `--listen` 或 config `listen`，就不再添加默认地址。

**实现方式**：手动创建 sockets（`socket.bind()`），通过 uvicorn 的 `sockets` 参数传入单个 Server。以后加 HTTPS 时可按 listen 条目单独配 TLS，届时切换到多 Server 方案。

### `0.0.0.0` 展开

`0.0.0.0` 是"绑定所有网卡"，不是可访问地址。处理方式：

- **绑定时**：照常用 `0.0.0.0` 传给 `socket.bind()`
- **Banner 显示时**：枚举本机所有网卡 IP（含 `127.0.0.1`），展开为多行
- **去重**：如果其他 listen 条目已覆盖某个 IP:port，展开时跳过该组合

枚举使用 `socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)` 获取本机所有 IPv4 地址，无需额外依赖。

### 启动 Banner

每个监听地址一行，右侧括号内标注对应的 mutbot.ai 入口：

```
  MutBot v0.5.999

  ➜ http://127.0.0.1:8741     (via https://mutbot.ai)
  ➜ http://192.168.1.100:8742 (via https://mutbot.ai/connect/#192.168.1.100:8742)
  ➜ http://10.0.0.1:8742      (via https://mutbot.ai/connect/#10.0.0.1:8742)
```

- 仅 `127.0.0.1:8741` 这一精确组合 via 指向官网首页（mutbot.ai 默认连接该地址）
- 所有其他地址（含 `127.0.0.1` 非 8741 端口）via 指向 `/connect/#{host}:{port}`
- 版本号从 `mutbot.__version__` 获取
- 用 `_banner_printed` 标志位防止 Ctrl+C 时重复打印

### mutbot.ai `/connect/` 路由支持

**路径格式**：`https://mutbot.ai/connect/#192.168.1.100:8742`

使用 hash 形式传递地址，GitHub Pages 正常响应 `/connect/index.html`，无 404 问题。

**实现方式**：

通过 Astro 生成 `connect/index.html` 页面（或直接在 `public/connect/` 下放静态 HTML）。页面逻辑：
1. 从 `location.hash` 提取后端地址（如 `192.168.1.100:8742`）
2. hash 为空或格式非法 → 直接跳转首页
3. 读取 localStorage 服务器列表，按 URL 去重：
   - **已存在** → 直接 `location.replace("/")` 跳转首页
   - **不存在** → 显示简单表单，让用户输入服务器名称（label），预填地址作为默认值。用户确认后构造 ServerEntry 追加到列表，再跳转首页

launcher.ts 无需改动——跳转回首页后 launcher 正常启动，从 localStorage 读取服务器列表，自动发现新增的服务器并连接。

### SOCKS5 代理支持

mutagent 的 `pyproject.toml`：`httpx>=0.27` → `httpx[socks]>=0.27`（引入 `socksio`）。mutbot 无需额外修改。

代理通过环境变量配置（`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`），支持 `socks5://` 协议，无需代码中显式传递（决策理由：环境变量是标准方式，最简单，后续有需求再加配置文件支持）。

### HTTP 请求工具标识

在 `mutagent/src/mutagent/http.py`（新文件）中声明 `HttpClient(Declaration)`，提供 `create(**kwargs) -> httpx.AsyncClient` 桩方法及默认 `@impl`。各调用点统一用 `HttpClient.create(...)` 替代直接 `httpx.AsyncClient(...)`（决策理由：集中管理，未来新增全局 HTTP 配置只需改一处）。

**Declaration + @impl 覆盖**：
- mutagent 内置 `@impl`：默认 User-Agent 为 `mutagent/{version}`
- mutbot 通过 `@impl` 覆盖：User-Agent 改为 `MutBot.ai/{version}`
- 无需 Config 配置项，符合 mutobj 的声明-实现分离模式

**影响范围**（mutagent 中改用 `HttpClient.create()` 的调用点）：
- `anthropic_provider.py` — LLM API 请求
- `openai_provider.py` — LLM API 请求
- `web_jina.py` — Jina 搜索/读取
- `web_local.py` — 本地 Web 获取
- `web_toolkit_impl.py` — 内置 Web 获取

mutbot 中的调用点：
- `proxy/routes.py` — LLM 代理转发（使用 `HttpClient.create()`）
- `copilot/auth.py` — Copilot 认证（保持伪装为 `GitHubCopilotChat`，不使用通用 User-Agent，这是 Copilot API 认证所需）

## 关键参考

### 源码

**mutbot**：
- `mutbot/src/mutbot/web/server.py:339-419` — main 函数完整流程
- `mutbot/src/mutbot/web/server.py:409-413` — 启动 banner（有重复打印 bug）
- `mutbot/src/mutbot/web/server.py:350-351` — host/port 命令行参数（将被替换为 --listen）
- `mutbot/src/mutbot/runtime/config.py:157-177` — config.json 加载（MutbotConfig）
- `mutbot/src/mutbot/proxy/routes.py:246,280-283` — LLM 代理转发
- `mutbot/src/mutbot/copilot/auth.py:83-98` — Copilot 认证 headers

**mutagent**：
- `mutagent/pyproject.toml:26-29` — httpx 依赖声明
- `mutagent/src/mutagent/builtins/anthropic_provider.py:67-71,305-310` — Anthropic HTTP 请求
- `mutagent/src/mutagent/builtins/openai_provider.py:70-73,262-267` — OpenAI HTTP 请求
- `mutagent/src/mutagent/builtins/web_jina.py:33-39,66-67` — Jina HTTP 请求
- `mutagent/src/mutagent/builtins/web_local.py:38-40` — 本地 Web 获取
- `mutagent/src/mutagent/builtins/web_toolkit_impl.py:40-41` — 内置 Web 获取

**mutbot.ai**：
- `mutbot.ai/src/scripts/launcher.ts:50-60` — 默认服务器配置（`localhost:8741`）
- `mutbot.ai/src/scripts/launcher.ts:148-153` — `connectServer()` WebSocket 连接
- `mutbot.ai/src/scripts/launcher.ts:109-130` — hash 路由解析
- `mutbot.ai/src/scripts/launcher.ts:290-318` — `loadReactForVersion()` 动态加载 React SPA

### 相关规范
- `mutbot.ai/docs/specifications/feature-remote-server.md` — 多服务器管理
- `mutbot.ai/docs/specifications/bugfix-workspace-hash-routing.md` — hash 路由规范

## 实施步骤清单

### 阶段 1：mutagent — HttpClient Declaration + SOCKS5 [✅ 已完成]

- [x] **Task 1.1**: 创建 `mutagent/src/mutagent/http.py`，声明 `HttpClient(Declaration)` 及默认 `@impl`
  - 状态：✅ 已完成

- [x] **Task 1.2**: 修改 `mutagent/pyproject.toml`，`httpx>=0.27` → `httpx[socks]>=0.27`
  - 状态：✅ 已完成

- [x] **Task 1.3**: 将 mutagent 中 5 个 httpx 调用点改用 `HttpClient.create()`
  - 状态：✅ 已完成

- [x] **Task 1.4**: mutagent 测试验证
  - 669 passed, 1 failed（已有断言问题，非本次改动）
  - 状态：✅ 已完成

### 阶段 2：mutbot — 多地址监听 + Banner + HttpClient 覆盖 [✅ 已完成]

- [x] **Task 2.1**: mutbot 提供 `@impl` 覆盖 `HttpClient.create`，User-Agent 改为 `MutBot.ai/{version}`
  - 新文件 `mutbot/src/mutbot/builtins/http_client.py`
  - 状态：✅ 已完成

- [x] **Task 2.2**: mutbot `proxy/routes.py` 改用 `HttpClient.create()`
  - 状态：✅ 已完成

- [x] **Task 2.3**: 实现 `--listen` 参数解析与 config `listen` 合并逻辑
  - 状态：✅ 已完成

- [x] **Task 2.4**: 实现多 socket 绑定
  - 状态：✅ 已完成

- [x] **Task 2.5**: 实现启动 Banner
  - 状态：✅ 已完成

- [x] **Task 2.6**: mutbot 启动测试验证
  - 配置系统测试 18 passed，listen 解析和 banner 生成验证通过
  - 状态：✅ 已完成

### 阶段 3：mutbot.ai — connect 页面 [✅ 已完成]

- [x] **Task 3.1**: 创建 connect 页面（`src/pages/connect.astro`）
  - Astro 页面，构建生成 `/connect/index.html`
  - 逻辑：解析 hash → 去重检查 localStorage → 已存在则直接跳转 / 不存在则表单输入 label → 写入 → 跳转首页
  - 状态：✅ 已完成

- [x] **Task 3.2**: connect 页面构建验证
  - Astro build 成功，正确生成 `/connect/index.html`
  - 状态：✅ 已完成

## 风险评估

### 高风险

**多 socket 绑定兼容性**（Task 2.4）
- uvicorn 的 `sockets` 参数是底层 API，文档较少，Windows 上行为可能与 Linux 不同
- 缓解：实施时先写最小 POC 验证 uvicorn sockets 在 Windows 上是否正常工作

### 中风险

**mutagent HttpClient 改造范围大**（Task 1.3）
- 涉及 5 个文件，各文件的 httpx 使用方式不完全一致（有的传 timeout 到构造函数，有的传到单次请求）
- 缓解：`HttpClient.create()` 透传 kwargs，不改变调用方原有的 timeout 语义

**`0.0.0.0` 网卡枚举**（Task 2.5）
- `socket.getaddrinfo(socket.gethostname(), ...)` 在某些环境下可能返回不完整的 IP 列表（如 WSL、Docker 容器）
- 缓解：枚举失败时 fallback 显示 `0.0.0.0:{port}`，不阻塞启动

### 低风险

**SOCKS5 依赖**（Task 1.2）— 仅改 extra，无代码变更
**connect 页面**（Task 3.1）— 独立静态页，不影响现有功能
**Banner 重复打印修复**（Task 2.5）— 加标志位，改动极小
