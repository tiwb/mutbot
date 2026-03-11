# 自研 ASGI Server — 去除 uvicorn 依赖

**状态**：✅ 已完成
**日期**：2026-03-11
**类型**：功能设计

## 背景

mutbot 当前使用 uvicorn 作为 ASGI server，但在实际使用中遇到多个痛点：

1. **优雅退出不可控** — uvicorn 的 `timeout_graceful_shutdown` 粒度粗，mutbot 需要 chain 它的 SIGINT handler（`_install_double_ctrlc_handler`），逻辑脆弱
2. **不支持多 IP 绑定** — mutbot 已自行实现 socket 绑定绕过 uvicorn
3. **reload 只适合开发** — 文件变更 reload 会杀掉子进程，所有 WebSocket 断开，无 drain/graceful handover
4. **在线更新无法实现** — 生产环境需要新旧 worker 交接、连接迁移，uvicorn 架构不支持
5. **依赖冗余** — uvicorn 带来 `click`（mutbot 不用其 CLI）和自己的日志系统（已用 `log_config=None` 禁用）

当前 uvicorn 在 mutbot 中的实际用途已缩减为：**h11 HTTP 解析 + ASGI 协议桥接**。socket 绑定、日志、信号处理均已自行实现。

## 设计方案

### 核心设计

自研轻量 ASGI server + MCP 协议支持，直接用 asyncio 网络层 + h11，完全替代 uvicorn。

#### 模块定位：`mutbot.server`

`mutbot.server` 是 mutbot 内部的**独立子包**，设计为可复用组件：

- **零内部依赖** — 不 import mutbot 任何其他模块（web、runtime、proxy 等）
- **最小外部依赖** — h11 + wsproto + httpx，均为纯 Python 轻量包
- **ASGI 标准接口** — 接受任何 ASGI app（FastAPI、Starlette、裸 ASGI）
- **内置 MCP 支持** — 同时支持作为 MCP server 和连接其他 MCP server
- **其他项目复用** — `pip install mutbot` 后 `from mutbot.server import Server` 即可，不会拖入 mutagent/fastapi 等

#### 包结构

```
mutbot/server/               ← 纯 ASGI server + 协议支持（~1200 行）
├── __init__.py              ← 导出 Server, MCPServer, MCPClient
├── _http.py        (~300)   ← h11 HTTP/1.1 解析 + ASGI 桥接
├── _ws.py          (~200)   ← WebSocket ASGI 桥接（基于 wsproto）
├── _sse.py          (~20)   ← SSE 响应格式化
├── _jsonrpc.py     (~100)   ← JSON-RPC 2.0 分发器（通用）
├── _mcp_types.py   (~100)   ← 最小 MCP 类型定义
├── _mcp_server.py  (~200)   ← MCP server 框架（tool/resource 注册 + 握手）
└── _mcp_client.py  (~200)   ← MCP client（Streamable HTTP + SSE 解析）

mutbot/web/                  ← 业务层（依赖 mutbot.server）
├── server.py                ← 入口 + supervisor 逻辑（热重载/在线更新）
├── routes.py                ← API + WebSocket 端点
└── ...
```

依赖方向：`mutbot.web → mutbot.server`（单向），`mutbot.server` 不知道 `mutbot.web` 的存在。

#### 外部依赖

| 依赖 | 用途 |
|------|------|
| **h11** | server 侧 HTTP/1.1 解析（收请求） |
| **wsproto** | WebSocket 帧协议解析（纯协议层，和 h11 同作者） |
| **httpx** | MCP client 发请求（含 TLS/SSL、连接池、重试） |

三者均为纯 Python、零依赖的轻量包。

httpx 传递依赖（httpcore → h11、anyio、certifi、idna）均为纯 Python 轻量包，且 anyio 在 Starlette 生态中已是标配。去掉 `uvicorn` 后，`click`、`httptools`、`websockets`、`watchfiles` 等传递依赖自动消失。

其余全部标准库：`asyncio`、`hashlib`/`struct`（WebSocket 帧）、`json`（JSON-RPC）、`socket`、`dataclasses`、`logging`。

#### 使用接口

```python
from mutbot.server import Server

# 最简用法（其他项目）
server = Server(app)
server.run(host="127.0.0.1", port=8000)

# mutbot 用法（自行绑定 socket + 多地址）
server = Server(app)
server.run(sockets=pre_bound_sockets)
```

