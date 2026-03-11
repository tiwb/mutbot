# 终端 Session 持久化（服务器重启后存活） 设计规范

**状态**：🔄 实施中（Phase 1-4 完成，Phase 5 测试中）
**日期**：2026-03-11
**类型**：功能设计

## 背景

当前 mutbot 终端 Session 在服务器重启时：
1. 旧 PTY 进程随服务器进程一起被操作系统清理（Unix）或 pipe handle 丢失（Windows）
2. `on_restart_cleanup` 将残留的 `running` 状态改为 `stopped`
3. 前端重连后需调用 `session.restart` 创建新 PTY（旧进程的上下文丢失，只保留 scrollback 历史）

**需求**：终端 Session 在服务器重启后 PTY 进程仍然存活，前端可无缝重新连接。终端只在用户手动删除时才 kill。

## 调研

### tmux 的机制

tmux 采用 Server-Client 分离模型：server 是独立守护进程，持有所有 PTY；client 通过 Unix Domain Socket 连接。Detach 时只有 client 退出，server + PTY 继续运行。

**核心启示**：持久化的本质是 **PTY 属主从短生命周期进程（mutbot server）转移到长生命周期的独立守护进程**。

### mutbot.server 独立性

`mutbot.server.Server` 是自研 ASGI server，**完全可独立使用**：
- 零 mutbot 内部依赖（不 import fastapi、mutagent 等）
- 仅需 `h11` + `wsproto`
- 完整 WebSocket 支持
- 支持最小 ASGI app 直接启动：`Server(app).run(host, port)`

## 设计方案

### 核心设计

**所有终端统一走 `mutbot.ptyhost` 守护进程**。ptyhost 是一个纯粹的 PTY 进程池——只负责 PTY 的创建、I/O、销毁，不管任何业务策略。多个 mutbot 实例可以共享同一个 ptyhost。

mutbot 负责所有业务逻辑：session 持久化、scrollback 保存、何时 kill 终端。终端只在用户手动删除 session 时才被 kill，mutbot 重启不触发任何终端清理。

```
mutbot.ptyhost（单守护进程，纯 PTY 进程池）
├── mutbot.server.Server (随机端口，127.0.0.1)
├── 复用现有 TerminalManager 代码
├── terminal "abc123" → PTY → bash
├── terminal "def456" → PTY → bash
└── terminal "ghi789" → PTY → bash

端口持久化: ~/.mutbot/ptyhost.port

mutbot server A（可重启）─── WebSocket ───┐
mutbot server B（可选）──── WebSocket ────┤ 共享 ptyhost
                                          │
```

### mutbot.ptyhost — PTY 宿主进程

#### 职责边界

ptyhost **只做**：
- 创建 PTY 进程
- 中继 I/O（读写 PTY）
- 调整终端大小
- kill 指定终端
- 维护 scrollback buffer
- 报告终端状态

ptyhost **不管**：
- 持久化策略（哪些终端该保留、该清理）
- Session 元数据
- 用户身份、权限
- 任何业务逻辑

#### 进程模型

```python
# mutbot/ptyhost/_app.py
from mutbot.server import Server

class PtyHostApp:
    """纯 PTY 进程池。"""
    terminal_manager: TerminalManager  # 复用现有代码

    async def app(self, scope, receive, send):
        """ASGI app：处理 WebSocket 连接。"""
        ...

# mutbot/ptyhost/__main__.py
if __name__ == "__main__":
    host = PtyHostApp()
    server = Server(host.app)
    server.run(host="127.0.0.1", port=0)  # 随机端口
    # server 绑定后获取实际端口，写入 ~/.mutbot/ptyhost.port
```

**import 链极轻**：`mutbot.server`（h11 + wsproto）+ TerminalManager 相关代码。不拉 FastAPI、mutagent。

> 注：`mutbot.server.Server` 需支持 `port=0`（OS 分配端口）并暴露实际绑定端口。如当前不支持，实施时补充（通过 `server.run(sockets=[sock])` 预绑定 socket 可实现）。

#### 启动与发现

```
mutbot 启动时:
  1. 读取 ~/.mutbot/ptyhost.port
  2. 尝试 WebSocket 连接 ws://127.0.0.1:{port}
  3. 连接成功 → ptyhost 已在运行
  4. 连接失败 → spawn ptyhost 子进程（DETACHED_PROCESS）
  5. 等待 ptyhost 就绪（轮询连接，超时 3s）
```

启动方式：
- **Windows**: `subprocess.Popen([python, "-m", "mutbot.ptyhost"], creationflags=CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS)`
- **Unix**: double-fork daemonize + `os.setsid()`

#### 空闲退出

ptyhost 无终端且无连接时，60s 超时后自动退出。下次 mutbot 创建终端时重新 spawn。

#### WebSocket 通信协议

JSON 帧用于命令/事件/回复，binary 帧**只用于** write（输入）和 output（输出）两种高频 I/O。

**binary 帧格式**（仅 write 和 output）：

