# Session Channel 架构重构 设计规范

**状态**：✅ 已完成
**日期**：2026-03-08
**类型**：重构

## 背景

`mutbot/src/mutbot/web/routes.py` 膨胀至 1,618 行，但问题不仅是文件大小——更深层的是架构缺陷：

1. **Channel 消息调度硬编码 isinstance switch**：`_handle_channel_json` 按 Session 类型分支处理，每新增一种 Session 都要改这个 switch
2. **Channel 基础设施和业务逻辑混杂**：terminal attach/detach/scrollback 和 agent message/cancel 全在同一层
3. **Session 没有通信能力**：Session 是被动的数据声明，channel 连接、消息处理都由 routes.py 外部硬编码
4. **前端直接操作基础设施**：前端调用 `channel.open` 暴露了传输层细节
5. **具体业务实现分散**：TerminalSession 的声明、生命周期 @impl、通信逻辑分散在 session.py、session_impl.py、routes.py 三处
6. **广播逻辑外挂**：Session 级广播（同 session 多 channel）由外部函数拼装，每个业务都要自己构造 broadcast_fn

### 核心洞察

mutbot 定义了 Session 与前端的通信方式——通过 Channel。这是 mutbot 的核心架构概念，不是 web 层的实现细节。Session 无法不知道 Channel 的存在，因为没有 Channel 它就无法与前端通信。

Session 的设计允许多个 Client 连接，因此 Session 级广播是框架级能力——只有 Session 知道自己有哪些 Channel、该广播给谁。

### 复盘经验（来自 `docs/postmortems/`）

- **拆文件不等于拆架构**——职责混杂的原因不是文件不够多，而是缺少正确的抽象层次
- **"谁不应该知道谁"比"谁应该知道谁"更有设计指导力**——Channel 不应该知道 Session 类型，前端不应该知道 Channel
- **把判断放在正确的层做**——后端拥有完整信息，应该由后端驱动；前端不应推理后端状态
- **真正的简化是让正确的组件承担正确的职责**——而不是减少代码行数

## 设计方案

### 设计原则

- **Channel 是 mutbot 核心层抽象**——定义 Session 的通信基础设施
- **Session 声明通信行为**——`on_connect` / `on_message` / `on_data` / `on_disconnect` 是 Session 的能力
- **Session 拥有广播能力**——框架自动管理 channel 列表，Session 内建 `broadcast_json` / `broadcast_binary`
- **前端面向 Session，不面向 Channel**——`session.connect` 而非 `channel.open`
- **传输实现在 web 层**——WebSocket 多路复用、帧格式、ACK 是 `web/transport.py` 的事
- **具体业务自包含**——Terminal 的所有实现（Manager + @impl）在同一模块，Agent 同理

### 层次结构

```
mutbot/
  channel.py                    # Channel 抽象 + ChannelContext（核心层）
  session.py                    # Session 基类（broadcast 桩方法 + 通信回调桩）
                                # + SessionChannels Extension + broadcast @impl
                                # + AgentSession / TerminalSession / DocumentSession 声明

  runtime/
    session_manager.py          # SessionManager（CRUD、持久化、启停调度）← 从 session_impl.py 重命名
    terminal.py                 # TerminalProcess + TerminalManager + TerminalSession 全部 @impl
    agent_bridge.py             # AgentBridge + AgentSession 全部 @impl ← 扩展已有文件

  web/
    transport.py                # Channel 的 WebSocket 实现 + Client + ChannelManager
    routes.py                   # WebSocket 端点（薄协调层）
    rpc_app.py                  # App 级 RPC handler
    rpc_session.py              # Session 级 RPC handler（含 session.connect）
    rpc_workspace.py            # Workspace 级 RPC handler
    serializers.py              # 数据序列化（已有 + 扩展）
```

### Channel 核心抽象

`mutbot/channel.py` — Session 看到的通信管道接口：

```python
import mutobj


class Channel(mutobj.Declaration):
    """Session 与前端之间的通信管道。

    Session 通过 Channel 发送消息给前端，通过 on_message / on_data
    接收前端消息。Channel 的传输实现（WebSocket 多路复用）对 Session 透明。
    """

    ch: int                        # 频道 ID（前端路由用）
    session_id: str = ""           # 关联的 Session ID（消息路由用）

    def send_json(self, data: dict) -> None:
        """发送 JSON 消息到前端（仅此 channel）。"""
        ...

    def send_binary(self, data: bytes) -> None:
        """发送二进制数据到前端（仅此 channel）。"""
        ...
```

### Channel 传输层实现

`mutbot/web/transport.py` — Channel 的 WebSocket 实现。`client` 引用是传输层细节，不属于 Channel 声明，通过 Extension 存储：