```python
from mutbot.server import MCPServer

# MCP server — 注册 tools 并挂载到 ASGI app
mcp = MCPServer(name="mutbot", version="0.3.0")

@mcp.tool(description="Search the web")
async def search_web(query: str) -> str:
    ...

server.mount_mcp("/mcp", mcp)
```

```python
from mutbot.server import MCPClient

# MCP client — 连接其他 MCP server
async with MCPClient("http://other-server/mcp") as client:
    tools = await client.list_tools()
    result = await client.call_tool("search", query="hello")
```

### 分层设计

**Layer 1 — ASGI Protocol Server + MCP**（核心，本设计范围）

单进程 asyncio ASGI server，替代 `uvicorn.Server`，同时提供 MCP 协议支持：

ASGI Server：
- `asyncio.start_server()` 接受 TCP 连接
- h11 解析 HTTP/1.1 请求，构造 ASGI `scope`
- 实现 `receive` / `send` callable 桥接 h11 ↔ ASGI app
- WebSocket upgrade：检测 `Upgrade: websocket` 头，切换到 wsproto 帧协议处理
- SSE：HTTP 响应保持连接不关，按 `data: ...\n\n` 格式持续写
- 支持传入已绑定的 socket 列表（`sockets` 参数）

MCP Server：
- JSON-RPC 2.0 分发器（通用，MCP 和现有 WebSocket RPC 都可复用）
- `initialize` / `initialized` 握手
- `tools/list`、`tools/call` — tool 注册与调用
- `resources/list`、`resources/read` — 资源注册与读取
- `prompts/list`、`prompts/get` — prompt 模板
- 传输：Streamable HTTP（POST + SSE 响应，远程 MCP 事实标准）

MCP Client：
- 连接其他 MCP server（Streamable HTTP 传输）
- 使用 httpx 发请求（TLS/SSL、连接池开箱即用）
- 解析 SSE 响应流

**Layer 2 — Supervisor 进程管理**（应用层，不在 `mutbot.server` 内）

在 `mutbot.web.server` 中实现，利用 `mutbot.server.Server` 作为 worker 内核：

- `multiprocessing.get_context("spawn")` 创建子进程（Windows 安全）
- 父进程绑定 socket，通过 spawn 传递给子进程
- 信号处理：父进程捕获 SIGINT/SIGTERM，协调子进程退出
- Windows：`CTRL_C_EVENT` 通知子进程
- 文件监听 / API 触发热重载（`--dev` 模式）

**Layer 3 — 在线更新**（远期目标）

零停机更新，新旧 worker 交接：

- spawn 新 worker，新 worker 就绪后开始接受新连接
- 旧 worker 进入 drain 模式：不接新连接，等待现有请求/WebSocket 完成
- 可配置 drain 超时
- WebSocket 长连接的处理策略（graceful close with reconnect hint / 强制断开）

### 对现有代码的影响

当前 `server.py` 中与 uvicorn 耦合的部分：

| 代码 | 处理 |
|------|------|
| `uvicorn.Config()` / `uvicorn.Server()` (L491-495) | 换成 `mutbot.server.Server` |
| `server.run(sockets=sockets)` (L515) | 接口兼容，传入相同的 sockets |
| `_install_double_ctrlc_handler()` chain uvicorn handler (L73-106) | 不再需要 chain，自己直接处理 SIGINT |
| `timeout_graceful_shutdown=3` (L493) | 自己实现优雅退出逻辑 |
| `server.startup` monkey-patch for banner (L499-512) | 直接在自己的 startup 流程中打 banner |

现有的 socket 绑定、日志初始化、lifespan、路由等代码**完全不受影响**。

### 实施概要

分两步：
1. 先实现 ASGI server 核心（`_http.py` + `_ws.py` + `_sse.py`），直接替换 uvicorn，确保现有功能不受影响
2. 实现 MCP 支持（`_jsonrpc.py` + `_mcp_types.py` + `_mcp_server.py` + `_mcp_client.py`）

Supervisor / 热重载 / 在线更新在 `mutbot.web.server` 应用层按需实现，不在本设计范围内。

## 实施步骤清单

### 阶段一：ASGI Server 核心 [✅ 已完成]

