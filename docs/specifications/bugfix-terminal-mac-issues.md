# Terminal 问题修复（6 项）

**状态**：✅ 已完成
**日期**：2026-03-06
**类型**：Bug修复

## 背景

6 个终端相关问题，均在 Mac 下复现：

1. Kill terminal 无限等待
2. Mac 宿主终端标题被修改（OSC 序列穿透）
3. VIM 在 web 终端中完全无法输入（tmux 中正常）
4. 多客户端连接同一终端时尺寸未取最小值
5. Terminal scrollback buffer 重启后丢失
6. Terminal 连接/重启 UX 问题（overlay 延迟、状态不一致）

---

## Bug 1：Kill Terminal 无限等待

### 症状

从 UI 关闭 terminal session 时，操作卡住不返回（非 2 秒超时，而是无限挂起）。

### 已发现的问题

**问题 A：`process.wait(timeout=2)` 阻塞事件循环**

同步的 `subprocess.wait()` 在 asyncio 事件循环线程执行，阻塞整个事件循环最多 2 秒。期间所有 WebSocket 消息、其他 HTTP 请求全部挂起，UI 完全冻结。

**问题 B：0x04 退出信号丢失 → WS 连接悬空**

`async_notify_exit` 和 `kill()` 存在严重的顺序错误：

1. `loop.create_task(async_notify_exit)` 仅调度，不立即执行
2. `tm.kill()` 立即执行：先清空 `_connections`，再终止进程
3. 当 `async_notify_exit` task 真正运行时，`_connections` 已空 → 无人收到 0x04

### 设计方案

**核心修复**：直接删除 `kill()` 中的 `process.wait(timeout=2)` 和随后的 `process.kill()`（方案 A，最小改动）。

依据：reader thread 的 `finally` 已处理 `proc.wait(timeout=1)` 和 exit code 采集；`os.close(session._fd)` 之后 reader thread 立即退出并回收进程。

**同步修复：notify 顺序**：`handle_session_delete` 中先 `await tm.async_notify_exit(terminal_id)` 再 `await sm.stop()`，`_terminal_on_stop` 不再 fire-and-forget。

---

## Bug 2：Mac 宿主终端标题被修改（OSC 序列穿透）

### 症状

运行 mutbot server 的 Mac 宿主终端标题被改为 `C:\Windows\System32\conhost.exe(bash)` 或类似内容。

### 代码调查结论

已排查所有可能将数据写到宿主终端（fd 0/1/2）的路径，均无已知泄漏。泄漏机制未从代码层确认（可能是 macOS Terminal.app 进程跟踪、宿主 shell preexec 钩子等）。

### 设计方案

在 reader thread 中**过滤 PTY 输出的 OSC 0/1/2 标题序列**，防止传播到 xterm.js 和 scrollback（保守防御，不影响超链接等其他 OSC）。

---

## Bug 3：VIM 在 web 终端中完全卡住

### 症状

VIM 在 web 终端中完全无法输入，同一环境下在 tmux 中正常工作。

### 根本原因

**主因：`term.write("", callback)` 可靠性问题 → `inputMuted` 永不解除**

`term.write("", callback)` 的回调在某些情况下不触发。若 callback 未调用，`inputMuted` 永远为 `true`，所有键盘输入被静默丢弃。

**次因：`0x03` 发送静默失败**

`send_bytes(b"\x03")` 失败时原代码 `except: pass`，客户端永远收不到 `0x03`。

**次要原因：`TERM` 环境变量未显式设置**

PTY shell 继承 server 进程环境，若 `TERM` 不是 `xterm-256color`，VIM 使用错误的终端能力。

### 设计方案

1. 用 `requestAnimationFrame` 替换 `term.write("", callback)` 解除 `inputMuted`
2. `0x03` 发送失败时 `await websocket.close(); return`（让客户端重连）
3. spawn 时传 `env={**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}`

---

## Bug 4：多客户端连接时尺寸未取最小值

### 症状

多个客户端连接同一 terminal 时，终端尺寸以最后连接的客户端为准，而非所有客户端中最小的（tmux 行为）。

### 设计方案

在 `TerminalSession` 中记录每个 client 的 reported size（`_client_sizes: dict[str, tuple[int, int]]`），resize 时取所有 client 的最小值；detach 时移除对应 client 并重新计算。

---

## Bug 5：Terminal Scrollback Buffer 重启后丢失

### 症状

服务端重启后，所有 terminal 的历史输出（scrollback buffer）丢失，重连后只能看到空白终端。

### 设计方案

**存储**：`TerminalSession` 新增 `scrollback_b64: str` 字段，通过现有序列化流程自动写入 session JSON。

**序列化时机**：`_terminal_on_stop` 中，`tm.kill()` 之前 base64 编码 scrollback 写入字段。

**恢复时机**：`_terminal_on_create` 中，创建 terminal 后 decode 并 `inject_scrollback`，然后清空字段。