```python
class ChannelTransport(mutobj.Extension[Channel]):
    """Channel 的 WebSocket 传输状态——对 Session 透明。"""
    _client: Client = None


@mutobj.impl(Channel.send_json)
def channel_send_json(self: Channel, data: dict) -> None:
    client = ChannelTransport.get_or_create(self)._client
    client.enqueue("json", {"ch": self.ch, **data})

@mutobj.impl(Channel.send_binary)
def channel_send_binary(self: Channel, data: bytes) -> None:
    client = ChannelTransport.get_or_create(self)._client
    prefix = encode_varint(self.ch)
    client.enqueue("binary", prefix + data)
```

ChannelManager.open() 创建 Channel 后设置 transport：

```python
def open(self, client: Client, session_id: str | None = None) -> Channel:
    ch_id = self._alloc_id()
    channel = Channel(ch=ch_id, session_id=session_id or "")
    ChannelTransport.get_or_create(channel)._client = client
    # ... 映射表注册 ...
    return channel
```

### RpcContext 类型安全改造

`mutbot/web/rpc.py` — `managers: dict[str, Any]` 改为具名字段，使用 TYPE_CHECKING 延迟导入：

```python
from __future__ import annotations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mutbot.runtime.session_manager import SessionManager
    from mutbot.runtime.terminal import TerminalManager
    from mutbot.web.transport import ChannelManager
    from mutbot.runtime.workspace import WorkspaceManager

@dataclass
class RpcContext:
    """RPC 调用上下文，传递给每个 handler"""
    workspace_id: str
    broadcast: Callable[[dict], Awaitable[None]]
    sender_ws: WebSocket | None = None
    _post_send: Callable[[], Any] | None = None

    # 类型安全的 manager 访问（部分字段 Optional，app 级不注入全部 manager）
    session_manager: SessionManager | None = None
    workspace_manager: WorkspaceManager | None = None
    terminal_manager: TerminalManager | None = None
    channel_manager: ChannelManager | None = None
    config: Any = None
    event_loop: asyncio.AbstractEventLoop | None = None
```

app 级和 workspace 级共用同一个 RpcContext 类（部分字段 Optional）——简单不过度设计。拆分到 rpc_app.py / rpc_session.py / rpc_workspace.py 后，各文件的 handler 天然只用各自需要的字段。

改造随 routes.py 拆分一起进行——每个 handler 搬迁时顺手将 `ctx.managers.get("session_manager")` 改为 `ctx.session_manager`，不需要单独一轮修改。

### ChannelContext — Session 通信上下文

`mutbot/channel.py` — ChannelContext 与 RpcContext 职责不同（ChannelContext 是 Session 通信上下文，不含 broadcast/sender_ws 等 RPC 概念），保持独立。RpcContext 提供工厂方法构造 ChannelContext 以避免重复组装：

```python
class ChannelContext:
    """Channel 操作的运行时上下文。"""
    workspace_id: str
    session_manager: SessionManager
    terminal_manager: TerminalManager
    event_loop: asyncio.AbstractEventLoop
```

```python
# RpcContext 工厂方法
class RpcContext:
    ...
    def make_channel_context(self) -> ChannelContext:
        return ChannelContext(
            workspace_id=self.workspace_id,
            session_manager=self.session_manager,
            terminal_manager=self.terminal_manager,
            event_loop=self.event_loop or asyncio.get_running_loop(),
        )
```

### Session 声明通信行为 + 广播能力

`mutbot/session.py` — Session 增加 channel 通信和广播：

```python
class Session(mutobj.Declaration):
    # ... 现有字段不变 ...

    # --- 广播能力（Declaration 桩，使用者可见）---

    def broadcast_json(self, data: dict) -> None:
        """广播 JSON 到连接此 Session 的所有 channel。"""
        ...

    def broadcast_binary(self, data: bytes) -> None:
        """广播 binary 到连接此 Session 的所有 channel。"""
        ...

    # --- Channel 生命周期回调（Declaration 桩）---

    def on_connect(self, channel: Channel, ctx: ChannelContext) -> None:
        """前端连接到此 Session 时调用。"""
        ...

    def on_disconnect(self, channel: Channel, ctx: ChannelContext) -> None:
        """前端断开此 Session 的 channel 时调用。"""
        ...

    async def on_message(self, channel: Channel, raw: dict, ctx: ChannelContext) -> None:
        """收到前端 JSON 消息。"""
        ...

    async def on_data(self, channel: Channel, payload: bytes, ctx: ChannelContext) -> None:
        """收到前端二进制数据。"""
        ...
```

Session 声明层保留 AgentSession / TerminalSession / DocumentSession 的声明——它们是业务基类，未来可派生出更具体的类型。

### 广播实现——Extension 隐藏 channel 管理细节

`_channels` 列表是实现细节，不属于 Declaration 公开声明（Declaration 是使用者视角，使用者关心"我要广播"，不关心 channel 列表管理）。使用 `mutobj.Extension` 隐藏：

