# 终端传输层韧性优化 设计规范

**状态**：✅ 已完成
**日期**：2026-03-16
**类型**：功能设计

## 背景

### 问题概述

终端是 mutbot 当前使用最频繁的功能。pyte 下沉到 ptyhost 后，渲染管线已从 raw bytes 转为 dirty diff（KB 级帧），传输量大幅降低。但传输层本身仍有几个韧性问题，在弱网、多终端、移动端等场景下影响体验：

1. **Send buffer 溢出直接 expire** — 没有降级策略，弱网下终端帧积压触发 client 过期，所有 channel 关闭，用户看到终端冻结
2. **不可见面板持续推送** — 终端面板最小化或切到其他标签页后，服务端仍推 ANSI 帧，白白占用 buffer 容量和带宽
3. **长时间后 workspace 断线不恢复** — 浏览器最小化后终端无法输入，需刷新页面
4. **Follow Me 后 PTY 尺寸未实际切换** — resize 命令转发时机有问题

### 当前传输层架构

```
ptyhost (80ms dirty diff) → mutbot TerminalManager → Client.enqueue()
    → asyncio.Queue → _send_worker → SendBuffer.append() → ws.send()
                                          ↓ overflow
                                     Client._expire()
                                          ↓
                                   所有 Channel 关闭
```

关键参数：
- SendBuffer: MAX_MESSAGES=1000, MAX_BYTES=1MB
- ACK 间隔: 5s，高吞吐每 100 条
- 死连接超时: 15s
- Buffering 超时: 30s

### 根因分析（已确认）

#### 问题 3 的根因：Chrome intensive throttling

**现象**：浏览器最小化一段时间后终端无法输入，需刷新页面。

**复现时间线**（从诊断日志确认）：

```
17:20:51  浏览器最小化 → visibilityState = hidden
17:20:51 ~ 17:21:48  前端 ACK 正常发送（约 57 秒）
~17:21:51  Chrome intensive throttling 生效（hidden 后 ~60 秒）
           setInterval 从 5s 被节流到 ≥60s
17:22:03  服务端 dead timeout（no data for 15.0s）→ enter_buffering
          但服务端未关闭 WebSocket → 前端不知道连接已断
17:22:33  Client expired（buffering 30s 超时）
17:23:02  用户恢复浏览器 → visibilityState = visible
          前端 WebSocket 仍显示 OPEN → 以为连接正常 → 输入无响应
```

**根因链**：
1. Chrome 对 hidden 页面的 `setInterval` 在 ~60 秒后启用 intensive throttling（每分钟最多 1 次）
2. 前端 5s ACK 定时器被节流 → 服务端 15s 内收不到任何数据 → dead timeout
3. 服务端 `enter_buffering()` 只将 `self.ws = None`，**不关闭 WebSocket**
4. 前端 `ws.readyState` 仍为 `OPEN`，不知道连接已失效 → 用户输入进入黑洞

**关键区分**：窗口失焦（blur）不触发 Chrome 节流，只有标签页 hidden（切换标签页或最小化）才触发。

#### 问题 1 的分析：buffer 溢出均发生在 buffering 状态

从日志分析，所有 buffer 溢出都发生在 **WS 已断开、Client 处于 buffering 状态** 时。终端帧持续入队等待重连 replay，但终端帧不需要 replay（有 snapshot 兜底）。正常连接状态下从未观察到溢出。

## 设计方案

设计原则：**简单保守**。传输层难以充分测试，复杂策略容易引入新问题。

### 机制一：即时 ACK + 心跳分离

**现状**：前端每 5 秒发一次 ACK，兼做心跳和确认。

**改为**：
- **收到内容消息时立即回复 ACK** — 服务端实时知道客户端的接收进度
- **5 秒定时器仅作为心跳**（无新内容时的保活信号）

**作用**：为机制三提供基础——服务端只有知道客户端收到了什么，才能做流量控制。

### 机制二：断开感知 + visibility 驱动重连

不尝试在 hidden 状态下保活。接受断开，确保前端感知并在恢复时快速重连。

