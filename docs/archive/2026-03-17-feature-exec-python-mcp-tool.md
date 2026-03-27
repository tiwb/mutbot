# 跨进程 Python 执行 MCP 工具

**状态**：✅ 已完成
**日期**：2026-03-17
**类型**：功能设计

## 背景

调试 mutbot 运行时状态（如 TerminalManager 的 `_client_views`、ptyhost 的 view scroll 状态等）时，现有 MCP 工具只能查询预定义的信息，无法灵活检查内部数据结构。需要一个通用的 `exec_python` MCP 工具，能在指定进程中执行任意 Python 代码并返回结果。

mutbot 有三个进程：

| 进程 | 关键状态 | 已有通信机制 |
|------|---------|-------------|
| Worker | SessionManager、TerminalManager、WebSocket 连接 | MCP endpoint 直接在此进程 |
| Ptyhost | pyte Screen、TermView、scrollback | Worker 通过 PtyHostClient WebSocket（seq-based RPC） |
| Supervisor | Worker 管理、连接代理 | HTTP management API（`/api/restart` 等） |

## 设计方案

### MCP 工具接口

```python
# tool: exec_python
# 参数:
#   code: str       — Python 代码
#   target: str     — "worker" | "ptyhost" | "supervisor"，默认 "worker"
# 返回: str         — 执行结果的文本表示
```

### 执行策略

先尝试 `eval(code)`（表达式求值），如果抛出 `SyntaxError` 则回退到 `exec(code)`（语句执行）。

- **eval 模式**：返回 `repr(result)`
- **exec 模式**：捕获 `sys.stdout` 输出（即 `print()` 的内容）作为返回值

### 各进程实现

**Worker（直接执行）**：
- MCP tool handler 中直接 `eval`/`exec`
- 注入命名空间：`tm`（TerminalManager）、`sm`（SessionManager）、`wm`（WorkspaceManager）、`cm`（ChannelManager）、`config`、`app`（server module）

**Ptyhost（WebSocket RPC 转发）**：
- PtyHostClient 新增 `eval(code)` 方法，发送 `{"cmd": "eval", "code": "..."}`
- ptyhost `_app.py` 新增 `elif action == "eval"` 分支
- 注入命名空间：`manager`（PtyManager）、`terminals`（`manager._terminals`）、`views`（`manager._views`）

**Supervisor（HTTP 转发）**：
- Worker 向 `127.0.0.1:<public_port>/api/eval` 发 HTTP POST
- Supervisor 在 `_handle_management` 中新增 `/api/eval` 分支
- 注入命名空间：`self`（Supervisor 实例）、`active_worker`、`old_workers`

### 安全

- 纯本地开发调试工具，不做沙箱
- Supervisor 的 `/api/eval` 沿用已有的 localhost-only 检查（与 `/api/restart` 一致）

### 错误处理

执行出错时返回 traceback 文本（`traceback.format_exc()`），不抛异常，方便调试。

## 实施概要

三个进程各加一个 eval 入口：Worker 在 MCP tool 中直接执行；Ptyhost 在 WebSocket 命令分发中加 eval 分支；Supervisor 在 HTTP management handler 中加 `/api/eval` 路由。MCP tool 根据 target 参数路由到对应进程。

## 实施步骤清单

- [x] **Task 1**: 提取通用 eval/exec 执行函数
  - [x] `_safe_eval` 在 mcp.py 中实现，ptyhost/supervisor 各自内联实现（避免跨进程 import）
  - 状态：✅ 已完成

- [x] **Task 2**: Worker 进程 — MCP tool `exec_python`
  - [x] `ExecTools` 类，支持 worker/ptyhost/supervisor 三个 target
  - [x] Worker 直接执行，注入 tm/sm/wm/cm/config/srv
  - [x] 已验证：通过 MCP 调用成功返回 `tm._client_views` 等内部状态
  - 状态：✅ 已完成

- [x] **Task 3**: Ptyhost 进程 — WebSocket eval 命令
  - [x] `_client.py` 新增 `eval_code(code)` 方法
  - [x] `_app.py` 命令分发新增 `elif action == "eval"` 分支
  - [x] 已验证：完全重启后 ptyhost eval 正常工作
  - 状态：✅ 已完成

- [x] **Task 4**: Supervisor 进程 — HTTP eval 端点
  - [x] `_handle_management` 新增 `/api/eval` 路由，含 Content-Length 读取
  - [x] localhost-only 安全检查
  - [x] 已验证：完全重启后 supervisor eval 正常工作
  - 状态：✅ 已完成

- [x] **Task 5**: 测试验证
  - [x] Worker target 已验证
  - [x] Ptyhost target 已验证
  - [x] Supervisor target 已验证
  - 状态：✅ 已完成

## 关键参考

### 源码
- `mutbot/src/mutbot/web/mcp.py` — MCP tool 定义
- `mutbot/src/mutbot/web/server.py` — Worker 启动、全局对象
- `mutbot/src/mutbot/ptyhost/_client.py:213` — `_send_command` seq-based RPC
- `mutbot/src/mutbot/ptyhost/_app.py:155-240` — ptyhost 命令分发
- `mutbot/src/mutbot/web/supervisor.py` — Supervisor `_handle_management`