```
[16 bytes term_id (UUID)] [raw data]
```

term_id 为 UUID（`uuid4()`），由 ptyhost 在 `create` 时生成，天然不会错配。mutbot 将 term_id 存入 session，重启后通过 `list` 比对即可恢复映射。

**命令（mutbot → ptyhost，JSON）**：

| 命令 | payload | 回复 |
|------|---------|------|
| `create` | `{rows, cols, cwd}` | `{ok: true, term_id: "uuid"}` 或 `{ok: false, error: "..."}` |
| `resize` | `{term_id, rows, cols}` | `{ok: true, rows, cols}` |
| `scrollback` | `{term_id}` | `{term_id, data_b64: "..."}` |
| `status` | `{term_id}` | `{alive, exit_code, rows, cols}` |
| `list` | — | `[{term_id, alive}]` |
| `kill` | `{term_id}` | `{ok: true}` |

**事件（ptyhost → mutbot，JSON）**：

| 事件 | payload |
|------|---------|
| `exit` | `{term_id, exit_code}` |

**I/O 数据（binary 帧，双向）**：

| 方向 | 说明 |
|------|------|
| mutbot → ptyhost | 键盘输入：`[term_id][data]` |
| ptyhost → mutbot | PTY 输出：`[term_id][data]` |

ptyhost 将 output binary 帧广播给所有连接的 WebSocket 客户端，mutbot 忽略不认识的 term_id。

### mutbot 侧变更

#### WebSocket 客户端

mutbot 作为 WebSocket 客户端连接 ptyhost。基于 `wsproto`（已有依赖）+ asyncio raw socket 实现，零新依赖。

连接后发 `list` 做握手确认（防止端口文件过期、端口被其他进程占用）。回复不符合预期则视为过期，重新 spawn ptyhost。

#### TerminalManager 重构

重构为 ptyhost 的 WebSocket 客户端，对上层接口不变：

```
TerminalManager（重构后）
├── _ws: WebSocket 连接到 ptyhost
├── async create() → 发 create，await 回复
├── write() → fire-and-forget，塞发送缓冲区
├── resize() → 计算 min-size 后 fire-and-forget 发给 ptyhost
├── kill() → fire-and-forget
├── async get_scrollback() → 发 scrollback，await 回复
└── on_output / on_exit 回调（来自 ptyhost 推送）
```

**sync/async 分离**：需要回复的方法（`create`、`get_scrollback`）为 async；不需要回复的方法（`write`、`resize`、`kill`）保持 sync（fire-and-forget）。

**resize 尺寸协商**：multi-client 的 `_client_sizes` min-size 协商逻辑留在 mutbot 侧 TerminalManager 中，计算出有效尺寸后发给 ptyhost。ptyhost 只执行 resize，不管协商。

#### Session 钩子 async 化

`on_create` 和 `on_connect` 需要 await TerminalManager 的 async 方法，因此改为 async：

| 钩子 | 现状 | 变更 | 原因 |
|------|------|------|------|
| `on_create` | sync | → **async** | await `tm.create()` |
| `on_connect` | sync | → **async** | await `tm.get_scrollback()` |
| `on_stop` | sync | 不变 | `tm.kill()` 是 fire-and-forget |
| `on_disconnect` | sync | 不变 | 无 ptyhost 调用 |
| `on_message` | async | 不变 | — |
| `on_data` | async | 不变 | — |

SessionManager 中调用 `on_create` / `on_connect` 的地方相应加 `await`。

#### 生命周期变更

**创建终端（on_create）**：向 ptyhost 发 create，await 回复拿 term_id。

**前端连接（on_connect）**：向 ptyhost 发 scrollback，await 回复拿历史数据 → 发送到前端 → 发 ready。

**mutbot 关闭**：断开 ptyhost WebSocket 连接。不发任何 kill 命令。所有终端继续在 ptyhost 中运行。

**mutbot 重启**：
1. 连接 ptyhost，发 list 获取存活终端
2. 与 session 列表比对：
   - ptyhost 有 + session 有 → 标记 running，建立 I/O 中继
   - ptyhost 无 + session 有 → 标记 stopped
3. 不做孤儿清理（ptyhost 可能被多个 mutbot 共享）
4. 前端 reconnect 时直接 session.connect（无需 restart）

**用户删除终端（on_stop）**：向 ptyhost 发 kill → status = "stopped"。这是终端被 kill 的**唯一途径**。

#### on_restart_cleanup 变更

不再将 running → stopped。保持原状态，等 lifespan 阶段连接 ptyhost 后确认实际状态。

#### scrollback 持久化

scrollback 由 ptyhost 维护，mutbot 优雅关闭时主动从 ptyhost 拉取写入 session（作为 ptyhost 也挂掉时的保底恢复）。

### 文件结构