**关键修复（shutdown 路径）**：`server.py:_shutdown_cleanup` 必须迭代 `_sessions`（所有 session 类型）而非 `_runtimes`（仅 AgentSession），确保 TerminalSession 的 `on_stop` 被调用，scrollback 得以保存。

---

## Bug 6：Terminal 连接/重启 UX 问题

### 症状

1. **exit 后刷新**：terminal 在很长一段时间内不显示 "Restart Terminal" overlay，用户误以为 terminal 活跃
2. **server 重启后**：terminal 应先展示 scrollback history，再显示 "Restart Terminal"（需用户确认后才创建新 PTY）
3. **页面连接中**：无视觉反馈，terminal 看起来可操作但实际不可输入

### 设计方案

**Frontend — `connected` 状态**：
- 新增 `connected` state（初始 `false`），仅在收到 `0x03` 时设为 `true`，`ws.onclose` 时重置为 `false`
- Overlay 条件：`!connected || expired`；内容：connecting 时显示 "Connecting..."，expired 时显示 "Restart Terminal" 按钮
- "Restart Terminal" 流程（`session.restart` RPC）：kill 旧 PTY、清空 scrollback_b64、创建新 PTY，而非 `terminal.create`

**Server — 死 terminal 快速检测**：
- 活跃 terminal WS 路径：scrollback 发送后，检查 `session.alive`；若已死，立即发 `0x04`，不发 `0x03`（避免 2s 延迟才触发 expired 状态）
- 死 terminal WS 路径：`terminal_id` 不在 `tm` 中时，查找对应 session 的 `scrollback_b64`，replay 后发 `0x03` + `0x04`（让客户端先看到历史，再显示 Restart 按钮）

**Server — `session.restart` RPC**：
- 接收 `session_id`，kill 旧 PTY（先 `async_notify_exit`），清空 `scrollback_b64`，调用 `on_create` 创建新 PTY，广播 `session_updated`，返回新 `terminal_id`

---

## 实施步骤清单

### Bug 1：Kill Terminal 无限等待 [✅ 已完成]

- [x] **Task 1.1**：移除 `kill()` 中的阻塞 wait
  - [x] `terminal.py`：删除 `process.wait(timeout=2)` 和 `process.kill()`，保留 `os.close(session._fd)` 和 `session.process.terminate()`
  - 状态：✅ 已完成

- [x] **Task 1.2**：修复 notify/kill 顺序问题（避免 0x04 丢失）
  - [x] `routes.py:handle_session_delete`：stop 前先 `await tm.async_notify_exit(terminal_id)`
  - [x] `routes.py:handle_session_delete_batch`：同上
  - [x] `session_impl.py:_terminal_on_stop`：移除 fire-and-forget `create_task`
  - 状态：✅ 已完成

### Bug 2：OSC 序列穿透宿主终端 [✅ 已完成]

- [x] **Task 2.1**：在 reader thread 中过滤 PTY 输出的 OSC 标题序列
  - [x] `terminal.py`：模块级 `_OSC_TITLE_RE` regex，`_on_pty_output` 入口过滤 OSC 0/1/2
  - 状态：✅ 已完成

### Bug 3：VIM 完全无法输入 [✅ 已完成]

- [x] **Task 3.1**：设置 PTY 环境变量
  - [x] `terminal.py:_spawn_unix`：`env={**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}`
  - 状态：✅ 已完成

- [x] **Task 3.2**：修复 `0x03` 发送失败导致 inputMuted 永不解除
  - [x] `routes.py`：`send_bytes(b"\x03")` 失败时 `await websocket.close(); return`
  - 状态：✅ 已完成

- [x] **Task 3.3**：修复 `term.write("", callback)` 回调不可靠
  - [x] `TerminalPanel.tsx`：改用 `requestAnimationFrame(() => { inputMuted = false; sendResize(...); })`
  - 状态：✅ 已完成

### Bug 4：多客户端尺寸未取最小值 [✅ 已完成]

- [x] **Task 4.1**：`TerminalSession` 添加 `_client_sizes` 字段
  - 状态：✅ 已完成

- [x] **Task 4.2**：`resize()` 方法改为按 client 记录并取最小值
  - [x] `terminal.py:resize(client_id=None)`：更新 `_client_sizes`，调用 `_apply_min_size`
  - [x] `_apply_min_size()` + `_set_pty_size()` helper 方法
  - 状态：✅ 已完成

- [x] **Task 4.3**：`detach()` 时清理 client size 并重新计算
  - [x] `terminal.py:detach()`：清理 `_client_sizes`，若有剩余 client 调用 `_apply_min_size`
  - 状态：✅ 已完成

- [x] **Task 4.4**：WS handler 传 client_id 并注册初始尺寸
  - [x] `routes.py:websocket_terminal`：初始 resize 和 0x02 resize 均传 `client_id=str(client_id)`
  - 状态：✅ 已完成

### Bug 5：Scrollback Buffer 重启后丢失 [✅ 已完成]