**服务端**：
- dead timeout 保持 15s（快速检测死连接）
- `enter_buffering()` 时**主动关闭 WebSocket**（已实现），前端收到 `onclose`

**前端重连策略**：
- `onclose` 后进入重连状态，使用指数退避（1s, 2s, 4s... 最大 30s）
- **hidden 期间不主动重试**（Chrome 会节流 setTimeout，重试也是浪费）
- **`visibilitychange` → visible 时立即重连**：取消当前退避定时器，立即尝试连接，重置 retryCount
- **`online` 事件也触发立即重连**（网络恢复场景）
- 连接成功 → 正常工作；连接失败 → 从头开始正常退避

**典型场景**：
```
用户最小化浏览器
  → ~75s 后断开（60s Chrome 节流 + 15s dead timeout）
  → 服务端关闭 WS → 前端收到 onclose → 进入重连状态
  → hidden 期间不重试
用户恢复浏览器
  → visibilitychange → visible
  → 立即重连（retryCount = 0）
  → 连接成功 → 终端通过 snapshot 恢复画面
```

### 机制三：终端流量自适应

**现状**：服务端不关心客户端是否收到，持续推送终端帧。buffer 溢出时直接 expire。

**改为**：
- 服务端根据客户端 ACK 进度控制终端帧推送速率
- 如果客户端未 ACK 的终端帧超过阈值（条数或字节数），**暂停推送**，等待客户端 ACK 后再继续
- 终端帧天然可丢弃——暂停期间产生的帧被跳过，恢复时发送最新 snapshot 即可
- **WS 断开后立即停止推送终端帧**（buffering 状态不再缓冲终端帧）

**效果**：
- 弱网时终端帧自动降速，不会撑爆 buffer
- 暂停→恢复时用 snapshot 保证画面正确性
- buffer 溢出场景从根本上消除

## 关键参考

### 源码

- `mutbot/src/mutbot/web/transport.py` — SendBuffer、Client、ChannelTransport、ChannelManager 完整实现
- `mutbot/src/mutbot/web/routes.py` — WebSocket endpoint、client 注册、expire 回调、channel 关闭
- `mutbot/src/mutbot/runtime/terminal.py` — TerminalManager，终端帧转发、ptyhost 断开处理
- `mutbot/src/mutbot/ptyhost/_client.py` — PtyHostClient，ptyhost 连接和断开检测
- `mutbot/frontend/src/lib/workspace-rpc.ts` — 前端 WebSocket RPC 客户端（ACK、重连、visibility 上报）

### 设计文档

- `mutbot/docs/design/transport.md` — 统一 WebSocket 传输层设计（Client 状态机、ACK、SendBuffer、断线重连）
- `mutbot/docs/specifications/feature-pyte-frameskip-scroll.md` — pyte 跳帧渲染（80ms 定时器、scroll 协议）
- `mutbot/docs/specifications/refactor-pyte-to-ptyhost.md` — pyte 下沉到 ptyhost（View 抽象、snapshot）
- `mutbot/docs/specifications/feature-terminal-resize-control.md` — resize 控制权（Follow Me + Auto）

### Chrome 节流机制

- 页面 hidden 约 60 秒后：**intensive throttling** 生效，`setInterval` 最多每分钟触发 1 次
- 页面恢复 visible 后立即恢复正常调度
- **窗口失焦（blur）不触发节流**，只有 `visibilityState === "hidden"` 才触发

### 已完成的诊断代码

- `transport.py` — `_last_recv_time` 跟踪 + dead timeout 静默时长日志 + `enter_buffering()` 主动关闭 WS
- `routes.py` — visibility/focus/blur 消息处理和日志
- `workspace-rpc.ts` — `visibilitychange` + `window.focus/blur` 事件上报

### 遗留问题来源

- `TODO.md` #90: 优化 terminal 输出发送帧数，合并发送
- `TODO.md` #93: 面板显示时 open channel，关闭或不可见后 close
- `TODO.md` #95: workspace 断线不自动重连
- `TODO.md` #113: 终端发送全部历史，弱网闪烁
- 记忆文件 #13: send buffer overflow → client expire → 冻结
- 记忆文件 #15: 确认去掉涌入检测后无冻结
- 记忆文件 #17: Follow Me 后 PTY 尺寸未实际切换

