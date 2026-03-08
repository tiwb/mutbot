# 统一 WebSocket 连接 设计规范

**状态**：✅ 已完成
**日期**：2026-03-06
**类型**：重构

## 背景

当前 mutbot 前端进入一个 workspace 后，需要建立多条独立的 WebSocket 连接：

| 端点 | 协议 | 用途 | 连接数 |
|------|------|------|--------|
| `/ws/workspace/{workspace_id}` | JSON-RPC | 工作区级 RPC 调用 + 服务端事件推送 | 1 |
| `/ws/session/{session_id}` | JSON | 每个 Agent session 的双向通信（用户消息、agent 流事件） | N（每个活跃 session 1 条） |
| `/ws/terminal/{term_id}` | Binary | 每个终端的 PTY I/O | M（每个活跃终端 1 条） |

**问题**：

1. **连接数膨胀**：打开 3 个 agent session + 2 个终端 = 6 条 WebSocket 连接，每条都需要独立的握手、心跳、重连逻辑
2. **重连复杂度**：每条连接独立重连，状态恢复逻辑分散在多处
3. **资源浪费**：每条连接都有 TCP 开销
4. **架构不一致**：workspace RPC 已有完善的 JSON-RPC 协议，但 session 和 terminal 各自为政
5. **Terminal 与 Session 割裂**：`TerminalSession` 已是 `Session` 子类，但通信协议完全不同，无法统一管理

**目标**：将 workspace 内所有通信统一到 `/ws/workspace/{workspace_id}` 一条连接上，通过频道（channel）多路复用。Terminal 和 Agent Session 使用统一的 channel 模型。在 WebSocket 之上构建可靠传输层，保证断线重连时消息不丢失。

## 现状分析

### 各连接承载的消息类型

**Session WebSocket** (`/ws/session/{session_id}`)：
- Client→Server：`message`、`cancel`、`run_tool`、`ui_event`、`log`、`stop`
- Server→Client：StreamEvent 系列（`turn_start`、`text_delta`、`tool_exec_start` 等）、`connections`（连接计数）
- 特点：JSON 格式，消息量大（流式文本），需要按 session 隔离
- 连接行为：连接后**手动**发消息触发 agent 响应

**Terminal WebSocket** (`/ws/terminal/{term_id}`)：
- Client→Server：`0x00 + data`（输入）、`0x02 + rows + cols`（resize）
- Server→Client：`0x01 + data`（输出）、`0x03`（scrollback 完成）、`0x04`（进程退出）
- 特点：**二进制协议**，数据量大（终端输出可能很密集），低延迟要求
- 连接行为：连接后**自动** attach 并接收输出

### 关键后端组件

- `ConnectionManager`（`connection.py`）：按 session_id 跟踪 WebSocket 连接，支持 broadcast 和 pending event 队列
- `AgentBridge`（`agent_bridge.py`）：通过 `broadcast_fn(session_id, data)` 推送 agent 事件
- `TerminalManager`（`runtime/terminal.py`）：通过 `attach(term_id, client_id, on_output, loop)` 注册输出回调，多客户端支持
- `RpcDispatcher`（`rpc.py`）：workspace RPC 的方法注册和分发

### 现状问题：Session 与 Terminal 行为不统一

| 维度 | Agent Session | Terminal Session |
|------|--------------|-----------------|
| 打开连接后 | 不推送，等用户发消息 | 自动 attach，立即推送输出 |
| 消息协议 | JSON only | Binary only |
| 连接跟踪 | `ConnectionManager` | `TerminalManager.attach()` |
| 多客户端 | 广播到所有连接 | 多回调支持 |

## 设计方案

### 核心概念

#### Channel — 替代独立 WebSocket 的多路复用单元

Channel 替代了现在各自独立的 WebSocket 连接。每个 channel 有一个服务端全局唯一的 ID。channel 与 session 解耦：

- Channel 大多数情况绑定 session，但 **session 可以为空**——workspace 也可以开单独的 channel（用于不属于任何 session 的独立功能）
- 一个 session 可以有多个 channel 连接它（不同 client、或将来同一 client 的不同用途）
- channel.open 时需要明确指定 `target` 类型和目标参数
- channel 同时支持 JSON 和 binary 消息，可混用

**channel.open 的统一行为**：打开即开始接收目标的事件推送。Agent Session 和 Terminal Session 行为一致——open 后服务端自动推送事件。

#### Client — 连接级身份

每个 WebSocket 连接代表一个 **client**。同一用户（或多个用户）可以在不同浏览器标签页中打开同一个 workspace，每个标签页是一个独立的 client。

**大部分业务逻辑不需要感知 Client**。Session 只需要知道有哪些 Channel 连接了自己，通过 Channel 发送和接收消息。Channel 上可以获取到更多信息（如来自哪个 client），但 Session 层面不需要关心。

```
Workspace
├── Client A (浏览器标签页 1)
│   ├── ch=1 → session "abc" (Agent)
│   ├── ch=2 → session "term1" (Terminal)
│   └── ch=3 → session "def" (Agent)
├── Client B (浏览器标签页 2)
│   ├── ch=4 → session "abc" (Agent)     ← 同一个 session，不同的全局 ch
│   └── ch=5 → session "term2" (Terminal)
```

### Channel ID 设计

- Channel ID（`ch`）为**从 1 开始的正整数**，服务端全局分配
- **复用最小可用自然数**（关闭后的 ID 可被新频道复用），保证 ID 尽量小以节省二进制带宽
- 全局唯一：不同 client 的 channel ID 不会重复

### Channel 协议模型

每个 channel 同时支持 JSON（Text Frame）和 Binary（Binary Frame）两种消息，可混用。channel 是统一的多路复用单元。

| 目标类型 | JSON 消息 | Binary 消息 |
|----------|-----------|-------------|
| `session`（Agent） | 用户消息、流事件等 | 将来可扩展（如大文件传输） |
| `session`（Terminal） | 将来可扩展（如元信息） | PTY I/O 数据 |

同一个 channel 既能传 JSON 又能传 binary，由目标类型决定各自的消息定义。

### 频道生命周期

客户端通过 workspace 级 RPC 管理频道。

