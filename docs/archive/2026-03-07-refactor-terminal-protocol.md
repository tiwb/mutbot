# 终端传输协议迭代优化 设计规范

**状态**：✅ 已完成
**日期**：2026-03-07
**类型**：重构

## 背景

统一 WebSocket 连接重构（`refactor-unified-websocket.md`）已完成 Phase 1-6，终端通过 channel 多路复用传输，同时支持 JSON 和 Binary 两种帧类型。

当前终端 channel 的**所有消息都走二进制协议**：

| msg_type | 方向 | 说明 | payload | 频率 |
|----------|------|------|---------|------|
| `0x00` | C→S | 输入 | data | **高频** — 每次按键 |
| `0x01` | S→C | 输出 | data | **高频** — PTY 输出流 |
| `0x02` | C→S | resize | 2B rows + 2B cols | **极低频** — 窗口大小改变时 |
| `0x03` | S→C | scrollback 完成 | 无 | **一次性** — channel 打开时 |
| `0x04` | S→C | 进程退出 | optional 4B exit_code | **一次性** — 进程结束时 |

**问题**：resize（0x02）、scrollback 完成（0x03）、进程退出（0x04）都是低频或一次性的控制消息，使用二进制协议带来的好处（节省带宽、低开销）微乎其微，但增加了编解码复杂度和调试难度。

**优化方向**：将低频控制消息改为 JSON 传输，二进制协议仅保留高频的 I/O 数据（输入 0x00、输出 0x01）。

## 设计方案

### 核心设计

将终端 channel 的消息按频率分为两类：

| 类别 | 帧类型 | 消息 | 理由 |
|------|--------|------|------|
| **I/O 数据** | Binary | 输入、输出 | 高频、大数据量，二进制零开销 |
| **控制信号** | JSON | resize、scrollback 完成、进程退出 | 低频/一次性，JSON 可读性好、易调试 |

#### 二进制协议简化

优化后二进制帧**不再需要 msg_type 字节**，因为只剩下两种消息，而方向天然区分：

```
优化前:
┌────────────┬──────────┬───────────────────┐
│ channel_id │ msg_type │ payload           │
│ (varint)   │ 1 byte   │ data bytes        │
└────────────┴──────────┴───────────────────┘

优化后:
┌────────────┬───────────────────┐
│ channel_id │ payload           │
│ (varint)   │ data bytes        │
└────────────┴───────────────────┘
```

- Client→Server 的 binary = 终端输入（原 0x00）
- Server→Client 的 binary = 终端输出（原 0x01）
- 方向已由 WebSocket 帧的发送方隐式区分，无需 msg_type

每帧节省 1 字节。对高频小包（单个按键输入）节省比例可观（原 2 字节 payload 变 1 字节）。

#### JSON 控制消息格式

控制消息通过 channel 的 JSON 帧发送：

```json
// Client→Server: 窗口大小改变
{"ch": 2, "type": "resize", "rows": 24, "cols": 80}

// Server→Client: scrollback 回放完成
{"ch": 2, "type": "scrollback_done"}

// Server→Client: 进程退出
{"ch": 2, "type": "process_exit", "exit_code": 0}

// Server→Client: 进程退出（无退出码）
{"ch": 2, "type": "process_exit"}
```

### 优势

1. **二进制协议极简化** — 去掉 msg_type，binary 帧 = 纯 I/O 数据，语义最清晰
2. **控制消息可读性** — JSON 格式易于日志记录和调试（当前 binary 控制消息在日志中是不可读的字节流）
3. **扩展性** — JSON 控制消息天然支持添加字段（如 resize 可携带 `pixel_width`/`pixel_height`），无需修改二进制编码
4. **代码简化** — 前后端 binary 帧解析逻辑大幅简化，不再需要 switch/if msg_type 分支

### 实施概要

后端修改 `routes.py` 中 terminal channel 的 binary handler 和 `terminal.py` 中的回调逻辑；前端修改 `TerminalPanel.tsx` 的消息收发代码。变更范围较小，仅涉及终端 channel 的消息编解码层。

## 关键参考

### 源码
- `src/mutbot/web/routes.py` — `_handle_channel_binary` 终端 binary 消息处理（L1352-1385）、`_attach_terminal_channel` scrollback 回放（L1468-1516）
- `src/mutbot/runtime/terminal.py` — `_on_pty_output` 输出回调（L200-296）、`_make_exit_payload` 退出消息构造（L297-318）
- `frontend/src/panels/TerminalPanel.tsx` — `handleBinaryData` 消息解析（L99-139）、`sendResize` 发送 resize（L88-97）、输入发送（L219-230）
- `frontend/src/lib/workspace-rpc.ts` — `sendBinaryToChannel`、`onBinaryChannel`、`onChannel` API

