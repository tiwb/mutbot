# LLM Proxy 独立服务化设计

**状态**：📝 设计中
**日期**：2026-04-12
**类型**：功能设计

## 需求

1. LLM proxy 当前作为 mutbot worker 内嵌模块运行，与 session/workspace 等业务逻辑耦合在同一进程
2. CopilotProvider 在 proxy 路径下未初始化认证（已知 bug，详见下方）
3. 希望 LLM proxy 可作为独立轻量服务运行，仅提供模型代理能力，不加载 session/workspace 等业务模块

## 关键参考

- `src/mutbot/proxy/routes.py` — LLM proxy 路由和代理逻辑
- `src/mutbot/proxy/routes.py:143` — `_get_backend_info()` provider 分发，CopilotAuth 未初始化的 bug 出处
- `src/mutbot/proxy/routes.py:28` — `_providers_config` 模块级变量，由 server.py 启动时赋值
- `src/mutbot/web/server.py:209-214` — proxy config 初始化和 `on_change` 回调
- `src/mutbot/web/server.py:445-497` — `worker_main()` Worker 启动流程
- `src/mutbot/web/supervisor.py` — Supervisor TCP 代理 + Worker 管理
- `src/mutbot/copilot/auth.py` — CopilotAuth 单例，`github_token` → JWT 换取
- `src/mutbot/copilot/provider.py:44-57` — `CopilotProvider.from_spec()` 初始化 auth
- `docs/specifications/bugfix-llm-proxy-auth-and-stream.md` — 前序 bug 修复（`/llm` 白名单 + StreamingResponse 参数）

## 设计方案

### 已知 Bug：CopilotProvider proxy 路径认证未初始化

proxy 的 `_get_backend_info()` 通过字符串匹配检测 provider 类型后直接调用 `CopilotAuth.get_headers()`，但 proxy 路径没走 `CopilotProvider.from_spec()` 初始化流程，`CopilotAuth` 单例的 `github_token` 未设置。

**现象**：请求 copilot 模型时报 `RuntimeError: Not authenticated`。

**修复**：proxy 初始化 `_providers_config` 时，对每个 provider config 调用对应 Provider 类的 `from_spec()` 完成认证初始化。将实例化后的 provider 缓存供后续请求使用，替代当前每次请求临时构建 headers 的方式。

### 独立服务化

当前 proxy 嵌入在 mutbot worker 中，依赖 `server.py` 的 `_on_startup` 初始化。目标是让 proxy 可以独立运行：

- **独立入口**：`python -m mutbot proxy` 或 `mutbot proxy`，仅启动 LLM proxy 服务，不加载 session/workspace/terminal 等模块
- **配置复用**：读取同一份 `~/.mutbot/config.json` 的 `providers` 配置
- **端口独立**：可配置独立监听端口（默认 8742），与主服务并行运行
- **Supervisor 集成**：可选通过 supervisor 管理，支持热重启

### 配置方案

```json
{
  "proxy": {
    "enabled": true,
    "listen": "127.0.0.1:8742"
  },
  "providers": {
    "copilot": { ... },
    "volcengine": { ... }
  }
}
```

- `proxy.enabled`：在主服务中是否启用内嵌 proxy（默认 true，当前行为）
- `proxy.listen`：独立模式的监听地址
- `providers`：provider 配置保持不变，两种模式共享

### Provider 实例化重构

当前 `_providers_config` 只存原始 config dict，每次请求通过字符串匹配 provider 类型再临时构造 headers。重构为：

1. 启动时遍历 providers config，调用各 Provider 的 `from_spec()` 创建实例
2. 缓存 provider 实例到模块级 `_provider_instances: dict[str, LLMProvider]`
3. 请求时通过 provider 实例获取 headers 和 base_url，不再临时构造
4. config 变更时重新初始化受影响的 provider 实例

这同时解决了 CopilotProvider 认证 bug——`from_spec()` 会调用 `auth.ensure_authenticated()`。

## 待定问题

### QUEST Q1: 独立模式是否需要 Supervisor
**问题**：独立 proxy 服务是否需要 supervisor 管理？还是简单的单进程模式就够了？
**建议**：先支持单进程模式（`mutbot proxy`），后续按需加 supervisor。proxy 是无状态的，重启代价小。

### QUEST Q2: 内嵌模式与独立模式的关系
**问题**：两种模式是否可以同时运行？还是互斥的？
**建议**：可以同时运行。内嵌模式挂在主服务的 `/llm` 路径下，独立模式监听独立端口。Claude Code 配置指向哪个都行。

### QUEST Q3: 独立模式的认证
**问题**：独立模式是否需要认证？还是只监听 localhost 就够了？
**建议**：默认只监听 localhost，与当前 `_LOCAL_ONLY_PREFIXES` 策略一致。如果需要远程访问再考虑 API key 认证。
