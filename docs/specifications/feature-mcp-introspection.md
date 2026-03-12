# MCP 运行时内省 设计规范

**状态**：📝 设计中
**日期**：2026-03-11
**类型**：功能设计

## 背景

mutbot 已自研 ASGI Server 并内置 MCP 协议支持（`mutbot.server.MCPServer`）。当前开发调试 mutbot 时，只能通过读日志、CDP 调前端等间接方式了解运行时状态。TODO.md 中已记录此需求（"Claude调试增强"）。

通过在 mutbot 进程内挂载 MCP endpoint，AI 工具（Claude Code 等）可直接内省运行中的服务器状态：session、workspace、连接、配置、日志——这些信息大部分只存在于内存中，日志和磁盘文件只有片段。

## 设计方案

### 核心设计

在 mutbot 进程内启动 MCPServer，注册一组运行时内省 tools，通过 `/mcp` HTTP 端点对外暴露。

#### MCP endpoint 挂载方式

当前 `mount_mcp` 是 ASGI app 包装方式，但 mutbot 用 FastAPI 路由系统。为避免和 FastAPI 路由冲突，采用 **FastAPI 路由委托**方式：

```python
# mutbot/web/mcp.py
from fastapi import Request, Response
from mutbot.server import MCPServer

mcp = MCPServer(name="mutbot", version=mutbot.__version__)

@router.api_route("/mcp", methods=["GET", "POST", "DELETE"])
async def mcp_endpoint(request: Request):
    # 将 FastAPI request 转为 ASGI scope/receive/send 调用 mcp.handle_request
    ...
```

这样 MCP 端点和现有 API/WebSocket 路由共存，无需改动 ASGI app 层。

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
| `get_session_messages` | `session_id`, `last_n?` | AgentSession 的最近 N 条消息（role, content 摘要, tokens） | 查看 agent 对话历史 |

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
| `set_config` | `key`, `value` | 更新结果 | 热更新配置（触发 on_change 回调） |

#### 模块结构

```
mutbot/web/
├── mcp.py          ← MCP endpoint + tool 注册（新增，~200 行）
├── server.py       ← lifespan 中初始化 mcp
├── routes.py       ← 现有 API/WebSocket 路由（不变）
└── ...
```

`mcp.py` import `server.py` 的全局 manager（`workspace_manager`, `session_manager` 等），注册 tool handler。这和 `routes.py` 访问全局 manager 的模式一致。

#### 访问控制

MCP endpoint 默认只在 `127.0.0.1` 可访问（和 mutbot 默认绑定一致）。如果 mutbot 绑定了 `0.0.0.0`，MCP endpoint 应检查来源 IP 或要求认证。初期不额外做认证，依赖网络层限制（本机访问）。

### 实施概要

1. 新增 `mutbot/web/mcp.py`，创建 MCPServer 实例并注册所有 tool
2. 在 `server.py` 中注册 MCP 路由到 FastAPI app
3. 编写测试验证每个 tool 的返回格式
4. 配置 Claude Code MCP 连接（`.claude/settings.json`）

## 待定问题

### QUEST Q1: get_session_messages 的消息内容截断策略
**问题**：AgentSession 的消息可能很长（包含代码、工具调用结果等）。`get_session_messages` 返回完整内容还是截断摘要？
**建议**：默认返回摘要（每条消息截取前 200 字符），提供 `full=true` 参数返回完整内容。

### QUEST Q2: set_config 的范围限制
**问题**：`set_config` 允许修改所有配置项，还是只开放安全的子集？
**建议**：初期只开放 `logging.console_level`、`default_model` 等安全项。`providers` 等涉及密钥的不允许通过 MCP 修改。用白名单控制。

### QUEST Q3: 是否需要 send_rpc tool
**问题**：是否需要一个 `send_rpc` tool 让 AI 模拟前端发送 WebSocket RPC 消息？这对测试很有用但可能有安全风险。
**建议**：暂不加入初期实施范围。运行时内省（只读）先上线，操作类 tool 后续按需添加。

## 关键参考

### 源码
- `mutbot/src/mutbot/web/server.py:31-35` — 全局 manager 声明
- `mutbot/src/mutbot/runtime/workspace.py` — WorkspaceManager（`_workspaces: dict[str, Workspace]`）
- `mutbot/src/mutbot/runtime/session_manager.py` — SessionManager（`_sessions`, `_runtimes`）
- `mutbot/src/mutbot/session.py` — Session 基类（AgentSession, TerminalSession, DocumentSession）
- `mutbot/src/mutbot/web/transport.py` — ChannelManager（`_channels`, Client state/buffer）
- `mutbot/src/mutbot/runtime/terminal.py` — TerminalManager（`_sessions: dict[str, TerminalProcess]`）
- `mutbot/src/mutbot/runtime/config.py` — MutbotConfig（`get()`, `set()`, `on_change()`）
- `mutagent/src/mutagent/runtime/log_store.py` — LogStore（内存日志存储）

### 相关规范
- `mutbot/docs/specifications/feature-asgi-server.md` — ASGI Server + MCP 协议实现（已完成）

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