```python
# mutbot/session.py

class SessionChannels(mutobj.Extension[Session]):
    """Session 的 channel 管理——框架自动维护，@impl 不需要关心。"""
    _channels: list[Channel] = mutobj.field(default_factory=list)


# broadcast_json / broadcast_binary 的 @impl（同在 session.py 中）
@impl(Session.broadcast_json)
def session_broadcast_json(self: Session, data: dict) -> None:
    for ch in SessionChannels.get_or_create(self)._channels:
        ch.send_json(data)

@impl(Session.broadcast_binary)
def session_broadcast_binary(self: Session, data: bytes) -> None:
    for ch in SessionChannels.get_or_create(self)._channels:
        ch.send_binary(data)
```

### 框架自动管理 channel 列表

`session.connect` / `session.disconnect` 中框架通过 Extension 维护 `_channels`，@impl 不需要关心：

```python
# web/rpc_session.py — session.connect
channel = cm.open(client, session_id=session_id)
SessionChannels.get_or_create(session)._channels.append(channel)  # 框架维护（Extension）
session.on_connect(channel, ch_ctx)                                # 业务逻辑
return {"ch": channel.ch}

# session.disconnect / 被动断开
session.on_disconnect(channel, ctx)                                # 业务逻辑
SessionChannels.get_or_create(session)._channels.remove(channel)  # 框架维护（Extension）
cm.close(channel.ch)
```

### 具体业务自包含

#### TerminalSession — `runtime/terminal.py`

将 TerminalSession 的所有 @impl（生命周期 + 通信）归到 `runtime/terminal.py`，与 TerminalManager 在一起。理由：每个 @impl 本质上都是调 TerminalManager 的方法，它们是同一功能模块的两面。

同时重命名：`terminal.py` 中的 `TerminalSession` dataclass → `TerminalProcess`（避免与 Declaration 层的 TerminalSession 同名混淆）。

```python
# runtime/terminal.py

from dataclasses import dataclass

@dataclass
class TerminalProcess:
    """PTY 进程运行时状态（原名 TerminalSession，与 Declaration 同名易混淆）。"""
    id: str
    workspace_id: str
    rows: int
    cols: int
    process: Any = None
    reader_thread: threading.Thread | None = None
    alive: bool = True
    exit_code: int | None = None
    _scrollback: bytearray = ...
    ...


class TerminalManager:
    """管理 TerminalProcess 的生命周期（PTY 创建、I/O、销毁）。"""
    _sessions: dict[str, TerminalProcess]   # 重命名内部字段
    ...


# --- TerminalSession @impl（生命周期）---

@impl(TerminalSession.on_create)
def terminal_on_create(self, sm):
    """创建 PTY，恢复 scrollback。"""
    # 当前 session_impl.py 中的逻辑迁入
    ...

@impl(TerminalSession.on_stop)
def terminal_on_stop(self, sm):
    """保存 scrollback，kill PTY。"""
    ...

# --- TerminalSession @impl（Channel 通信）---

@impl(TerminalSession.on_connect)
def terminal_on_connect(self, channel, ctx):
    """attach PTY + scrollback replay + 发送 ready。"""
    tm = ctx.terminal_manager
    term_id = self.config.get("terminal_id", "")
    # 发送清屏 + scrollback
    scrollback = tm.get_scrollback(term_id) or b""
    channel.send_binary(_CLEAR_SCREEN + _strip_replay_queries(scrollback))
    # 判断 PTY 状态，发送 ready
    ts = tm.get(term_id)
    if ts and ts.alive:
        channel.send_json({"type": "ready", "alive": True})
        tm.attach(term_id, ..., on_output=lambda data: channel.send_binary(data), ...)
    else:
        channel.send_json({"type": "ready", "alive": False})
    ...

@impl(TerminalSession.on_message)
async def terminal_on_message(self, channel, raw, ctx):
    """处理 resize。"""
    if raw.get("type") == "resize":
        tm = ctx.terminal_manager
        actual = tm.resize(self.config["terminal_id"], raw["rows"], raw["cols"], ...)
        if actual:
            # 框架级广播——不需要知道 ChannelManager
            self.broadcast_json({"type": "pty_resize", "rows": actual[0], "cols": actual[1]})
    ...

@impl(TerminalSession.on_data)
async def terminal_on_data(self, channel, payload, ctx):
    """键盘输入转发到 PTY。"""
    ctx.terminal_manager.write(self.config["terminal_id"], payload)

@impl(TerminalSession.on_disconnect)
def terminal_on_disconnect(self, channel, ctx):
    """detach PTY。"""
    ctx.terminal_manager.detach(self.config["terminal_id"], ...)
    ...
```

注意：
- @impl 通过 `ctx.terminal_manager`（类型安全）访问 manager，不依赖 web 层
- resize 广播使用 `self.broadcast_json()`（Session 框架能力），不需要自己找 ChannelManager

#### AgentSession — `runtime/agent_bridge.py`