**打开频道**：
```json
// Client→Server: 打开 Agent Session 频道
{ "type": "rpc", "id": "1", "method": "channel.open", "params": { "target": "session", "session_id": "abc123" } }

// Server→Client: 分配全局唯一 channel ID，开始推送事件
{ "type": "rpc_result", "id": "1", "result": { "ch": 1 } }

// Client→Server: 打开 Terminal Session 频道
{ "type": "rpc", "id": "2", "method": "channel.open", "params": { "target": "session", "session_id": "term1" } }

// Server→Client: 分配 channel ID，开始推送终端输出
{ "type": "rpc_result", "id": "2", "result": { "ch": 2 } }
```

`target` 参数明确指定频道类型。当前 target 只有 `"session"`，将来可扩展其他类型。服务端根据 session 类型（Agent/Terminal/...）决定具体的推送行为。

**关闭频道**：
```json
{ "type": "rpc", "id": "3", "method": "channel.close", "params": { "ch": 1 } }
```

**被动关闭**（session 删除、终端退出等）：
```json
{ "type": "event", "event": "channel.closed", "closed_ch": 1, "reason": "session_deleted" }
```

注意：使用 `closed_ch` 而非 `ch`，避免被路由层当作频道消息。此事件是 workspace 级通知。

### 消息格式

同一个 channel 可以同时收发 JSON（Text Frame）和 Binary（Binary Frame）消息，两者共享同一个 channel ID 空间。

**消息不携带序号**。WebSocket（TCP）已保证单次连接内消息有序、不重复、不丢失。双方各自维护接收计数器即可隐式跟踪序号，无需在线上传输。详见"可靠传输层"章节。

消息分为两类：
- **内容消息**：业务数据，计入接收计数
- **控制消息**（`welcome`、`ack`）：传输层元数据，**不计入**接收计数

#### JSON 消息（Text Frame）

**内容消息**：
```json
{
  "ch": 1,                          // 频道 ID，无则为 workspace 级
  "type": "message",                // 消息类型
  ...payload                        // 原有消息体
}
```

- 无 `ch` 字段 或 `ch: 0`：workspace 级消息（RPC 调用/响应/事件）
- `ch: N`（N ≥ 1）：频道消息，路由到对应频道

**控制消息**（不计入接收计数）：
```json
// 连接握手（首条消息）
{"type": "welcome", "resumed": true, "last_seq": 15}

// 确认 + 心跳
{"type": "ack", "ack": 42}
```

**示例 — 消息流**：
```json
// Server→Client（接收方自动计数：#1, #2, #3, #4）
{"type": "rpc_result", "id": "1", "result": {"ch": 1}}      // #1
{"ch": 1, "type": "turn_start"}                              // #2
{"ch": 1, "type": "text_delta", "delta": "Hello"}            // #3
{"ch": 1, "type": "text_delta", "delta": " world"}           // #4

// Client→Server（接收方自动计数：#1, #2）
{"type": "rpc", "id": "1", "method": "channel.open", "params": {...}}  // #1
{"ch": 1, "type": "message", "text": "hello"}                          // #2

// 双向 ACK（不计入计数）
{"type": "ack", "ack": 4}   // Client→Server: 已收到 4 条 server 消息
{"type": "ack", "ack": 2}   // Server→Client: 已收到 2 条 client 消息
```

#### Binary 消息（Binary Frame）

与 JSON 消息共享同一个接收计数序列（per-client，不分 JSON/binary）。二进制帧格式不变——**没有 seq 前缀**：

```
Binary Frame 格式:
┌────────────┬───────────────────────────────┐
│ channel_id │ channel-specific data         │
│ (varint)   │ （由 channel 类型自行定义）      │
└────────────┴───────────────────────────────┘
```

- `channel_id`：与 JSON 消息的 `ch` 字段是同一个值，varint 编码
- 路由层解析 `channel_id`，将剩余字节转发给对应 channel 的 handler
- 接收方在路由前先递增接收计数（与 JSON 消息共享同一个计数器）

**channel_id 变长编码**：

采用高位续传编码（LEB128 / protobuf varint）：
- 每字节低 7 位为数据位，最高位（bit 7）为续传标志
- 最高位 = 0：当前字节是最后一个字节
- 最高位 = 1：后续还有字节

```
ch=1     → 0x01                (1 字节，覆盖 1-127)
ch=127   → 0x7F                (1 字节)
ch=128   → 0x80 0x01           (2 字节，覆盖 128-16383)
ch=300   → 0xAC 0x02           (2 字节)
```

复用最小自然数确保绝大多数情况只需 1 字节。

**示例 — Terminal Session 的 binary 格式**：

Terminal channel 在 channel_id 之后定义自己的 msg_type + payload：

```
┌────────────┬──────────┬───────────────────┐
│ channel_id │ msg_type │ payload           │
│ (varint)   │ 1 byte   │ 剩余 bytes        │
└────────────┴──────────┴───────────────────┘
```

| msg_type | 方向 | 说明 | payload |
|----------|------|------|---------|
| `0x00` | Client→Server | 输入 | data |
| `0x01` | Server→Client | 输出 | data |
| `0x02` | Client→Server | resize | 2B rows + 2B cols |
| `0x03` | Server→Client | scrollback 完成 | 无 |
| `0x04` | Server→Client | 进程退出 | optional 4B exit_code |

其他 channel 类型可以定义完全不同的二进制子协议。

### 可靠传输层

#### 设计前提

WebSocket（TCP）在**单次连接内**已保证：有序传输、不重复、不丢失、明确分包。

但 `ws.send()` 成功只代表数据进入本地 TCP 发送缓冲区，**不代表对方已收到**。断线瞬间，TCP 缓冲区中的数据丢失，双方都不知道最后几条消息是否到达。WiFi/5G 切换导致 IP 变更，TCP 连接必断，WebSocket 无重连能力。

#### 核心思路：隐式计数 + ACK + 发送缓冲

**利用 WebSocket 已有的保证，不重复造轮子**：

1. **不在消息中传 seq**——WebSocket 保证有序不重复，双方各自对收到的内容消息计数即可，计数天然一致
2. **定时发送 ACK**——告知对方"我已收到 N 条消息"，对方据此安全丢弃发送缓冲
3. **发送缓冲保留未确认消息**——断线重连后，根据对方报告的接收计数精确重发

```
发送方:                                    接收方:
 msg → 存入 send_buffer → ws.send()        收到 msg → recv_count++
                                           定时发送 ack(recv_count)
 收到 ack(N) → 丢弃 buffer 前 N 条
```

#### 接收计数规则

每一端维护一个**接收计数器** `recv_count`，规则简单：

