# 统一 WebSocket 传输层设计

**日期**：2026-03-07

## 概述

MutBot 前后端通过两条 WebSocket 连接通信：

| 端点 | 用途 | 协议 |
|------|------|------|
| `/ws/app` | 全局操作（引导阶段） | JSON-RPC |
| `/ws/workspace/{workspace_id}` | Workspace 内所有通信 | JSON-RPC + Channel 多路复用 + 可靠传输 |

两条连接分工明确：`/ws/app` 在用户选择 Workspace 之前使用，`/ws/workspace/{id}` 在进入 Workspace 后使用。

## App WebSocket

`/ws/app` 是全局级连接，用于 Workspace 选择前的引导阶段操作：工作区列表查询、创建工作区、目录浏览等。

- 纯 JSON-RPC 协议，消息格式与 Workspace 级 RPC 一致
- 连接后立即推送 `welcome` 事件，包含应用状态（版本号、是否需要初始设置）
- 无 Channel、无可靠传输、无 Send Buffer——引导阶段操作简单且可重试
- 使用 `ReconnectingWebSocket` 自动重连

## Workspace WebSocket

Workspace 内所有通信统一到 `/ws/workspace/{workspace_id}` 一条连接上，通过频道（Channel）多路复用。Agent Session、Terminal Session 等不同类型的通信共享同一条连接，由传输层统一管理可靠传输、心跳和断线重连。

## 核心概念

### Client — 连接级身份

每个 WebSocket 连接代表一个 Client。同一用户可以在多个浏览器标签页中打开同一个 Workspace，每个标签页是一个独立的 Client。

Client 标识由客户端生成（`crypto.randomUUID()`），存内存变量，页面刷新后重新生成。刷新页面等同于完整重连。

### Channel — 多路复用单元

Channel 替代了独立的 WebSocket 连接。每个 Channel 有一个服务端全局唯一的正整数 ID（从 1 开始），关闭后 ID 可被复用。

- Channel 大多数情况绑定 Session，但 Session 可以为空
- 一个 Session 可以有多个 Channel 连接（不同 Client，或同一 Client 的不同用途）
- Channel 同时支持 JSON 和 Binary 消息，可混用
- 打开 Channel 即开始接收目标的事件推送，Agent 和 Terminal 行为一致

```
Workspace
├── Client A (浏览器标签页 1)
│   ├── ch=1 → session "abc" (Agent)
│   ├── ch=2 → session "term1" (Terminal)
│   └── ch=3 → session "def" (Agent)
├── Client B (浏览器标签页 2)
│   ├── ch=4 → session "abc" (Agent)     ← 同一 session，不同 ch
│   └── ch=5 → session "term2" (Terminal)
```

### 消息分类

消息分为两类：

- **内容消息**：业务数据，计入接收计数
- **控制消息**（`welcome`、`ack`）：传输层元数据，不计入接收计数

## 消息格式

### JSON 消息（Text Frame）

```json
// 无 ch 或 ch=0：workspace 级（RPC 调用/响应/事件）
{"type": "rpc", "id": "1", "method": "session.list", "params": {...}}

// ch≥1：频道消息，路由到对应 channel
{"ch": 1, "type": "text_delta", "delta": "Hello"}

// 控制消息（不计入接收计数）
{"type": "welcome", "resumed": true, "last_seq": 15}
{"type": "ack", "ack": 42}
```

### Binary 消息（Binary Frame）

```
┌────────────┬───────────────────────────────┐
│ channel_id │ channel-specific data         │
│ (varint)   │ （由 channel 类型自行定义）      │
└────────────┴───────────────────────────────┘
```

channel_id 采用 LEB128 变长编码（与 protobuf varint 相同）：1-127 单字节，128+ 多字节。复用最小自然数确保绝大多数情况只需 1 字节。

各 Channel 类型在 channel_id 之后定义自己的子协议。例如 Terminal Channel 的子协议见 [terminal.md](terminal.md)。

## Channel 生命周期

客户端通过 Workspace 级 RPC 管理 Channel。

**打开**：`channel.open` RPC，指定 `target` 类型和目标参数，服务端分配 channel ID 并开始推送事件。

**关闭**：
- 主动关闭：客户端调用 `channel.close` RPC
- 被动关闭：session 删除、session 重启等场景，服务端推送 `channel.closed` 事件