## 实施步骤清单

### Phase 1: 机制一 — 即时 ACK + 心跳分离 [✅ 已完成]

- [x] **Task 1.1**: 前端即时 ACK
  - [x] `workspace-rpc.ts` — `onContentReceived()` 中收到内容消息后立即调用 `sendAck()`，不再仅依赖 batch 阈值
  - [x] 5s 定时器保留，仅作为心跳（无新内容时的保活信号）
  - 状态：✅ 已完成

- [x] **Task 1.2**: 服务端适配即时 ACK
  - [x] `transport.py` — `on_content_received()` 改为每次收到内容消息立即调用 `_send_ack_now()`
  - [x] `SendBuffer.on_ack()` 已确认高频调用安全（幂等，忽略无效 ACK）
  - 状态：✅ 已完成

### Phase 2: 机制二 — 断开感知 + visibility 驱动重连 [✅ 已完成]

- [x] **Task 2.1**: 确认服务端 `enter_buffering()` 关闭 WS
  - [x] 已确认：transport.py:300-302 主动 `asyncio.ensure_future(self._close_ws(ws))`
  - 状态：✅ 已完成

- [x] **Task 2.2**: 前端 visibility 感知重连
  - [x] `onclose` 中检查 `visibilityState`：hidden 时标记 `pendingReconnect`，不启动退避定时器
  - [x] `visibilitychange` → visible 时：若 `pendingReconnect`，立即重连并重置 retryCount
  - [x] 新增 `online` 事件监听，网络恢复时触发立即重连
  - [x] `close()` 中清理 `onlineHandler`
  - 状态：✅ 已完成

- [ ] **Task 2.3**: 前端重连 UI 反馈（可选，暂不实施）
  - [ ] 断开时显示连接状态提示，重连成功后自动消失
  - 状态：⏸️ 待开始

### Phase 3: 机制三 — 终端流量自适应 [✅ 已完成]

- [x] **Task 3.1**: buffering 状态停止缓冲终端帧
  - [x] `transport.py` — `Client.binary_allowed()` 检查 `state != "connected"` 时返回 False
  - [x] `_channel_send_binary` 中调用 `binary_allowed()` 拦截，丢弃帧不入队
  - [x] 重连后通过 snapshot 恢复画面（已有机制）
  - 状态：✅ 已完成

- [x] **Task 3.2**: 终端帧流控 — 基于 ACK 进度的背压
  - [x] `transport.py` — `BINARY_PAUSE_THRESHOLD = 200`，pending 超限时暂停推送
  - [x] `on_peer_ack()` 中背压恢复（滞后阈值 1/2 = 100，避免频繁切换）
  - [x] 恢复时触发 `_on_binary_resume` 回调
  - [x] `terminal.py` — attach 时注册 resume 回调，请求 snapshot 恢复画面
  - 状态：✅ 已完成

### Phase 4: 测试验证 [进行中]

- [ ] **Task 4.1**: 基本功能验证
  - [ ] 正常使用终端：输入、输出、滚动正常
  - [ ] 多终端同时使用无异常
  - 状态：⏸️ 待开始

- [ ] **Task 4.2**: Chrome 节流场景验证
  - [ ] 最小化浏览器 > 75s → 恢复后终端自动重连可用
  - [ ] 切换标签页 > 75s → 切回后终端自动重连可用
  - 状态：⏸️ 待开始

- [ ] **Task 4.3**: 弱网场景验证
  - [ ] Chrome DevTools 模拟慢速网络，终端帧自动降速不冻结
  - [ ] 断网 → 恢复网络 → online 事件触发重连
  - 状态：⏸️ 待开始

### Phase 5: 清理 [待开始]

- [ ] **Task 5.1**: 更新设计文档
  - [ ] 同步更新 `docs/design/transport.md` 中的 ACK 机制和重连策略描述
  - 状态：⏸️ 待开始

## 测试验证

（实施阶段填写）