- 收到内容消息（JSON 或 Binary）→ `recv_count += 1`
- 收到控制消息（`welcome`、`ack`）→ **不递增**
- JSON 和 Binary 共享同一个计数器
- 计数从 0 开始（首次连接时 `recv_count = 0`，收到第一条内容消息后变为 1）

双方无需协商计数——WebSocket 保证消息有序不重复，所以只要计数规则一致（哪些消息计入、哪些不计入），双方的计数就一定匹配。

#### 确认机制（ACK）

双向累积确认。`ACK(N)` 语义：**"我已收到你发来的 N 条内容消息，你可以安全丢弃前 N 条了"**。

```json
// Client→Server: "我已收到你发来的 42 条内容消息"
{"type": "ack", "ack": 42}

// Server→Client: "我已收到你发来的 15 条内容消息"
{"type": "ack", "ack": 15}
```

发送方收到 ACK(N) 后，从发送缓冲区头部丢弃前 N - peer_ack 条（即新确认的部分）。**这是发送方唯一安全丢弃消息的时机**。

#### 心跳

ACK 同时承担心跳职责，不需要额外的 ping/pong 机制：

| 参数 | 值 | 说明 |
|------|-----|------|
| ACK 间隔 | **5 秒** | 空闲时每 5 秒发一次 ACK（即使 ack 值未变），保活 + 死连接检测 |
| 高吞吐 ACK | 每 **100 条**消息 | 高吞吐场景下更频繁确认，避免 send buffer 积压 |
| 死连接超时 | **15 秒** | 超过 15 秒未收到任何消息（含 ACK）→ 视为连接死亡 |

**任何消息**（内容消息或 ACK）都重置对方的死连接计时器。正常通信时，数据消息本身就充当心跳。只有空闲时才需要独立发送 ACK。

#### 发送缓冲区（Send Buffer）

每一端维护一个 send buffer（有序队列），保存已发送但**未被对方 ACK 确认**的消息：

```python
class SendBuffer:
    _buffer: deque[tuple[str, bytes | dict]]  # (frame_type, data)
    _total_sent: int = 0    # 已发送的总消息数（= 对方的理论 recv_count）
    _peer_ack: int = 0      # 对方已确认收到的消息数

    # 缓冲区限制
    MAX_MESSAGES = 1000
    MAX_BYTES = 1 * 1024 * 1024  # 1MB
```

**关键属性**：
- `_buffer[0]` 对应对方应收到的第 `_peer_ack + 1` 条消息
- `_buffer[-1]` 对应第 `_total_sent` 条消息
- `len(_buffer) == _total_sent - _peer_ack`

**操作**：

```python
def send(self, frame_type, data):
    """发送消息：存入 buffer + 写入 WebSocket"""
    self._buffer.append((frame_type, data))
    self._total_sent += 1
    if self._buffer_overflow():
        raise BufferOverflow()  # 调用方处理：expire client
    if self.ws is not None:
        self._ws_send(frame_type, data)  # 实际写入 WebSocket
    # 断线时：只存 buffer 不发送，重连后重发

def on_ack(self, n: int):
    """收到对方 ACK(n)：安全丢弃前 n 条"""
    discard = n - self._peer_ack
    for _ in range(discard):
        self._buffer.popleft()
    self._peer_ack = n

def replay(self, last_peer_count: int):
    """重连后：从对方实际收到的位置开始重发"""
    skip = last_peer_count - self._peer_ack  # buffer 中对方已收到的部分
    for i, (frame_type, data) in enumerate(self._buffer):
        if i >= skip:
            self._ws_send(frame_type, data)
```

**为什么需要双向 send buffer**：

| 方向 | 谁的 send buffer | 保存什么 | 典型大小 |
|------|-----------------|---------|---------|
| Server→Client | 服务端 | agent 流事件、终端输出、RPC 响应 | 较大（终端高吞吐） |
| Client→Server | 客户端（浏览器） | 用户消息、RPC 请求、终端输入 | 较小（用户输入低频） |

**断线时 send buffer 的两类消息**：
1. **已写入 WebSocket 但未被 ACK**——对方可能收到了（TCP 已送达），也可能没收到（在 TCP 缓冲区中丢失）
2. **断线后新产生的**——存入 buffer 但未写入 WebSocket

重连时对方报告实际收到的消息数 `last_seq`，发送方精确跳过已收到的部分，从断点开始重发。

#### 断线重连协议

```
┌─────────┐                                    ┌─────────┐
│ Client  │                                    │ Server  │
└────┬────┘                                    └────┬────┘
     │            正常通信                          │
     │ msg ─────────────────────────────────────►  │ server recv: 1
     │  ◄─────────────────────────────────── msg  │ client recv: 1
     │  ◄─────────────────────────────────── msg  │ client recv: 2
     │ ack:2 ──────────────────────────────────►  │ (server 丢弃 buffer 前 2 条)
     │  ◄─────────────────────────────────── msg  │ client recv: 3
     │  ◄──── msg (TCP 缓冲区中，未到达 client)    │ client recv: 仍=3
     │                                              │
     │         ~~~ 连接断开 (WiFi→5G) ~~~            │
     │                                              │
     │  Client: recv_count=3                        │
     │  Server: recv_count=1, peer_ack=2            │
     │                                              │
     │  Server: 进入 buffering                      │
     │  ├── send buffer: 2 条未 ACK + 新产生的       │
     │  └── 超时 30s 或 buffer 溢出 → expired       │
     │                                              │
     │  Client: 重连                                │
     │  ?client_id=xxx&last_seq=3                   │
     ├─────────────────────────────────────────────►│
     │                                              │ 匹配 client
     │                                              │ last_seq=3 在 buffer 范围内 ✓
     │                                              │
     │  welcome(resumed:true, last_seq:1)           │
     │◄─────────────────────────────────────────────┤
     │                                              │
     │  重发 buffer 中第 3 条之后的消息               │
     │◄─────────────────────────────────────────────┤
     │                                              │
     │  Client 检查: server last_seq=1              │
     │  Client send buffer 中第 1 条之后 → 无需重发  │
     │                                              │
     │            恢复正常通信                        │
     │◄────────────────────────────────────────────►│
```

**重连 URL**：
```
ws://host/ws/workspace/{workspace_id}?client_id=xxx&last_seq=3
```
- `client_id`：客户端标识（客户端用 `crypto.randomUUID()` 生成，**存内存变量**，每次页面加载重新生成。刷新页面 = 完整重连，与当前行为一致）
- `last_seq`：客户端实际收到的内容消息总数（首次连接时不携带此参数）