```json
// 被动关闭通知（workspace 级事件，不属于任何 channel）
{"type": "event", "event": "channel.closed", "closed_ch": 1, "reason": "session_deleted"}
```

注意使用 `closed_ch` 而非 `ch`，避免被路由层当作频道消息。

## 可靠传输层

### 设计前提

WebSocket（TCP）在单次连接内保证有序、不重复、不丢失。但 `ws.send()` 成功只代表数据进入本地 TCP 缓冲区，不代表对方已收到。断线瞬间 TCP 缓冲区中的数据丢失。

### 隐式计数

不在消息中传 seq。WebSocket 保证有序不重复，双方各自对收到的内容消息计数，计数天然一致。JSON 和 Binary 共享同一个计数器。

### ACK 确认

双向累积确认。`ACK(N)` 语义："我已收到你发来的 N 条内容消息"。发送方收到 ACK 后安全丢弃发送缓冲区中已确认的消息。

ACK 同时承担心跳职责：

| 参数 | 值 | 说明 |
|------|-----|------|
| ACK 间隔 | 5 秒 | 空闲时定期发送，保活 + 死连接检测 |
| 高吞吐 ACK | 每 100 条 | 避免 send buffer 积压 |
| 死连接超时 | 15 秒 | 超时未收到任何消息 → 视为连接死亡 |

### 发送缓冲区

每一端维护 Send Buffer，保存已发送但未被对方 ACK 确认的消息。缓冲区限制：1000 条 / 1MB，溢出则 Client 过期。

## 断线重连

### 重连 URL

```
ws://host/ws/workspace/{workspace_id}?client_id=xxx&last_seq=3
```

- `client_id`：客户端标识
- `last_seq`：客户端实际收到的内容消息总数（首次连接不携带）

### 重连流程

```
Client 断线 → 指数退避重连 → 携带 client_id + last_seq
                                        │
                                ┌───────┴───────┐
                         Server 找到 Client    未找到
                         且 buffer 可覆盖      或已过期
                                │                │
                       resumed=true         resumed=false
                       replay 缺失消息       完整重置
```

**恢复成功**（`resumed: true`）：服务端从 send buffer 中重发客户端缺失的消息，客户端从自己的 send buffer 中重发服务端缺失的消息。双方计数继续递增，Channel 状态保持。

**完整重连**（`resumed: false`）：双方重置计数器和 send buffer，客户端清空所有 Channel 状态，重新 `channel.open` 所有需要的频道，各面板重新加载数据。

### Client 状态机

```
                       ┌──────────┐
           新连接 ────►│connected │◄──── 重连成功
                       └────┬─────┘
                            │ WebSocket 断开
                            ▼
                       ┌──────────┐
                       │buffering │  服务端继续向 send buffer 写入
                       └────┬─────┘
                            │
                 ┌──────────┼──────────┐
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

## 线程安全

传输层统一处理线程安全，业务代码无需关心：

- Channel 的 `enqueue_*()` 使用 `asyncio.Queue.put_nowait()`，任何线程可安全调用
- Send buffer 操作在 event loop 的 send worker 中顺序执行
- ChannelManager 映射表由 `threading.Lock` 保护（读线程并发访问场景）

Terminal 读线程直接调用 `channel.enqueue_binary()`，无需 `run_coroutine_threadsafe`。

## 架构层次

```
App WebSocket (/ws/app)          Workspace WebSocket (/ws/workspace/{id})
┌──────────────────────┐         ┌─────────────────────────────────────────────┐
│  AppRpc (JSON-RPC)   │         │  业务层（AgentPanel / TerminalPanel / RPC）   │
├──────────────────────┤         ├─────────────────────────────────────────────┤
│  ReconnectingWS      │         │  Channel 层（多路复用、按 ch 路由）            │
├──────────────────────┤         ├─────────────────────────────────────────────┤
│  WebSocket           │         │  可靠传输层（计数、ACK、Send Buffer、重连）    │
└──────────────────────┘         ├─────────────────────────────────────────────┤
                                 │  WebSocket（TCP 有序保证）                    │
                                 └─────────────────────────────────────────────┘
```

App WebSocket 轻量直通；Workspace WebSocket 业务层只与 Channel 交互，不感知计数、ACK 或断线细节。