- [x] **Task 1.1**: 创建 `mutbot/server/` 包骨架
  - [x] `__init__.py` 导出 `Server`
  - [x] 确认包结构可独立 import，无 mutbot 内部依赖
  - 状态：✅ 已完成

- [x] **Task 1.2**: 实现 `_http.py` — h11 HTTP/1.1 解析 + ASGI 桥接
  - [x] `asyncio.Protocol` 接受连接（使用 Protocol 而非 start_server，支持流控和协议切换）
  - [x] h11 解析请求，构造 ASGI `scope`（type="http"）
  - [x] 实现 `receive` / `send` callable 桥接 h11 ↔ ASGI app
  - [x] 支持 keep-alive、chunked transfer encoding
  - [x] 支持传入已绑定的 socket 列表
  - 状态：✅ 已完成

- [x] **Task 1.3**: 实现 `_ws.py` — WebSocket ASGI 桥接
  - [x] 基于 wsproto 处理帧编解码
  - [x] HTTP upgrade 握手（通过重建原始 HTTP 请求交给 wsproto）
  - [x] 构造 ASGI WebSocket scope + receive/send
  - 状态：✅ 已完成

- [x] **Task 1.4**: 实现 `_sse.py` — SSE 响应格式化
  - [x] `data: ...\n\n` 格式写入
  - [x] 支持 event、id 字段
  - 状态：✅ 已完成

- [x] **Task 1.5**: 实现 `Server` 类 — 统一入口
  - [x] `Server(app)` 构造，接受 ASGI app
  - [x] `server.run(host=, port=)` 简单模式
  - [x] `server.run(sockets=)` 预绑定 socket 模式
  - [x] ASGI lifespan 协议支持（startup/shutdown）
  - [x] SIGINT 优雅退出（直接处理，双击 Ctrl+C 强制退出）
  - 状态：✅ 已完成

- [x] **Task 1.6**: 替换 `mutbot/web/server.py` 中的 uvicorn 调用
  - [x] `uvicorn.Config` / `uvicorn.Server` → `mutbot.server.Server`
  - [x] 移除 `_install_double_ctrlc_handler` 的 uvicorn chain 逻辑（改为 no-op）
  - [x] 移除 `server.startup` monkey-patch，banner 通过 `on_startup` 回调打印
  - 状态：✅ 已完成

- [x] **Task 1.7**: 更新 `pyproject.toml` 依赖
  - [x] 去掉 `uvicorn[standard]`，添加 `h11>=0.14.0` 和 `wsproto>=1.2.0`
  - 状态：✅ 已完成

- [x] **Task 1.8**: 验证现有功能
  - [x] `python -m mutbot` 启动正常
  - [x] HTTP 路由正常（静态文件 200）
  - [x] WebSocket 连接正常（upgrade 101 + echo）
  - [x] 多 socket 绑定正常
  - [x] Lifespan startup/shutdown 正常
  - 状态：✅ 已完成

- [x] **Task 1.9**: 编写 `mutbot.server` 模块单元测试
  - [x] SSE 格式化（3 tests）
  - [x] HTTP 请求/响应：GET、POST、query string、keep-alive（5 tests）
  - [x] App 异常返回 500（1 test）
  - [x] WebSocket 升级 + text/binary echo（2 tests）
  - [x] Lifespan startup/shutdown + 启动失败（2 tests）
  - [x] Graceful shutdown + 多 socket（2 tests）
  - [x] FlowControl 单元测试（4 tests）
  - 18/18 全部通过
  - 状态：✅ 已完成

### 阶段二：MCP 协议支持 [✅ 已完成]

- [x] **Task 2.1**: 实现 `_jsonrpc.py` — JSON-RPC 2.0 分发器
  - [x] method → handler 路由注册（装饰器 + 编程式）
  - [x] 请求解析、响应构造、错误处理
  - [x] notification（无 id）支持
  - [x] batch 请求支持
  - 状态：✅ 已完成

- [x] **Task 2.2**: 实现 `_mcp_types.py` — 最小类型定义
  - [x] ToolDef、ResourceDef、PromptDef、ToolResult、ServerCapabilities
  - [x] 只定义实际用到的核心类型
  - 状态：✅ 已完成