**服务端处理流程**：

1. 查找 `client_id` 对应的 Client 记录
2. **找到且 `buffering` 状态**：
   - 检查 `last_seq` 是否在 send buffer 可覆盖范围内：`last_seq >= peer_ack`（即 buffer 中保留了客户端缺失的所有消息）
   - 如果可覆盖 → **恢复成功**：
     - 替换 WebSocket 引用，取消超时计时器
     - 发送 `{"type": "welcome", "resumed": true, "last_seq": M}`（M = 服务端的 recv_count）
     - 调用 `send_buffer.replay(last_seq)` 重发缺失消息
     - 客户端收到 welcome 后，调用自己的 `send_buffer.replay(M)` 重发
     - 双方恢复正常通信，计数继续递增
   - 如果不可覆盖 → `{"type": "welcome", "resumed": false}`
3. **找不到或已 `expired`** → `{"type": "welcome", "resumed": false}`

**完整重连（`resumed: false`）**：
- 双方重置计数器和 send buffer
- 客户端清空所有 channel 状态
- 重新 `channel.open` 所有需要的频道
- 各面板重新加载数据（等效刷新页面）

**Client 状态机**：

```
                       ┌──────────┐
           新连接 ────►│connected │◄──── 重连成功 (resumed)
                       └────┬─────┘
                            │ 15s 无消息 / WebSocket 断开
                            ▼
                       ┌──────────┐
                       │buffering │
                       └────┬─────┘
                            │
                 ┌──────────┼──────────┐
                 │          │          │
            超时 30s    buffer 溢出  last_seq 不可覆盖
                 │          │          │
                 ▼          ▼          ▼
               ┌─────────────────────────┐
               │        expired          │
               │  关闭所有 Channel        │
               │  回收 Channel ID        │
               │  清除 Client 记录       │
               └─────────────────────────┘
```

### 线程安全

可靠传输层统一处理线程安全，业务代码无需关心：

| 组件 | 线程安全机制 | 说明 |
|------|------------|------|
| `channel.enqueue_*()` | `asyncio.Queue.put_nowait()` | Python GIL 保证线程安全，任何线程可调用 |
| send buffer 操作 | send worker 单线程 | 在 event loop 中顺序执行，无竞争 |
| ACK 处理 | event loop 线程 | 收到 ACK 消息时在 event loop 中处理 |
| ChannelManager 映射表 | `threading.Lock` | event loop 线程和 TerminalManager 读线程并发访问 |

TerminalManager 读线程直接调用 `channel.enqueue_binary()`，无需 `run_coroutine_threadsafe`。消息入队后，存入 send buffer 和实际发送都在 event loop 的 send worker 中完成。

### 后端架构变更

#### 路由层（`routes.py`）

**保留**：`/ws/workspace/{workspace_id}` 端点（现有）
**删除**：`/ws/session/{session_id}`、`/ws/terminal/{term_id}`
**不变**：`/ws/app`（workspace 外的独立端点）

在 `websocket_workspace` handler 中：
- 接受连接后检查 `client_id` 和 `last_seq` query params
- 如果匹配到 buffering Client 且 last_seq 可恢复：替换 ws，重发，推送 `welcome(resumed=true, last_seq=M)`
- 如果是新连接：创建 Client，推送 `welcome(resumed=false)`
- Text Frame → 解析 JSON：
  - `type: "ack"`：更新 peer_ack，清理 send buffer
  - 内容消息：递增 recv_count，按 `ch` 路由
- Binary Frame → 解析 varint `channel_id`，递增 recv_count，转发剩余字节给对应 channel handler
- 连接断开 → Client 进入 `buffering` 状态
- 启动 per-client ACK 定时器和死连接检测定时器

#### Client

```python
class Client:
    ws: WebSocket | None           # 断线时置为 None
    workspace_id: str
    client_id: str                 # 唯一标识（客户端生成 UUID）
    state: Literal["connected", "buffering", "expired"]

    # 可靠传输 — 发送方向 (Server→Client)
    _send_queue: asyncio.Queue     # 统一发送队列（所有 channel 入队到此）
    _send_buffer: deque            # [(frame_type, data)] 未被 ACK 的消息
    _total_sent: int = 0           # 已发送的总消息数
    _peer_ack: int = 0             # Client 已 ACK 确认的消息数

    # 可靠传输 — 接收方向 (Client→Server)
    _recv_count: int = 0           # 已收到的 client 内容消息数

    # 定时器
    _ack_timer: asyncio.TimerHandle | None      # 5s ACK/心跳
    _dead_timer: asyncio.TimerHandle | None     # 15s 死连接检测
    _expire_timer: asyncio.TimerHandle | None   # 30s 缓冲超时

    # 缓冲区限制
    BUFFER_TIMEOUT = 30            # 缓冲超期（秒）
    BUFFER_MAX_MESSAGES = 1000
    BUFFER_MAX_BYTES = 1 * 1024 * 1024  # 1MB

    def send_json(self, data: dict):
        """workspace 级消息直接入队（RPC 响应、事件等）"""
        self._send_queue.put_nowait(("json", data))
```

Workspace 级消息（RPC 结果、事件推送等）通过 `client.send_json()` 直接入队，与 Channel 消息共享同一个发送队列和 send buffer。

**Send Worker**（每个 Client 一个）：

```python
async def _send_worker(self):
    """从统一队列取出消息，存入 send buffer，写入 WebSocket"""
    while not self._closed:
        frame_type, data = await self._send_queue.get()
        try:
            self._send_buffer.send(frame_type, data)
        except BufferOverflow:
            self._expire()
            return
```

#### Channel

Channel 是业务层与传输层的桥梁。业务代码只调用 `enqueue_*`，不关心计数、ACK、断线：

```python
class Channel:
    ch: int                    # 全局唯一 ID
    client: Client             # 所属 client
    target: str                # 目标类型（如 "session"）
    session: Session | None    # 绑定的 session，可为空

    def enqueue_json(self, data: dict):
        """线程安全：从任何线程调用。消息进入 Client 统一发送队列"""
        self.client._send_queue.put_nowait(("json", {"ch": self.ch, **data}))

    def enqueue_binary(self, data: bytes):
        """线程安全：从任何线程调用"""
        self.client._send_queue.put_nowait(("binary", (self.ch, data)))
```