- [x] **Task 5.1**：`TerminalSession` 新增 `scrollback_b64` 字段
  - [x] `session.py:TerminalSession`：`scrollback_b64: str = ""`
  - 状态：✅ 已完成

- [x] **Task 5.2**：`TerminalManager` 添加 `inject_scrollback()` 方法
  - [x] `terminal.py`：prepend 历史数据，截断到 SCROLLBACK_MAX
  - 状态：✅ 已完成

- [x] **Task 5.3**：`_terminal_on_stop` 中保存 scrollback 到 session 字段
  - [x] `session_impl.py`：kill 前 base64 编码写入 `self.scrollback_b64`
  - 状态：✅ 已完成

- [x] **Task 5.4**：`_terminal_on_create` 中恢复历史 scrollback
  - [x] `session_impl.py`：创建 terminal 后 decode 并 `inject_scrollback`，清空字段
  - 状态：✅ 已完成

- [x] **Task 5.5**：修复 `_shutdown_cleanup` 遗漏 TerminalSession
  - [x] `server.py:_shutdown_cleanup`：改为迭代 `_sessions`（所有类型）而非 `_runtimes`（仅 AgentSession）
  - 状态：✅ 已完成

### Bug 6：Terminal 连接/重启 UX [✅ 已完成]

- [x] **Task 6.1**：Frontend `connected` 状态与 overlay 重构
  - [x] `TerminalPanel.tsx`：新增 `connected` state（初始 `false`）
  - [x] `TerminalPanel.tsx`：0x03 handler 的 `requestAnimationFrame` 中 `setConnected(true)`
  - [x] `TerminalPanel.tsx`：`ws.onclose` 中 `setConnected(false)`，`handleRecreate` 中 `setConnected(false)`
  - [x] `TerminalPanel.tsx`：overlay 条件改为 `!connected || expired`，connecting 时显示 "Connecting..."，expired 时显示 "Restart Terminal"
  - [x] `index.css`：新增 `.terminal-connecting` 样式
  - 状态：✅ 已完成

- [x] **Task 6.2**：Frontend 使用 `session.restart` RPC 替代 `terminal.create`
  - [x] `TerminalPanel.tsx:init()`：session-backed terminal 调用 `session.restart` RPC
  - [x] `TerminalPanel.tsx:initRef`：session-backed terminal 跳过 `terminal.delete`（lifecycle 由 `session.restart` 管理）
  - 状态：✅ 已完成

- [x] **Task 6.3**：Server 活跃 terminal WS 路径——死 terminal 快速检测
  - [x] `routes.py:websocket_terminal`：scrollback 发送后检查 `session.alive`；若已死立即发 `0x04` 并 return，不发 `0x03`（消除 2s 延迟）
  - 状态：✅ 已完成

- [x] **Task 6.4**：Server 死 terminal WS 路径——scrollback replay + 立即 exit
  - [x] `routes.py:websocket_terminal`：`terminal_id` 不在 `tm` 时，查找对应 session 的 `scrollback_b64`，replay 后发 `0x03` + `0x04`；无匹配则 close(4004)
  - 状态：✅ 已完成

- [x] **Task 6.5**：Server 新增 `session.restart` RPC
  - [x] `routes.py`：`handle_session_restart` — kill 旧 PTY（`async_notify_exit` + `kill`），清空 `scrollback_b64`，调用 `on_create`，广播 `session_updated`，返回新 `terminal_id`
  - 状态：✅ 已完成

## 测试验证

- OSC 过滤 regex：`_OSC_TITLE_RE` 正确过滤 OSC 0/1/2，保留 OSC 3+
- `TerminalSession._client_sizes`：存在并默认为空 dict
- `TerminalSession.scrollback_b64`：存在并默认为空字符串
- Scrollback 路径验证（手动）：
  - 正常退出 terminal → 重启 server → 刷新页面：history 可见 + Restart Terminal ✅
  - 关闭 server → 重启 → 刷新：history 可见 + Restart Terminal ✅
  - 刷新两次：history 不丢失 ✅
  - exit 后刷新：立即显示 Connecting... → 随即显示 Restart Terminal（无 2s 延迟）✅

## 关键参考

### 源码
- `src/mutbot/runtime/terminal.py` — `kill()`, `inject_scrollback()`, `_apply_min_size()`, reader thread
- `src/mutbot/runtime/session_impl.py` — `_terminal_on_stop`, `_terminal_on_create`
- `src/mutbot/web/server.py:_shutdown_cleanup` — 必须迭代 `_sessions` 而非 `_runtimes`
- `src/mutbot/web/routes.py:websocket_terminal` — WS 协议：0x01 PTY 输出，0x03 replay complete，0x04 exit，4004 terminal not found
- `src/mutbot/web/routes.py:handle_session_restart` — `session.restart` RPC
- `frontend/src/panels/TerminalPanel.tsx` — `connected` state，overlay，`session.restart` 调用
- `frontend/src/index.css` — `.terminal-expired-overlay`, `.terminal-connecting`