将 AgentSession 的 @impl 归到 `runtime/agent_bridge.py`（已有 AgentBridge），让 Agent 功能自包含：

```python
# runtime/agent_bridge.py

class AgentBridge:
    """Manages one Session's Agent as an asyncio task."""

    def __init__(self, session_id, agent, loop, session: AgentSession):
        self.session = session
        # broadcast_fn 不再需要外部传入——直接用 session.broadcast_json
        ...

    def _broadcast(self, data: dict) -> None:
        """通过 Session 广播到所有 channel。"""
        self.session.broadcast_json(data)
    ...


# --- AgentSession @impl（生命周期）---

@impl(AgentSession.on_create)
def agent_on_create(self, sm):
    ...

@impl(AgentSession.on_stop)
def agent_on_stop(self, sm):
    ...

# --- AgentSession @impl（Channel 通信）---

@impl(AgentSession.on_connect)
def agent_on_connect(self, channel, ctx):
    """确保 bridge 已启动。"""
    ...

@impl(AgentSession.on_message)
async def agent_on_message(self, channel, raw, ctx):
    """处理 message / cancel / run_tool / ui_event / log / stop。"""
    sm = ctx.session_manager
    msg_type = raw.get("type", "")

    if msg_type == "message":
        text = raw.get("text", "")
        if text:
            bridge = sm.get_bridge(self.id)
            if bridge is None:
                bridge = sm.start(self.id, ctx.event_loop, session=self)
            bridge.send_message(text, raw.get("data"))
    elif msg_type == "cancel":
        bridge = sm.get_bridge(self.id)
        if bridge:
            await bridge.cancel()
    # ... run_tool, ui_event, log, stop ...
```

注意：AgentBridge 不再需要外部传入 `broadcast_fn`——它持有 session 引用，直接调 `self.session.broadcast_json(data)`。这消除了 `_make_channel_broadcast_fn` 辅助函数。

#### SessionManager — `runtime/session_manager.py`

`session_impl.py` 重命名为 `session_manager.py`，只保留 SessionManager（纯基础设施）：
- Session CRUD（create / get / list / update / delete）
- 持久化（load_from_disk / _persist / persist_dirty_loop）
- Agent 启停调度（start / stop）— `start()` 签名简化，不再需要 broadcast_fn 参数
- serialize / deserialize 辅助

移出的内容：
- TerminalSession 的 @impl → `runtime/terminal.py`
- AgentSession 的 @impl → `runtime/agent_bridge.py`

### 前端 API 变更

**现在**（暴露基础设施）：
```typescript
const ch = await rpc.openChannel("session", { session_id });
rpc.onChannel(ch, handler);
// 断开
rpc.closeChannel(ch);
```

**改为**（面向业务）：
```typescript
const { ch } = await rpc.call("session.connect", { session_id });
rpc.onChannel(ch, handler);      // channel 路由机制不变
// 断开
await rpc.call("session.disconnect", { session_id, ch });
```

前端的 `onChannel` / `sendToChannel` / `sendBinaryToChannel` 机制不变——这些是 WorkspaceRpc 内部的 channel 路由，属于传输层。变的只是打开/关闭 channel 的入口从 `channel.open` 变成 `session.connect`。

### session.connect RPC 实现

```python
# web/rpc_session.py

@workspace_rpc.method("session.connect")
async def handle_session_connect(params, ctx):
    session_id = params["session_id"]
    sm = ctx.session_manager
    cm = ctx.channel_manager

    session = sm.get(session_id)
    if session is None:
        return {"error": "session not found"}

    # 1. 分配 channel（基础设施）
    client = find_client_by_ws(ctx.sender_ws)
    channel = cm.open(client, session_id=session_id)

    # 2. 框架管理 channel 列表（Extension）
    SessionChannels.get_or_create(session)._channels.append(channel)

    # 3. 构建 ChannelContext（从 RpcContext 工厂方法）
    ch_ctx = ctx.make_channel_context()

    # 4. 调用 Session 自己的 on_connect（零 isinstance）
    session.on_connect(channel, ch_ctx)

    return {"ch": channel.ch}
```

### WebSocket 消息循环简化

`routes.py` 中 `websocket_workspace` 的消息循环：

```python
# 现在（硬编码分发）
if ch > 0:
    channel = cm.get_channel(ch)
    if channel:
        await _handle_channel_json(channel, raw, sm, cm, loop, ctx)

# 改为（Session 自分发）
if ch > 0:
    channel = cm.get_channel(ch)
    if channel and channel.session_id:
        session = sm.get(channel.session_id)
        if session:
            await session.on_message(channel, raw, ch_ctx)
```

同理 binary：

```python
if channel and channel.session_id:
    session = sm.get(channel.session_id)
    if session:
        await session.on_data(channel, data[consumed:], ch_ctx)
```

### routes.py 拆分

解决了核心架构问题后，routes.py 自然拆分：