Channel 不再有自己的 send queue 和 send worker——统一由 Client 的 send worker 管理，保证消息在所有 channel 间全局有序。

#### ChannelManager（服务端全局）

管理所有 channel 的分配、路由和生命周期，线程安全：

```python
class ChannelManager:
    _lock: threading.Lock
    _channels: dict[int, Channel]
    _session_channels: dict[str, set[int]]

    def open(self, client: Client, target: str, **kwargs) -> Channel:
        """打开频道，分配全局唯一 channel ID（复用最小可用自然数）"""

    def close(self, ch: int) -> None:
        """关闭频道，回收 channel ID"""

    def get_channel(self, ch: int) -> Channel | None:
        """按 ch 查找 channel"""

    def get_channels_for_session(self, session_id: str) -> list[Channel]:
        """线程安全快照：获取连接到指定 session 的所有 channel"""

    def close_all_for_client(self, client: Client) -> None:
        """client 过期时关闭其所有频道"""
```

#### Session 与 Channel 的关系

Session 不需要感知 Client、计数、ACK 或线程安全细节：

```python
# Session 推送事件时（任何线程）
channels = channel_manager.get_channels_for_session(session_id)
for channel in channels:
    channel.enqueue_json({"type": "text_delta", "delta": "..."})

# Terminal 推送输出时（读线程，直接调用，无需 run_coroutine_threadsafe）
channels = channel_manager.get_channels_for_session(term_session_id)
for channel in channels:
    channel.enqueue_binary(bytes([0x01]) + output_data)
```

#### AgentBridge 适配

`AgentBridge` 的 `broadcast_fn` 改为通过 ChannelManager 查找 channel：

```python
# 之前: broadcast_fn(session_id, {"type": "text_delta", ...})
# 之后: 通过 channel_manager.get_channels_for_session(session_id) 获取所有 channel，
#        逐一 enqueue（每个 channel 有自己的 ch，传输层自动管理缓冲和 ACK）
```

#### TerminalManager 适配

```python
# 之前（读线程）: run_coroutine_threadsafe(on_output(payload), loop) — 每个客户端单独回调
# 之后（读线程）: channel.enqueue_binary(data) — 线程安全，无需跨线程调度
```

### 前端架构变更

#### WorkspaceRpc 增强

扩展 `WorkspaceRpc` 支持频道管理、binary frame 和可靠传输：

```typescript
class WorkspaceRpc {
  // --- 连接标识 ---
  clientId: string;          // crypto.randomUUID(), 内存变量

  // --- 可靠传输 ---
  private recvCount = 0;     // 收到的 server 内容消息数（= 重连时的 last_seq）
  private sendBuffer: Array<{type: string, data: any}> = [];
  private totalSent = 0;     // 已发送的内容消息数
  private peerAck = 0;       // Server 已 ACK 确认的消息数

  // --- 现有 RPC 功能不变 ---
  call<T>(method: string, params?: object): Promise<T>;
  on(event: string, handler: Function): () => void;

  // --- 频道管理 ---
  async openChannel(target: string, params: object): Promise<number>;
  async closeChannel(ch: number): Promise<void>;

  // --- 频道消息 ---
  sendToChannel(ch: number, data: object): void;
  sendBinaryToChannel(ch: number, data: ArrayBuffer): void;
  onChannel(ch: number, handler: (msg: object) => void): () => void;
  onBinaryChannel(ch: number, handler: (data: ArrayBuffer) => void): () => void;
}
```

**发送流程**（客户端）：
1. `sendToChannel(ch, data)` → 存入 `sendBuffer` → `totalSent++` → 写入 WebSocket
2. 收到 Server ACK(N) → 从 `sendBuffer` 头部丢弃前 N - peerAck 条

**接收流程**（客户端）：
1. 收到 Server 内容消息 → `recvCount++` → 路由到对应 channel handler
2. 定时（5 秒）或累计 100 条 → 发送 `{"type": "ack", "ack": recvCount}`

**重连流程**：
1. WebSocket 断开 → 停止 ACK 定时器
2. 重连时携带 `client_id` + `last_seq`（= 当前 `recvCount`）
3. 收到 `welcome(resumed: true, last_seq: M)`：
   - 重发 `sendBuffer` 中 Server 未收到的部分（跳过前 M - peerAck 条）
   - 恢复 ACK 定时器
4. 收到 `welcome(resumed: false)`：
   - 清空 `sendBuffer`、重置所有计数器
   - 清空所有 channel 状态
   - 通知各面板重新初始化（等效刷新页面）

#### AgentPanel 适配

从独立 `ReconnectingWebSocket` 改为使用频道：

```typescript
// 之前:
const ws = new ReconnectingWebSocket(`/ws/session/${sessionId}`);
ws.onMessage = (msg) => handleEvent(msg);
ws.send({ type: "message", text });

// 之后: openChannel 后自动接收事件
const ch = await rpc.openChannel("session", { session_id: sessionId });
rpc.onChannel(ch, (msg) => handleEvent(msg));
rpc.sendToChannel(ch, { type: "message", text });
// 不需要时:
await rpc.closeChannel(ch);
```

#### TerminalPanel 适配

从独立 binary WebSocket 改为使用频道：

```typescript
// 之前: 独立 WebSocket 连接
const ws = new WebSocket(`ws://.../ws/terminal/${termId}`);
ws.binaryType = "arraybuffer";

// 之后: 与 Agent Session 一样的 openChannel 接口
const ch = await rpc.openChannel("session", { session_id: termSessionId });