### 相关规范
- `docs/specifications/refactor-unified-websocket.md` — Channel 架构、binary 帧格式、可靠传输层

## 风险评估

**整体风险：低**。变更范围集中在终端 channel 的消息编解码层，不涉及传输层、Channel 架构或可靠传输机制。

| 风险点 | 级别 | 说明 |
|--------|------|------|
| 前后端不同步 | 无 | 统一发布，不存在版本不一致 |
| terminal.py 读线程回调 | **低** | `_on_pty_output` 去掉 0x01 前缀；新增 `on_exit` 回调替代 `_make_exit_payload`，读线程调用 `enqueue_binary/json` 都是线程安全的（put_nowait） |
| scrollback_done / process_exit 的发送时序 | **低** | 从 `enqueue_binary` 改为 `enqueue_json`，两者共享同一个 Client send queue，发送顺序不变 |
| 前端 handleBinaryData 简化 | 无 | 去掉 msg_type 分支后逻辑更简单 |

**唯一需要注意的点**：`terminal.py` 的 `_notify_process_exit` 和 `async_notify_exit` 目前通过 `on_output(payload)` 回调发送 binary exit payload。改为 JSON 后，给 `attach()` 增加 `on_exit(exit_code)` 回调，与 `on_output(data)` 并列。`terminal.py` 保持传输无关，不引入 ChannelManager 依赖。

## 实施步骤清单

### Task 1: 后端 — terminal.py 回调机制重构
- [x] `_on_pty_output`: `payload = b"\x01" + data` → `payload = data`（去掉 0x01 前缀）
- [x] `attach()` 签名增加 `on_exit: Callable[[int | None], None]` 回调参数
- [x] `_connections` 存储结构从 `(on_output, loop)` 扩展为 `(on_output, on_exit, loop)`
- [x] `_notify_process_exit`: 改为调用 `on_exit(exit_code)` 回调（不再构造 binary payload）
- [x] `async_notify_exit`: 同上改为调用 `on_exit(exit_code)` 回调
- [x] 删除 `_make_exit_payload` 方法（不再需要）
- 状态：✅ 已完成

### Task 2: 后端 — routes.py 消息处理适配
- [x] `_handle_channel_binary`: 去掉 `msg_type` 解析，payload 整体即为终端输入数据；删除 resize 分支
- [x] `_handle_channel_json`: 新增 terminal channel 的 JSON 消息处理 — `resize` 类型
- [x] `_attach_terminal_channel`: scrollback 发送去掉 0x01 前缀；`bytes([0x03])` 改为 `enqueue_json({"type": "scrollback_done"})`；`bytes([0x04])` 改为 `enqueue_json({"type": "process_exit", ...})`
- [x] `on_output` 闭包：直接透传 data（不再带 0x01 前缀）
- [x] `on_exit` 闭包：构造 `{"type": "process_exit", ...}` JSON 并 `enqueue_json`
- [x] `tm.attach()` 调用处传入 `on_exit` 回调
- 状态：✅ 已完成

### Task 3: 前端 — TerminalPanel.tsx 适配
- [x] `handleBinaryData`: 去掉 msg_type 分支，payload 整体即为输出数据 → `term.write(payload)`
- [x] 新增 `handleJsonMessage`: 处理 `scrollback_done`（unmute input）和 `process_exit`（显示退出信息）
- [x] `sendResize`: 从 binary 改为 `rpc.sendToChannel(ch, {type: "resize", rows, cols})`
- [x] 输入发送：去掉 `buf[0] = 0x00` 前缀，直接发送编码后的数据
- [x] paste 功能：同步去掉 `buf[0] = 0x00` 前缀
- [x] 注册 `rpc.onChannel(ch, handleJsonMessage)` 监听 JSON 控制消息
- 状态：✅ 已完成

### Task 4: 更新 docs/design/terminal.md 设计文档
- [x] 重写"WebSocket 通信协议"章节：binary 帧仅用于 I/O（输入/输出），控制消息（resize、scrollback_done、process_exit）改为 JSON
- [x] 更新 Input Muting 说明：从"收到 0x03"改为"收到 scrollback_done JSON 消息"
- 状态：✅ 已完成

### Task 5: 构建验证
- [x] 前端构建：`npm --prefix frontend run build`
- [ ] 功能验证：终端输入输出、resize、scrollback replay、进程退出
- 状态：🔄 进行中

## 测试验证
- 前端 TypeScript 编译 + Vite 构建通过
- 功能验证待手动测试