| 模块 | 职责 | 预估行数 |
|------|------|----------|
| `routes.py` | Router、WebSocket 端点、Client 注册表、workspace 广播 | ~350 |
| `rpc_app.py` | App 级 RPC：workspace CRUD、filesystem、app menu | ~170 |
| `rpc_session.py` | Session RPC：CRUD + connect/disconnect | ~250 |
| `rpc_workspace.py` | Workspace 级 RPC：workspace/terminal/file/log/config/menu | ~200 |
| `serializers.py` | 已有消息序列化 + workspace/session/terminal 序列化 | ~250 |

注意：原方案中的 `web/session_handlers/` 目录不再需要——Session 通信的 @impl 已归入各自的业务模块（`runtime/terminal.py`、`runtime/agent_bridge.py`），不在 web 层。

### 模块间依赖关系

```
mutbot/channel.py              ← 核心抽象：Channel Declaration + ChannelContext
mutbot/session.py              ← 依赖 channel.py（Channel, ChannelContext）
                                  声明 broadcast_json / broadcast_binary 桩方法
                                  SessionChannels Extension + broadcast @impl

mutbot/runtime/
  session_manager.py           ← SessionManager（纯基础设施）
  terminal.py                  ← TerminalProcess + TerminalManager + TerminalSession @impl
                                  依赖：channel.py, session.py（声明层）
  agent_bridge.py              ← AgentBridge + AgentSession @impl
                                  依赖：channel.py, session.py（声明层）
                                  AgentBridge 通过 session.broadcast_json 广播

mutbot/web/
  transport.py                 ← ChannelTransport Extension + Channel @impl (send_json/send_binary)
                                  + Client + ChannelManager
  routes.py                    ← WebSocket 端点，import rpc_*.py 触发注册
  rpc_app.py                   ← app_rpc handlers
  rpc_session.py               ← workspace_rpc handlers（含 session.connect/disconnect）
                                  通过 SessionChannels Extension 管理 channel 列表
  rpc_workspace.py             ← workspace_rpc handlers
  serializers.py               ← 数据转换（无状态）
```

依赖方向（严格单向）：

```
web/rpc_*.py → web/routes.py（dispatcher, workspace 广播）
web/rpc_*.py → web/serializers.py（数据转换）
web/rpc_session.py → channel.py（ChannelContext）
web/rpc_session.py → session.py（SessionChannels Extension）
web/routes.py → web/transport.py（Client, ChannelManager）
web/transport.py → channel.py（Channel @impl）
runtime/terminal.py → channel.py, session.py（声明层）
runtime/agent_bridge.py → channel.py, session.py（声明层）
runtime/session_manager.py → session.py
session.py → channel.py
```

### @impl 加载时机

`runtime/terminal.py` 和 `runtime/agent_bridge.py` 中的 @impl 需要在 Session 方法被调用前 import。当前 `mutbot/__init__.py` 已有 `import mutbot.builtins`，可以在 builtins 中触发加载：

```python
# mutbot/builtins/__init__.py
import mutbot.runtime.terminal       # noqa: F401  ← 触发 TerminalSession @impl
import mutbot.runtime.agent_bridge   # noqa: F401  ← 触发 AgentSession @impl
```

### TerminalProcess 重命名

`runtime/terminal.py` 中的 `TerminalSession` dataclass 重命名为 `TerminalProcess`，避免与 Declaration 层的 `mutbot.session.TerminalSession` 同名混淆。

影响范围：
- `runtime/terminal.py` 内部：类定义 + TerminalManager 中所有引用
- `runtime/session_impl.py`（→ session_manager.py）：如有类型注解引用
- 不影响外部 API——这个 dataclass 不暴露给 web 层或前端

### ChannelManager.open() 接口简化

移除 `target` 参数——分析确认 `target` 当前只是存储但未被用于路由，实际分发靠的是 isinstance switch。新设计中 channel 的用途由 Session 类型通过 @impl 决定，不需要额外标记。

```python
# 现在
channel = cm.open(client, "session", session_id=session_id)

# 改为
channel = cm.open(client, session_id=session_id)
```

### AgentBridge 简化

AgentBridge 不再需要外部传入 `broadcast_fn` 参数：
- **现在**：`sm.start(session_id, loop, broadcast_fn)` — broadcast_fn 在 routes.py 中用 ChannelManager 构造
- **改为**：`sm.start(session_id, loop, session=self)` — bridge 持有 session 引用，直接调 `session.broadcast_json()`

注意签名变更：现有 `broadcast_fn` 签名是 `async def(session_id: str, data: dict)`，bridge 内部 20+ 处调用。改为 `session.broadcast_json(data)` 后去掉 session_id 参数。bridge 内部的 `self.broadcast_fn(self.session_id, data)` 全部替换为 `self.session.broadcast_json(data)`。