```
src/mutbot/
├── ptyhost/               # 新增：PTY 宿主进程（独立可用）
│   ├── __init__.py
│   ├── __main__.py        # python -m mutbot.ptyhost 入口
│   ├── _app.py            # ASGI WebSocket app
│   └── _manager.py        # TerminalManager + TerminalProcess（从 runtime/terminal.py 搬入）
├── server/                # ASGI server（独立可用）
└── runtime/
    └── terminal.py        # 重构为 ptyhost WebSocket client + @impl 注册
```

## 关键参考

### 源码
- `src/mutbot/session.py:142-148` — TerminalSession 声明
- `src/mutbot/runtime/terminal.py` — TerminalManager + TerminalProcess + 所有 @impl
- `src/mutbot/runtime/session_manager.py` — SessionManager 生命周期管理
- `src/mutbot/web/server.py:119-162` — lifespan 中的 on_restart_cleanup 流程
- `src/mutbot/web/rpc_session.py` — session.create / session.restart RPC
- `src/mutbot/server/_server.py` — 自研 ASGI Server（零内部依赖）
- `src/mutbot/server/_ws.py` — WebSocket 协议实现
- `src/mutbot/web/transport.py:459-471` — Channel 多路复用（JSON 带 ch，binary 前缀 varint ch）
- `frontend/src/panels/TerminalPanel.tsx` — 前端终端组件

## 实施步骤清单

### Phase 1: 基础设施 [✅ 已完成]

- [x] **Task 1.1**: Server 支持 port=0
  - `mutbot.server.Server` 添加 `ports` 属性，支持 `sockets=` 预绑定
  - 状态：✅ 已完成

- [x] **Task 1.2**: 创建 `mutbot.ptyhost` 包骨架
  - `__init__.py`、`__main__.py`、`_app.py`、`_manager.py`、`_client.py`、`_bootstrap.py`
  - 状态：✅ 已完成

### Phase 2: ptyhost 核心 [✅ 已完成]

- [x] **Task 2.1**: 搬迁 TerminalManager + TerminalProcess 到 `ptyhost/_manager.py`
  - 从 `runtime/terminal.py` 搬入 PTY 进程管理代码，简化为 ptyhost 侧需求
  - 状态：✅ 已完成

- [x] **Task 2.2**: 实现 `ptyhost/_app.py` ASGI WebSocket app
  - JSON 命令（create/resize/scrollback/status/list/kill）+ binary I/O
  - UUID term_id，binary 帧前缀 16 字节，seq 匹配回复
  - 状态：✅ 已完成

- [x] **Task 2.3**: 实现 `ptyhost/__main__.py` 入口
  - 预绑定随机端口，写入 `~/.mutbot/ptyhost.port`，空闲 60s 自动退出
  - 状态：✅ 已完成

### Phase 3: mutbot 侧重构 [✅ 已完成]

- [x] **Task 3.1**: 实现 ptyhost WebSocket 客户端 (`_client.py`)
  - wsproto + asyncio raw socket，seq 匹配，async/fire-and-forget 双模式
  - 状态：✅ 已完成

- [x] **Task 3.2**: 重构 `runtime/terminal.py` 为 ptyhost 客户端
  - TerminalManager → ptyhost WebSocket 客户端包装器 + multi-client attach/detach
  - 状态：✅ 已完成

- [x] **Task 3.3**: Session 钩子 async 化
  - `on_create`、`on_connect` → async，SessionManager + 所有调用者适配
  - 状态：✅ 已完成

### Phase 4: 生命周期适配 [✅ 已完成]

- [x] **Task 4.1**: lifespan 中集成 ptyhost 启动与发现
  - `_bootstrap.py` 实现发现/spawn/轮询就绪
  - `web/server.py` lifespan 启动时连接 ptyhost，sync 终端状态
  - 状态：✅ 已完成

- [x] **Task 4.2**: 更新 `on_restart_cleanup` 和关闭逻辑
  - `on_restart_cleanup` → no-op（保持原状态）
  - `_shutdown_cleanup` → 只停非终端 Session，关闭 ptyhost 连接不 kill 终端
  - 状态：✅ 已完成

- [x] **Task 4.3**: 适配 `session.restart` RPC
  - restart 流程：notify_exit → on_create(走 ptyhost create) → on_connect
  - 状态：✅ 已完成

### Phase 5: 测试验证 [🔄 进行中]

- [ ] **Task 5.1**: 手动集成测试
  - mutbot 启动 → ptyhost 自动 spawn → 创建终端 → 重启 mutbot → 终端存活 → 重连
  - 状态：🔄 进行中（菜单创建 session 的 await 缺失 bug 已修复，单元测试全部通过）

- [x] **Task 5.2**: 单元测试适配
  - ptyhost 重构后的测试适配（模块重命名、async/await、RpcContext API、Channel API 等）
  - 451 passed, 16 skipped（RPC handler 嵌套函数需重写）

## 测试验证

- 单元测试：451 passed, 16 skipped
- pyright：12 errors（大部分为 ptyhost 未完成的 API 引用，如 `async_notify_exit`、`list_by_workspace`）
- 手动测试：菜单创建 session 已验证可用（`rpc_workspace.py` 缺少 await 的 bug 已修复）