- [x] **Task 2.3**: 实现 `_mcp_server.py` — MCP server 框架
  - [x] `MCPServer` 类：name/version/capabilities
  - [x] `@mcp.tool()` / `@mcp.resource()` / `@mcp.prompt()` 装饰器
  - [x] `initialize` / `initialized` 握手处理
  - [x] `tools/list`、`tools/call` 分发
  - [x] `resources/list`、`resources/read` 分发
  - [x] `prompts/list`、`prompts/get` 分发
  - [x] `mount_mcp(app, path, mcp)` 挂载到 ASGI app
  - [x] Streamable HTTP 端点（POST JSON-RPC + JSON/SSE 响应）
  - [x] Session 管理（Mcp-Session-Id header + DELETE 终止）
  - 状态：✅ 已完成

- [x] **Task 2.4**: 实现 `_mcp_client.py` — MCP client
  - [x] `MCPClient(url)` 异步上下文管理器
  - [x] `initialize` 握手 + `initialized` notification
  - [x] `list_tools()`、`call_tool()`、`list_resources()`、`read_resource()` 等
  - [x] httpx 发 POST 请求 + 解析 SSE 响应流
  - [x] Session ID 自动管理 + DELETE 终止
  - 状态：✅ 已完成

- [x] **Task 2.5**: MCP 端到端验证 + 测试
  - [x] JSON-RPC 单元测试（12 tests：方法调用、notification、错误、batch、解析）
  - [x] MCP server 注册测试（3 tests：tool、resource、prompt）
  - [x] MCP client ↔ server 端到端（9 tests：init、tools、resources、prompts、ping、session、error）
  - [x] ToolResult 辅助方法（2 tests）
  - 26/26 全部通过
  - 状态：✅ 已完成

## 测试验证

`tests/test_server.py` — 18 个测试全部通过：
- SSE 格式化（3 tests）
- HTTP 请求/响应（5 tests：GET、POST、query string、keep-alive、app 异常 500）
- WebSocket 升级 + echo（2 tests：text + binary）
- Lifespan 协议（2 tests：正常 startup/shutdown + 启动失败）
- Server 管理（2 tests：graceful shutdown + 多 socket）
- FlowControl 单元测试（4 tests）

`tests/test_mcp.py` — 26 个测试全部通过：
- JSON-RPC 2.0 分发器（12 tests：方法调用、notification、错误处理、batch、解析）
- MCP server 注册（3 tests：tool、resource、prompt 装饰器）
- MCP client ↔ server 端到端（9 tests：初始化、tool 调用、resource 读取、prompt 获取、ping、session、错误处理）
- ToolResult 辅助方法（2 tests）

合计 **44 tests，全部通过，3.59 秒完成**。

## 关键参考

### 源码
- `mutbot/src/mutbot/web/server.py` — 当前 server 入口，uvicorn 集成点（L415-518）
- `mutbot/src/mutbot/web/server.py:73-106` — `_install_double_ctrlc_handler` 信号链
- `mutbot/src/mutbot/web/server.py:153-309` — lifespan 管理（startup/shutdown）
- `mutbot/src/mutbot/web/routes.py` — WebSocket 端点，依赖 `fastapi.WebSocket`（底层是 starlette ASGI）
- `mutbot/src/mutbot/web/transport.py` — Client 可靠传输层

### Starlette ASGI 接口
- `starlette/types.py` — `Scope`, `Receive`, `Send`, `ASGIApp` 类型定义
- `starlette/websockets.py:26` — `WebSocket(scope, receive, send)` 构造，只需要 ASGI 三元组

### 外部依赖
- `pyproject.toml:19` — `uvicorn[standard]>=0.20.0` 当前依赖声明（待替换为 h11 + wsproto + httpx）

### MCP 协议
- MCP 规范：Streamable HTTP 传输（POST JSON-RPC + SSE 响应）
- 官方 `mcp` SDK：109 文件 / 76 万字节（过重，不使用）
- MCP 核心交互：`initialize` → `tools/list` → `tools/call`（JSON-RPC 2.0）

### uvicorn 参考实现
- `uvicorn/protocols/http/h11_impl.py` — h11 ASGI 桥接实现
- `uvicorn/protocols/websockets/` — WebSocket ASGI 桥接
- `uvicorn/supervisors/basereload.py` — reload supervisor 架构