// binary: PTY I/O
rpc.sendBinaryToChannel(ch, buildTerminalInput(inputData));  // 0x00 + data
rpc.onBinaryChannel(ch, (data) => {
  const { msgType, payload } = parseTerminalFrame(data);
  handleTerminalData(msgType, payload);
});
```

## 关键参考

### 源码 — 后端
- `src/mutbot/web/routes.py` — WebSocket 端点定义（session: L910-1010, terminal: L1026-1171, workspace: L1286-1347, app: L1232-1280）
- `src/mutbot/web/connection.py` — `ConnectionManager`，按 session_id 跟踪连接
- `src/mutbot/web/rpc.py` — `RpcDispatcher`，RPC 方法注册和分发
- `src/mutbot/web/agent_bridge.py` — `AgentBridge`，agent 事件广播
- `src/mutbot/runtime/terminal.py` — `TerminalManager`，PTY 管理和多客户端回调
- `src/mutbot/session.py` — `Session` 基类，`TerminalSession` 是其子类

### 源码 — 前端
- `frontend/src/lib/websocket.ts` — `ReconnectingWebSocket` 基础类
- `frontend/src/lib/workspace-rpc.ts` — `WorkspaceRpc` JSON-RPC 客户端
- `frontend/src/lib/app-rpc.ts` — `AppRpc` 引导阶段客户端
- `frontend/src/lib/connection.ts` — WebSocket 端点计算
- `frontend/src/panels/TerminalPanel.tsx` — 终端面板，binary WebSocket + xterm.js
- `frontend/src/panels/AgentPanel.tsx` — Agent 面板，session WebSocket + 流事件处理

### 调研结论
- WebSocket/TCP 无法跨 IP 保持连接，WiFi/5G 切换导致连接必断
- WebSocket 无内置重连、session 恢复或消息回放能力
- `ws.send()` 成功 ≠ 对方收到，断线时 TCP send buffer 数据丢失
- 浏览器端无法发送 WebSocket Ping，只能用应用层心跳

## 实施步骤清单

### Phase 1: 后端可靠传输层 [✅ 已完成]

核心基础设施，所有后续工作依赖此阶段。

- [x] **Task 1.1**: 实现 `SendBuffer` 类
  - [x] `append(frame_type, data)` — 存入 buffer，溢出前检查
  - [x] `on_ack(n)` — 丢弃已确认消息
  - [x] `replay(last_peer_count)` — 重连后重发
  - [x] buffer 溢出检测（MAX_MESSAGES=1000, MAX_BYTES=1MB）
  - [x] 单元测试：正常发送、ACK 清理、replay 精确重发、溢出抛异常、JSON/Binary 混合计数
  - 状态：✅ 已完成

- [x] **Task 1.2**: 实现 `Client` 类
  - [x] 状态机：connected → buffering → expired
  - [x] `_send_queue`（asyncio.Queue）+ `_send_worker` 协程
  - [x] `_recv_count` 接收计数（JSON + Binary 共享）
  - [x] `send_json(data)` — workspace 级消息入队
  - [x] ACK 定时器（5s 间隔 / 100 条高吞吐）
  - [x] 死连接检测（15s 无消息 → 断开）
  - [x] buffering 超时（30s → expired）
  - [x] lazy event loop：`_get_loop()` 延迟获取
  - [x] 单元测试：状态转换、send worker 顺序性、ACK 发送、死连接检测、buffering → expired
  - 状态：✅ 已完成

- [x] **Task 1.3**: 实现 varint 编解码工具函数
  - [x] `encode_varint(n) -> bytes`
  - [x] `decode_varint(data) -> (value, bytes_consumed)`
  - [x] 单元测试：1-127 单字节、128+ 多字节、边界值、memoryview
  - 状态：✅ 已完成

### Phase 2: 后端 Channel 层 [✅ 已完成]

在可靠传输层之上构建多路复用。

- [x] **Task 2.1**: 实现 `Channel` 类
  - [x] `enqueue_json(data)` — 注入 `ch` 字段后入 Client 发送队列
  - [x] `enqueue_binary(data)` — 加 varint channel_id 前缀后入队
  - [x] 线程安全（put_nowait）
  - [x] 单元测试覆盖线程安全场景
  - 状态：✅ 已完成

- [x] **Task 2.2**: 实现 `ChannelManager` 类（全局单例）
  - [x] `open(client, target, **kwargs) -> Channel` — 分配最小可用 ID（min-heap 回收）
  - [x] `close(ch)` — 回收 ID
  - [x] `get_channel(ch) -> Channel | None`
  - [x] `get_channels_for_session(session_id) -> list[Channel]` — 线程安全快照
  - [x] `close_all_for_client(client)` — client 过期时清理
  - [x] threading.Lock 保护映射表
  - [x] 单元测试：ID 分配回收、多 client 隔离、线程安全并发测试
  - 状态：✅ 已完成

### Phase 3: 后端 WebSocket 路由重构 [✅ 已完成]

改造 workspace WebSocket handler，集成可靠传输层和 Channel 路由。

- [x] **Task 3.1**: 重构 `websocket_workspace` handler
  - [x] 解析 `client_id` 和 `last_seq` query params
  - [x] 新连接：创建 Client，发送 `welcome(resumed=false)`
  - [x] 重连：匹配 Client，验证 last_seq，发送 `welcome(resumed=true, last_seq=M)`，replay
  - [x] Text Frame 路由：`ack` → 更新 peer_ack；内容消息 → recv_count++ → 按 `ch` 分发
  - [x] Binary Frame 路由：解析 varint channel_id → recv_count++ → 转发给 channel handler
  - [x] 连接断开 → Client 进入 buffering
  - [x] 注册 `channel.open` 和 `channel.close` RPC 方法
  - 状态：✅ 已完成

- [x] **Task 3.2**: 适配 AgentBridge 广播机制
  - [x] `broadcast_fn` 改为通过 `channel_manager.get_channels_for_session()` 分发
  - [x] 无 channel 连接时的行为：消息丢弃（agent 事件是瞬时的，不需要队列）
  - [x] 验证所有 15+ 处 broadcast 调用点行为正确
  - 状态：✅ 已完成

- [x] **Task 3.3**: 适配 TerminalManager
  - [x] 读线程回调改为 `channel.enqueue_binary()`（同步回调，线程安全）
  - [x] attach/detach 改为通过 ChannelManager 管理
  - [x] channel.open 时自动 attach + scrollback replay
  - [x] channel.close 时自动 detach
  - [x] OutputCallback 类型更新支持同步和异步回调
  - 状态：✅ 已完成

- [x] **Task 3.4**: 适配 ConnectionManager / 删除旧端点
  - [x] 删除 `/ws/session/{session_id}` handler
  - [x] 删除 `/ws/terminal/{term_id}` handler
  - [x] 删除相关 pending event 队列逻辑
  - [x] 更新 server.py lifespan：注入 ChannelManager
  - [x] 保留 `workspace_connection_manager` 用于 workspace 级广播
  - 状态：✅ 已完成

- [x] **Task 3.5**: `channel.closed` 被动关闭事件
  - [x] `_close_channels_for_session(session_id, reason)` 工具函数
  - [x] session 删除时 → 关闭关联 channel → 推送 `channel.closed` 事件
  - [x] session 重启时 → 关闭关联 channel（reason: `session_restarted`）
  - [x] client expired 时 → 关闭所有 channel（无需推送，ws 已断）
  - [x] 终端进程退出 → 不关闭 channel（0x04 已通过 channel 发送）
  - [x] 单元测试：session 删除、session 重启、client expired 3 个场景
  - 状态：✅ 已完成

### Phase 4: 前端可靠传输层 [✅ 已完成]

在 WorkspaceRpc 中实现客户端侧的可靠传输。

Tasks 4.1-4.4 合并实施：完全重写 `workspace-rpc.ts`，从 `ReconnectingWebSocket` 封装改为直接管理 `WebSocket`，内建可靠传输、Binary Frame 支持和 Channel 多路复用。

- [x] **Task 4.1**: WorkspaceRpc 增加可靠传输
  - [x] `clientId = crypto.randomUUID()`（内存变量，页面加载时生成）
  - [x] `recvCount` 接收计数器（JSON + Binary 共享）
  - [x] `sendBuffer` + `totalSent` + `peerAck`
  - [x] 发送时存入 sendBuffer → totalSent++ → ws.send()
  - [x] 收到 server ACK → 清理 sendBuffer（`onPeerAck`）
  - [x] ACK 定时器（5s / 100 条批量触发）
  - [x] 重连 URL 携带 `client_id` + `last_seq`
  - 状态：✅ 已完成

- [x] **Task 4.2**: WorkspaceRpc 增加 welcome 处理和重连逻辑
  - [x] `welcome(resumed=true, last_seq=M)` → `replayFromBuffer` 重发 server 未收到的消息
  - [x] `welcome(resumed=false)` → `resetState` 重置所有状态、清空 channel、通知面板
  - [x] 连接断开 → 停止 ACK 定时器，指数退避重连（1s → 30s，最多 10 次）
  - [x] `onOpen` 回调延迟到 welcome 消息确认后触发
  - 状态：✅ 已完成

- [x] **Task 4.3**: WorkspaceRpc 增加 Binary Frame 支持
  - [x] WebSocket `binaryType = "arraybuffer"`
  - [x] `sendBinaryToChannel(ch, data)` — varint encode ch + data → ws.send(binary)
  - [x] 收到 Binary Frame → `handleBinaryFrame` 解析 varint ch → 路由到 channel binary handler
  - [x] Binary 消息计入 recvCount
  - [x] `encodeVarint` / `decodeVarint` 工具函数（LEB128）
  - 状态：✅ 已完成

- [x] **Task 4.4**: WorkspaceRpc 增加 Channel 管理 API
  - [x] `openChannel(target, params) → Promise<number>` — 调用 `channel.open` RPC
  - [x] `closeChannel(ch)` — 调用 `channel.close` RPC，清理本地 handler
  - [x] `sendToChannel(ch, data)` — JSON 消息注入 `ch` 字段
  - [x] `sendBinaryToChannel(ch, data)` — varint 前缀 + payload
  - [x] `onChannel(ch, handler)` / `onBinaryChannel(ch, handler)` — 注册 channel 消息回调
  - [x] `onChannelClosed(handler)` — 监听 `channel.closed` 被动关闭事件
  - [x] `channel.closed` 事件 → 清理本地 channel handler → 通知面板
  - [x] `resetState` 时通知所有 channel `connection_reset`
  - 状态：✅ 已完成

注：前端无测试框架（无 vitest/jest），单元测试暂未实施。

### Phase 5: 前端面板适配 [✅ 已完成]

将各面板从独立 WebSocket 迁移到 Channel 模式。

- [x] **Task 5.1**: AgentPanel 适配
  - [x] 移除独立 `ReconnectingWebSocket` 连接
  - [x] 改为 `rpc.openChannel("session", { session_id })` 打开频道
  - [x] `rpc.onChannel(ch, handleEvent)` 接收流事件
  - [x] `rpc.sendToChannel(ch, { type: "message", text })` 发送消息
  - [x] 面板卸载时 `rpc.closeChannel(ch)`
  - [x] remote-log 重写：`setLogChannel(rpc, ch, sessionId)` 替代 `setLogSocket`
  - [x] `onChannelClosed` 监听被动关闭，更新连接状态
  - 状态：✅ 已完成

- [x] **Task 5.2**: TerminalPanel 适配
  - [x] 移除独立 binary WebSocket 连接和自建重连逻辑
  - [x] 改为 `rpc.openChannel("session", { session_id: termSessionId })`
  - [x] `rpc.onBinaryChannel(ch, handleBinaryData)` 接收终端输出
  - [x] `rpc.sendBinaryToChannel(ch, data)` 发送输入
  - [x] resize 通过 binary channel 发送（0x02 + rows + cols）
  - [x] scrollback replay + input muting 逻辑保持不变（由 binary msg_type 驱动）
  - [x] 进程退出（0x04）处理保持不变
  - [x] `onChannelClosed` 监听被动关闭
  - [x] paste 功能改为通过 channel 发送
  - 状态：✅ 已完成

- [x] **Task 5.3**: 清理前端旧代码
  - [x] `ReconnectingWebSocket` 保留（仍被 AppRpc `/ws/app` 使用）
  - [x] 所有 `/ws/session/` 和 `/ws/terminal/` 引用已移除
  - [x] `connection.ts` 保留（`getWsUrl` 仍被 workspace-rpc、app-rpc、LogPanel 使用）
  - [x] `remote-log.ts` 重写为 channel-based（不再依赖 `ReconnectingWebSocket`）
  - [x] AppRpc（`/ws/app`）不受影响
  - 状态：✅ 已完成

### Phase 6: Workspace 级消息走 send buffer [✅ 已完成]

**问题**：workspace 级消息（`config_changed`、`session_created`、`session_updated` 等事件 + `queue_event` 挂起事件）通过 `workspace_connection_manager.broadcast()` → `ws.send_json()` 直接发送，绕过了 `Client.send_json()` 和 send buffer。客户端正确计入 `recvCount`，但服务端 `total_sent` 没有增加，导致 ACK 值不对齐 → send buffer 无法清理 → dead timeout → 所有 channel 断开。

**修复**：所有发给 workspace WebSocket 客户端的消息都必须经过 `Client.send_json()`。

需要改造的路径：

| 位置 | 当前实现 | 修复 |
|------|---------|------|
| `routes.py:1140` — `config_changed` | `await websocket.send_json(...)` | `client.send_json(...)` |
| `routes.py:1146` — `RpcContext.broadcast` | `workspace_connection_manager.broadcast()` | 改为遍历 workspace 的所有 Client，调用 `client.send_json()` |
| `routes.py:1067` — `sm._broadcast_fn` | `workspace_connection_manager.broadcast()` | 同上 |
| `routes.py:1269` — `session_updated` 广播 | `workspace_connection_manager.broadcast()` | 同上 |
| `routes.py:117` — `queue_event` | `workspace_connection_manager.queue_event()` | 新连接 flush 时通过 `client.send_json()` |
| RPC 响应（`rpc.py` dispatch 返回值） | handler 中 `client.send_json(response)` | ✅ 已走 send buffer |

同时需要一个 `_clients` 按 workspace_id 索引的结构（或遍历 `_clients` 字典按 `workspace_id` 过滤），用于 workspace 级广播。`workspace_connection_manager` 在 workspace WebSocket 上下文中可以完全移除。

- [x] **Task 6.1**: 实现 workspace 级广播通过 Client.send_json()
  - [x] 添加 `_workspace_clients: dict[str, set[Client]]` 索引
  - [x] 新建 `_broadcast_to_workspace(workspace_id, data, exclude_client=None)` 工具函数
  - [x] `RpcContext.broadcast` 改为调用 `_broadcast_to_workspace`
  - [x] `sm._broadcast_fn` 改为调用 `_broadcast_to_workspace`
  - [x] `config_changed` 改为 `client.send_json()`
  - [x] `session_updated`（`routes.py:1269`）改为 `_broadcast_to_workspace`
  - [x] `queue_event` flush 改为通过 `client.send_json()`
  - 状态：✅ 已完成

- [x] **Task 6.2**: RPC 响应确认走 send buffer
  - [x] 确认 `websocket_workspace` handler 中 RPC dispatch 返回值已通过 `client.send_json(response)` 发送
  - 状态：✅ 已完成

- [x] **Task 6.3**: 清理 workspace_connection_manager 在 workspace WS 中的使用
  - [x] workspace WS handler 不再往 `workspace_connection_manager._connections` 注册
  - [x] `workspace_connection_manager` 仅保留 `queue_event` + `_pending_events`（setup 流程使用）
  - [x] `_on_client_expire` 回调同步清理 `_workspace_clients`
  - 状态：✅ 已完成

- [x] **Task 6.4**: 修复 Terminal scrollback 在 channel.open 结果前发送
  - **问题**：`channel.open` handler 中 `_attach_terminal_channel` 的 `enqueue_binary`（scrollback + 0x03）在 `rpc_result` 之前入队，客户端收到 binary 时还不知道 ch=N，消息被丢弃，`connected` 永远不变 true，Terminal 一直显示 "Connecting"
  - **修复**：`_attach_terminal_channel` 改为同步函数，通过 `RpcContext._post_send` 延后到 `rpc_result` 入队之后执行
  - [x] `_attach_terminal_channel` 从 `async def` 改为 `def`（内部全是同步操作）
  - [x] `RpcContext` 新增 `_post_send: Callable | None` 字段
  - [x] 消息循环中 `dispatch` 返回后先 `client.send_json(response)` 再执行 `_post_send`
  - [x] `handle_channel_open` 中 terminal attach 注册为 `_post_send` 回调
  - 状态：✅ 已完成

### Phase 6b: 页面刷新后面板无法恢复连接 [✅ 已完成]

- [x] **Task 6b.1**: 修复 AgentPanel 刷新后无法打开 channel
  - **问题**：`App.tsx` 中 `setRpc(wsRpc)` 在 WorkspaceRpc 构造后立即调用（WS 尚未连接），AgentPanel 的 `useEffect([sessionId, rpc])` 立即触发 `rpc.openChannel()`。此时 WS 未 OPEN，RPC 请求被缓冲到 sendBuffer。随后 WS 连接建立 → welcome(resumed=false) → `resetState()` 拒绝所有 pending RPC（包括 channel.open），AgentPanel 的 `.catch` 触发 → `setConnected(false)`，永远卡在断开状态
  - **修复**：将 `setRpc(wsRpc)` 从构造后立即调用移到 `onOpen` 回调中，确保面板仅在 WS 连接就绪后才尝试打开 channel
  - [x] `App.tsx`: `setRpc(wsRpc)` 移到 `onOpen` 回调中
  - 状态：✅ 已完成

- [x] **Task 6b.2**: 修复断线重连后面板不恢复
  - **问题**：WS 断线重连时 `onOpenCb` 再次调用 `setRpc(wsRpc)`，但同一引用不触发 React re-render，面板的 `useEffect([sessionId, rpc])` 不重新执行
  - **修复**：`onClose` 回调中加 `setRpc(null)`，制造 `wsRpc→null→wsRpc` 状态变化
  - [x] `App.tsx`: `onClose` 中加 `setRpc(null)`
  - 状态：✅ 已完成

- [x] **Task 6b.3**: 清理 `workspace_connection_manager` 残留
  - **问题**：`workspace_connection_manager` (ConnectionManager) 只剩 `queue_event` 和 `_pending_events` 被使用，直接访问私有属性
  - **修复**：用模块级 dict + 函数替代，删除 `workspace_connection_manager` 和 `ConnectionManager` import
  - [x] `routes.py`: `_workspace_pending_events` dict + `queue_workspace_event()` + `_pop_pending_events()`
  - [x] `routes.py`: 删除 `workspace_connection_manager`
  - [x] `server.py`: config 变更广播改用 `_broadcast_to_all_workspaces()`
  - 状态：✅ 已完成

### Phase 7: 集成测试与验收 [待开始]

- [ ] **Task 7.1**: 端到端功能验证
  - [x] Agent 对话：发送消息 → 流式响应 → tool 执行 → 完成
  - [x] Terminal：输入输出 → resize → scrollback replay → 进程退出
  - [x] 多 session 并发：同时打开多个 Agent + Terminal
  - [x] 多标签页：两个浏览器标签页打开同一 workspace
  - 状态：⏸️ 待开始

- [ ] **Task 7.2**: 断线重连验证
  - [ ] 短暂断线（< 30s）→ resumed=true，消息不丢失
  - [ ] 长时间断线（> 30s）→ resumed=false，面板重新初始化
  - [ ] 断线期间 agent 继续产生事件 → 重连后正确 replay
  - [ ] 断线期间用户在终端输入 → 重连后 sendBuffer 重发
  - 状态：⏸️ 待开始

## 测试验证
（实施阶段填写）