消除的代码：
- `_make_channel_broadcast_fn()` 辅助函数
- `_ensure_agent_broadcast()` 辅助函数
- SessionManager.set_broadcast() / _broadcast_fn 机制

## 已确认决策

### RpcContext 类型安全 + ChannelContext 保持独立
RpcContext 的 `managers: dict[str, Any]` 改为具名字段（部分 Optional），随 routes.py 拆分一起改造。ChannelContext 与 RpcContext 职责不同（RPC 调度 vs Session 通信），保持独立类。RpcContext 提供 `make_channel_context()` 工厂方法构造 ChannelContext。

### session.disconnect 与被动 channel 清理
前端主动断开走 `session.disconnect`，WebSocket 断线时被动关闭在 `ChannelManager.close_all_for_client()` 中统一处理——对每个被关闭的 channel，如果有 session_id，查找 session 并执行 `session.on_disconnect(channel, ctx)` + 从 `SessionChannels` Extension 中移除。

### channel.open 前端调用完全移除
前端不再暴露 `openChannel()` / `closeChannel()`，统一使用 `session.connect` / `session.disconnect`。

### DocumentSession 无需 channel @impl
Declaration 桩方法默认空操作，未提供 @impl 时调用桩即可。DocumentSession 连接后如不需要实时通信，on_connect 空操作。

### 增量过渡策略
Session 通信 @impl、routes.py 重构、前端 API 变更三者强耦合。采用增量过渡降低风险：
1. 先加新路径（session.connect + Session 通信 @impl），新旧路径并存
2. 前端切换到 session.connect
3. 移除旧路径（channel.open RPC + isinstance switch + broadcast_fn 机制）

每一步都保持系统可运行。

## 实施步骤清单

### Phase 1: 核心抽象层 [✅ 已完成]

纯新增代码，不修改现有行为。

- [x] **Task 1.1**: 创建 `mutbot/channel.py`
  - [x] Channel Declaration（ch + session_id + send_json/send_binary 桩）
  - [x] ChannelContext 类（具名字段 + TYPE_CHECKING 延迟导入）
  - 状态：✅ 已完成

- [x] **Task 1.2**: 更新 `mutbot/session.py`
  - [x] 添加 broadcast_json / broadcast_binary 桩方法
  - [x] 添加 on_connect / on_disconnect / on_message / on_data 桩方法
  - [x] 添加 SessionChannels Extension + broadcast @impl
  - 状态：✅ 已完成

- [x] **Task 1.3**: 验证核心抽象
  - [x] import 无循环依赖：`channel.py ← session.py`（单向）
  - [x] Session 子类（AgentSession/TerminalSession）自动继承新桩方法
  - [x] broadcast @impl 可调用（无 channel 时空操作）
  - 状态：✅ 已完成

### Phase 2: 文件重组 [✅ 已完成]

重命名和移动文件，更新 import。不改逻辑。

- [x] **Task 2.1**: TerminalSession dataclass → TerminalProcess
  - [x] `runtime/terminal.py` 中类重命名 + TerminalManager 内部引用
  - [x] 全局搜索确认无遗漏引用（session_impl.py 中的 TerminalSession 是 Declaration，不改）
  - 状态：✅ 已完成

- [x] **Task 2.2**: `session_impl.py` → `session_manager.py`
  - [x] 重命名文件（git mv）
  - [x] 更新所有 import 路径（session.py、channel.py、server.py、config_toolkit.py、session_toolkit.py）
  - 状态：✅ 已完成

- [x] **Task 2.3**: `web/agent_bridge.py` → `runtime/agent_bridge.py`
  - [x] 移动文件（git mv）
  - [x] 更新 session_manager.py 中的 import 路径
  - 状态：✅ 已完成

- [x] **Task 2.4**: 验证文件重组
  - [x] 所有 import 正常（mutbot、SessionManager、TerminalProcess、AgentBridge）
  - 状态：✅ 已完成

### Phase 3: Channel 传输层改造 [✅ 已完成]

将 transport.py 中的 Channel 类替换为 Declaration 实例 + @impl。

- [x] **Task 3.1**: 改造 `web/transport.py`
  - [x] 移除旧 Channel 类
  - [x] 添加 ChannelTransport Extension（存储 client 引用）
  - [x] 添加 Channel.send_json / send_binary 的 @impl
  - [x] ChannelManager.open() 改为创建 Channel Declaration 实例 + 设置 ChannelTransport
  - [x] 移除 target 参数
  - [x] 方法重命名：enqueue_json → send_json、enqueue_binary → send_binary
  - 状态：✅ 已完成

- [x] **Task 3.2**: 更新 transport.py 的调用方
  - [x] routes.py 中所有 `channel.enqueue_json` → `channel.send_json`
  - [x] routes.py 中所有 `channel.enqueue_binary` → `channel.send_binary`
  - [x] routes.py 中 `cm.open(client, "session", ...)` → `cm.open(client, ...)`
  - [x] routes.py 中 `channel.client` → `ChannelTransport.get(channel)._client`
  - 状态：✅ 已完成

