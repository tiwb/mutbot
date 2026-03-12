# MCP 运行时内省 设计规范

**状态**：✅ 已完成
**日期**：2026-03-12
**类型**：功能设计

## 背景

mutbot 基于 `mutagent.net` 自研 ASGI Server，MCP 协议支持已内置于 `mutagent.net.mcp`（MCPView + MCPToolSet Declaration 模式）。当前开发调试 mutbot 时，只能通过读日志、CDP 调前端等间接方式了解运行时状态。TODO.md 中已记录此需求（"Claude调试增强"）。

通过在 mutbot 进程内挂载 MCPView 端点，AI 工具（Claude Code 等）可直接内省运行中的服务器状态：session、workspace、连接、配置、日志——这些信息大部分只存在于内存中，日志和磁盘文件只有片段。

## 设计方案

### 核心设计

在 mutbot 中声明 MCPView 子类和 MCPToolSet 子类，利用 `mutagent.net` 已有的 MCP 基础设施提供运行时内省 tools。MCPView 作为 View 子类被 Server.route 统一发现和分发，MCPToolSet 子类的 tool 方法被 MCPToolProvider 自动发现，零注册。

#### MCP endpoint 挂载方式

MCPView 是 View 的子类，被 `Server.route` 统一发现和分发，和现有的 View/WebSocketView 端点共存。只需在 `mutbot/web/mcp.py` 中定义 MCPView 子类，并在 `server.py` 中 import 触发注册即可。

```python
# mutbot/web/mcp.py
from mutagent.net.mcp import MCPView, MCPToolSet
import mutbot

class MutBotMCP(MCPView):
    path = "/mcp"
    name = "mutbot"
    version = mutbot.__version__
    instructions = "MutBot 运行时内省工具，用于查看服务器状态、session、日志等"

class ServerTools(MCPToolSet):
    """服务器状态 tools。"""
    path = "/mcp"

    async def server_status(self) -> dict: ...
    async def list_workspaces(self) -> list: ...
    # ...
```

#### Tools 清单

**服务器状态**

| tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `server_status` | 无 | JSON: uptime, session 数, workspace 数, 连接数, 内存 | 一览全局运行状态 |

**Workspace 内省**

| tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `list_workspaces` | 无 | workspace 列表（id, name, path, session 数, last_accessed） | 列出所有 workspace |

**Session 内省**

| tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `list_sessions` | `workspace_id?` | session 列表（id, type, title, status, model, created, updated） | 列出 session，可按 workspace 过滤 |
| `inspect_session` | `session_id` | session 详情：config, status, runtime 状态, 通道数, token 用量 | 查看单个 session 的完整运行时状态 |
| `get_session_messages` | `session_id`, `last_n?`, `role?`, `full?` | AgentSession 的最近 N 条消息（role, content, tokens） | 查看 agent 对话历史。默认截取前 500 字符；`full=true` 返回完整内容；`role` 按角色过滤（user/assistant/tool） |

**连接内省**

| tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `list_connections` | 无 | 活跃 WebSocket client 列表（id, workspace, state, channels, buffer_size） | 查看所有客户端连接 |

**日志查询**

| tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `query_logs` | `level?`, `logger?`, `pattern?`, `last_n?` | 匹配的日志条目列表 | 查询内存中的日志（通过 LogStore） |
| `get_errors` | `last_n?` | 最近的 ERROR/WARNING 日志 + traceback | 快速排查错误 |

**配置**

| tool | 参数 | 返回 | 说明 |
|------|------|------|------|
| `get_config` | `key?` | 当前配置值（完整或指定 key） | 读取运行时配置 |
| `set_config` | `key`, `value` | 更新结果 | 热更新配置（触发 on_change 回调）。白名单：`logging.console_level`、`default_model` |

#### 模块结构

```
mutbot/web/
├── mcp.py          ← MCPView 子类 + MCPToolSet 子类（新增）
├── server.py       ← import mcp 模块触发注册
├── routes.py       ← 现有 API/WebSocket 路由（不变）
└── ...
```

`mcp.py` import `server.py` 的全局 manager（`workspace_manager`, `session_manager` 等），在 MCPToolSet 子类的 tool 方法中访问。这和 `routes.py` 访问全局 manager 的模式一致。

#### 访问控制

MCP endpoint 默认只在 `127.0.0.1` 可访问（和 mutbot 默认绑定一致）。如果 mutbot 绑定了 `0.0.0.0`，MCP endpoint 应检查来源 IP 或要求认证。初期不额外做认证，依赖网络层限制（本机访问）。

### 实施概要

1. 新增 `mutbot/web/mcp.py`，定义 MCPView 子类 + MCPToolSet 子类
2. 在 `server.py` 中 `import mutbot.web.mcp` 触发 Declaration 子类注册
3. 编写测试验证每个 tool 的返回格式
4. 配置 Claude Code MCP 连接（`.claude/settings.json`）

## 设计决策

### D1: get_session_messages 的消息内容截断策略
默认每条消息截取前 500 字符，提供 `full=true` 参数返回完整内容。增加 `role` 参数按角色过滤（user/assistant/tool）。

### D2: set_config 的范围限制
白名单控制，初期只开放：`logging.console_level`、`default_model`。`providers` 等涉及密钥的配置项不允许通过 MCP 修改。

### D3: send_rpc tool
暂不实现。初期只做只读内省，操作类 tool 后续按需添加。

## 关键参考

### 源码
- `mutbot/src/mutbot/web/server.py:28-32` — 全局 manager 声明
- `mutbot/src/mutbot/runtime/workspace.py` — WorkspaceManager（`_workspaces: dict[str, Workspace]`）
- `mutbot/src/mutbot/runtime/session_manager.py` — SessionManager（`_sessions`, `_runtimes`）
- `mutbot/src/mutbot/session.py` — Session 基类（AgentSession, TerminalSession, DocumentSession）
- `mutbot/src/mutbot/web/transport.py` — ChannelManager（`_channels`, Client state/buffer）
- `mutbot/src/mutbot/runtime/terminal.py` — TerminalManager（`_sessions: dict[str, TerminalProcess]`）
- `mutbot/src/mutbot/runtime/config.py` — MutbotConfig（`get()`, `set()`, `on_change()`）
- `mutagent/src/mutagent/runtime/log_store.py` — LogStore（内存日志存储）
- `mutagent/src/mutagent/net/mcp.py` — MCPView + MCPToolSet Declaration
- `mutagent/src/mutagent/net/_mcp_impl.py` — MCPToolProvider + MCPView 实现

### 相关规范
- `mutagent/docs/specifications/refactor-net-layer.md` — net 层下沉重构（已完成）
- `mutagent/docs/specifications/refactor-net-declarations.md` — net 层 Declaration 分离（已完成）

### 运行时对象关系
```
workspace_manager._workspaces[id] → Workspace
  └── workspace.sessions → [session_id, ...]
       └── session_manager._sessions[id] → Session (Agent/Terminal/Document)
            └── session_manager._runtimes[id] → AgentSessionRuntime
                 └── .agent → mutagent.Agent（LLM, tools, context.messages）

terminal_manager._sessions[term_id] → TerminalProcess
channel_manager._channels[ch] → Channel
  └── _session_channels[session_id] → {ch, ...}

routes._clients[client_id] → Client（WebSocket, SendBuffer）
log_store → LogStore（内存日志环形缓冲）
```
