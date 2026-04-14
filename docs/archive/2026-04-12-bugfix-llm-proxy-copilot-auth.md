# LLM Proxy CopilotProvider 认证未初始化 设计规范

**状态**：✅ 已完成
**日期**：2026-04-12
**类型**：Bug修复

## 需求

1. CopilotProvider 在 proxy 路径下未初始化认证——`_get_backend_info()` 直接调用 `CopilotAuth.get_instance()` 获取单例，但 proxy 路径没走 `from_spec()` 初始化，`github_token` 未设置，导致 `RuntimeError: Not authenticated`
2. proxy 的 provider 分发逻辑基于字符串匹配 + 临时构造 headers，应改为实例化 Provider 并复用

## 关键参考

- `src/mutbot/proxy/routes.py:143-171` — `_get_backend_info()` provider 分发，bug 出处
- `src/mutbot/proxy/routes.py:28` — `_providers_config` 模块级变量，由 server.py 启动时赋值
- `src/mutbot/web/server.py:209-214` — proxy config 初始化和 `on_change` 回调
- `src/mutbot/copilot/auth.py` — CopilotAuth 单例，`github_token` → JWT 换取
- `src/mutbot/copilot/provider.py:44-57` — `CopilotProvider.from_spec()` 初始化 auth
- `src/mutbot/runtime/session_manager.py:289-306` — `create_llm_client()` 中 `from_spec()` 的现有用法
- `docs/specifications/bugfix-llm-proxy-auth-and-stream.md` — 前序修复（`/llm` 白名单 + StreamingResponse 参数）

## 设计方案

### Provider 实例化重构

当前 `_providers_config` 只存原始 config dict，每次请求通过字符串匹配 provider 类型再临时构造 headers。重构为：

1. 启动时遍历 providers config，调用各 Provider 的 `from_spec()` 创建实例
2. 缓存 provider 实例到模块级 `_provider_instances: dict[str, LLMProvider]`
3. 请求时通过 provider 实例获取 headers 和 base_url，不再临时构造
4. config 变更时重新初始化受影响的 provider 实例

这同时解决了 CopilotProvider 认证 bug——`from_spec()` 会调用 `auth.ensure_authenticated()`。

## 实施步骤清单

- [x] `routes.py` 新增 `_provider_instances` 缓存 + 初始化函数
- [x] `routes.py` 重构 `_get_backend_info` 从 provider 实例获取 headers/base_url
- [x] `server.py` 启动时调用初始化 + config 变更时重建实例
- [x] 端到端验证

### 架构决策记录

- **proxy 留在 Worker 内**（评估后决定不分离为独立进程，Supervisor 复杂度翻倍但收益仅为"架构更干净"）
- **独立模式暂不实施**（核心场景是内嵌在主服务中通过 `/llm` 路径访问，独立需求尚未出现）