- [x] **Task 3.3**: 验证传输层改造
  - [x] Channel Declaration + @impl 可正常 send_json/send_binary
  - [x] ChannelManager.open/close 工作正常
  - [x] routes.py import 正常
  - 状态：✅ 已完成

### Phase 4: RpcContext 类型安全 [✅ 已完成]

- [x] **Task 4.1**: 改造 `web/rpc.py` RpcContext
  - [x] `managers: dict[str, Any]` → 具名字段（部分 Optional）
  - [x] 添加 `make_channel_context()` 工厂方法
  - 状态：✅ 已完成

- [x] **Task 4.2**: 更新 routes.py 中所有 `ctx.managers.get(...)` 调用
  - [x] ~15 处 session_manager、~15 处 workspace_manager、~5 处 terminal_manager、~3 处 channel_manager、~1 处 config
  - [x] 更新 RpcContext 创建处（app WS + workspace WS）
  - 状态：✅ 已完成

- [x] **Task 4.3**: 验证 RpcContext 改造
  - [x] RpcContext 字段正确、routes.py import 正常
  - 状态：✅ 已完成

### Phase 5: Session 通信 @impl + 新路径 [✅ 已完成]

添加 session.connect 新路径，保留旧路径（channel.open）并存。

- [x] **Task 5.1**: TerminalSession 通信 @impl — `runtime/terminal.py`
  - [x] 从 session_manager.py 迁入 on_create / on_stop / on_restart_cleanup @impl
  - [x] 从 routes.py 迁入 on_connect（attach PTY + scrollback + ready）
  - [x] 从 routes.py 迁入 on_disconnect（detach PTY）
  - [x] 从 routes.py 迁入 on_message（resize → broadcast_json）
  - [x] 从 routes.py 迁入 on_data（键盘输入 → PTY write）
  - 状态：✅ 已完成

- [x] **Task 5.2**: AgentSession 通信 @impl — `runtime/agent_bridge.py`
  - [x] 添加 on_connect @impl（空操作）
  - [x] 从 routes.py 迁入 on_message @impl（message/cancel/run_tool/ui_event/log/stop）
  - [x] AgentBridge 改造：移除 broadcast_fn 依赖，直接使用 session.broadcast_json
  - [x] AgentBridge 内部 17 处 `self.broadcast_fn(self.session_id, data)` → `self._session.broadcast_json(data)`
  - [x] UIToolkit._resolve_broadcast() 改用 session.broadcast_json
  - 状态：✅ 已完成

- [x] **Task 5.3**: @impl 加载注册
  - [x] `builtins/__init__.py` 添加 `import mutbot.runtime.terminal`
  - [x] 确认 @impl 在 Session 方法被调用前已加载
  - 状态：✅ 已完成

- [x] **Task 5.4**: 添加 session.connect / session.disconnect RPC
  - [x] `web/rpc_session.py`（新文件）实现 session.connect handler
  - [x] 实现 session.disconnect handler
  - [x] 框架管理 SessionChannels Extension（connect 时 append，disconnect 时 remove）
  - [x] routes.py 注册新 RPC handler
  - 状态：✅ 已完成

- [x] **Task 5.5**: WebSocket 消息循环添加新路径
  - [x] channel JSON 消息：通过 session.on_message 分发（新路径）
  - [x] channel binary 消息：通过 session.on_data 分发（新路径）
  - [x] 旧路径（_handle_channel_json/binary）保留作为 fallback
  - 状态：✅ 已完成

- [x] **Task 5.6**: 被动 channel 清理
  - [x] _on_client_expire 中触发 session.on_disconnect + SessionChannels 移除
  - [x] 旧路径 channel.open/close 中也维护 SessionChannels Extension
  - 状态：✅ 已完成

- [x] **Task 5.7**: 验证新路径（后端）
  - [x] 所有 import 正常
  - [x] @impl 注册正确（on_connect/on_message/on_data/on_disconnect）
  - 状态：✅ 已完成

### Phase 6: 前端切换 [✅ 已完成]

前端从 channel.open 切换到 session.connect。

- [x] **Task 6.1**: 前端适配 session.connect
  - [x] workspace-rpc.ts：添加 `cleanupChannelHandlers(ch)` 本地清理方法
  - [x] AgentPanel.tsx：`rpc.openChannel("session", {session_id})` → `rpc.call("session.connect", {session_id})`
  - [x] TerminalPanel.tsx：同上，添加 `channelSessionId` 变量跟踪 disconnect 所需的 session_id
  - [x] 清理时 `rpc.closeChannel(ch)` → `rpc.cleanupChannelHandlers(ch)` + `rpc.call("session.disconnect", {session_id, ch})`
  - [x] remote-log.ts：无需修改（使用 sendToChannel，保持不变）
  - 状态：✅ 已完成

- [x] **Task 6.2**: 验证前端切换
  - [x] TypeScript 编译通过
  - [x] 构建前端：`npm --prefix frontend run build` ✓
  - 状态：✅ 已完成

### Phase 7: 清理旧路径 + routes.py 拆分 [✅ 已完成]

移除旧路径，拆分 routes.py。

- [x] **Task 7.1**: 移除旧路径
  - [x] 移除 channel.open / channel.close RPC handler
  - [x] 移除 `_handle_channel_json` / `_handle_channel_binary` isinstance switch
  - [x] 移除 `_attach_terminal_channel` / `_detach_terminal_channel`
  - [x] 移除 `_make_channel_broadcast_fn` / `_ensure_agent_broadcast`
  - [x] 移除 `_ensure_channel_rpc_registered`
  - [x] 移除 `_broadcast_pty_resize` / `_CSI_QUERY_RE` / `_CLEAR_SCREEN` / `_strip_replay_queries`
  - [x] 移除 `fe_logger`（已由 agent_bridge.py 处理）
  - [x] session_manager.py: 移除 `broadcast_fn` 参数和 ToolSet 注入
  - [x] session.restart: 改用 session.on_connect 替代 `_attach_terminal_channel`
  - [x] session.run_setup: 移除 `_make_channel_broadcast_fn` 调用
  - [x] `_close_channels_for_session`: 改用 session.on_disconnect
  - [x] WS 消息循环: 移除旧路径 fallback
  - [x] 前端移除 `openChannel()` / `closeChannel()` 公开 API
  - [x] `builtins/__init__.py` 添加 `import mutbot.runtime.agent_bridge` 触发 @impl
  - 状态：✅ 已完成

- [x] **Task 7.2**: routes.py 拆分
  - [x] 提取 `rpc_app.py`（App 级 RPC handler）— 157 行
  - [x] 提取 `rpc_workspace.py`（Workspace 级 RPC handler）— 261 行
  - [x] 将 session RPC handler 合并到 `rpc_session.py` — 404 行
  - [x] 提取 serializer 函数到 `serializers.py` — 277 行
  - [x] routes.py 瘦身为 WebSocket 端点 + Client 注册表 + workspace 广播 — 486 行
  - 状态：✅ 已完成

- [x] **Task 7.3**: 最终验证
  - [x] 所有 Python import 正常
  - [x] 前端 TypeScript 编译通过 + 构建成功
  - [x] routes.py 从 1,618 行降至 486 行
  - 状态：✅ 已完成

## 关键参考

### 源码
- `src/mutbot/session.py` — Session Declaration 层级（Session / AgentSession / TerminalSession / DocumentSession）
- `src/mutbot/runtime/session_impl.py` — SessionManager + 现有 @impl（待拆分重命名为 session_manager.py）
- `src/mutbot/runtime/terminal.py` — TerminalProcess(现名TerminalSession) + TerminalManager
- `src/mutbot/web/agent_bridge.py` — AgentBridge（待迁入 runtime/ 并扩展 @impl）
- `src/mutbot/web/routes.py:1305-1413` — `_handle_channel_json` / `_handle_channel_binary`（待消除的 isinstance switch）
- `src/mutbot/web/routes.py:1505-1559` — `_attach_terminal_channel`（→ TerminalSession.on_connect @impl）
- `src/mutbot/web/routes.py:1592-1609` — `_detach_terminal_channel`（→ TerminalSession.on_disconnect @impl）
- `src/mutbot/web/routes.py:1612-1618` — `_ensure_agent_broadcast`（→ 由 session.broadcast_json 替代）
- `src/mutbot/web/routes.py:1420-1430` — `_make_channel_broadcast_fn`（→ 由 session.broadcast_json 替代）
- `src/mutbot/web/routes.py:1439-1486` — `_ensure_channel_rpc_registered`（→ rpc_session.py session.connect）
- `src/mutbot/web/transport.py:451-592` — 现有 Channel / ChannelManager（传输层，保留 + 简化 target）
- `src/mutbot/web/rpc.py:31-47` — RpcContext（managers dict → 具名字段改造）+ RpcDispatcher

### 前端
- `frontend/src/lib/workspace-rpc.ts:244-294` — openChannel / closeChannel（→ 移除）、onChannel / sendToChannel / sendBinaryToChannel（内部路由，保留）
- `frontend/src/panels/AgentPanel.tsx:346-406` — 当前 channel.open 调用（→ session.connect）
- `frontend/src/panels/TerminalPanel.tsx` — 当前 channel.open 调用（→ session.connect）

### 复盘文档
- `docs/postmortems/2026-03-08-channel-architecture-discovery.md` — 本次架构设计的对话过程记录
- `docs/postmortems/2026-03-08-process-iterative-bugfix.md` — 终端可靠性修复回顾，"把判断放在正确的层"原则
