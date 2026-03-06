# Terminal Mac 问题修复（5 项）

**状态**：✅ 已完成

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

- [x] **Task 5.1**：`TerminalSession` session 类型新增 `scrollback_b64` 字段
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

## 测试验证

- OSC 过滤 regex：单元测试通过（`_OSC_TITLE_RE` 正确过滤 OSC 0/1/2，保留 OSC 3+）
- `TerminalSession._client_sizes` 字段：存在并默认为空 dict
- `TerminalSession.scrollback_b64` 字段：存在并默认为空字符串
**日期**：2026-03-06
**类型**：Bug修复

## 背景

5 个终端相关问题，均在 Mac 下复现：

1. Kill terminal 无限等待
2. Mac 宿主终端标题被修改（OSC 序列穿透）
3. VIM 在 web 终端中完全无法输入（tmux 中正常）
4. 多客户端连接同一终端时尺寸未取最小值
5. terminal scrollback buffer 重启后丢失

---

## Bug 1：Kill Terminal 无限等待

### 症状

从 UI 关闭 terminal session 时，操作卡住不返回（非 2 秒超时，而是无限挂起）。

### 代码流程追踪

```
handle_session_delete (routes.py:511)
  └── await sm.stop(session_id)           ← async
        └── session.on_stop(self)          ← 同步调用！
              └── _terminal_on_stop()
                    ├── loop.create_task(tm.async_notify_exit(...))  ← 此时 fire-and-forget
                    └── tm.kill(terminal_id)                         ← 同步阻塞
                          ├── self._sessions.pop(...)                ← 先删 session
                          ├── self._connections.pop(...)             ← 再清 connections
                          ├── os.close(session._fd)
                          ├── session.process.terminate()
                          └── session.process.wait(timeout=2)        ← 阻塞事件循环
```

### 已发现的问题

**问题 A：`process.wait(timeout=2)` 阻塞事件循环**

同步的 `subprocess.wait()` 在 asyncio 事件循环线程执行，阻塞整个事件循环最多 2 秒。
期间所有 WebSocket 消息、其他 HTTP 请求全部挂起，UI 完全冻结。

**问题 B：0x04 退出信号丢失 → WS 连接悬空**

`async_notify_exit` 和 `kill()` 存在严重的顺序错误：

1. `loop.create_task(async_notify_exit)` 仅调度，不立即执行
2. `tm.kill()` 立即执行：先清空 `_connections`，再终止进程
3. 当 `async_notify_exit` task 真正运行时，`_connections` 已空 → 无人收到 0x04

reader thread 的 `_notify_process_exit` 也有同样问题（connections 已被清空）。

结果：前端的 terminal WebSocket 没有收到 0x04，进入 2 秒轮询检测：
```javascript
// 每 2 秒一次 alive check（routes.py:1029-1041）
// 发现 session is None → 发送 0x04 → break
```
所以前端 WS 最多在 2 秒后才能感知终端已死，但 RPC 的 `session.delete` 响应应该已经返回。

**问题 C：无限挂起的可能根源（需重点排查）**

已知的 2 秒阻塞不足以解释"无限等待"。可能的深层原因：

1. **`process.wait(timeout=2)` 在 Mac 上未按预期超时**：macOS 上 `subprocess.wait(timeout=N)` 内部使用 polling with `os.waitpid(WNOHANG)` + `time.sleep()`，如果进程是某种特殊状态，可能挂住
2. **阻塞事件循环导致级联故障**：`process.wait(timeout=2)` 冻结事件循环期间，其他等待中的 coroutine 积压。当阻塞结束后，`asyncio.wait_for` 的超时检测是否正确恢复尚不清楚
3. **shutdown path 的额外阻塞**：`_shutdown_cleanup()` 中 `kill_all()` 是全同步的，多个 terminal 的 `wait(timeout=2)` 串行执行，可轻易超过 `asyncio.wait_for(timeout=10.0)` 的上限。而 asyncio timeout 无法中断同步代码，导致 watchdog（10秒后 os._exit）才是唯一出路
4. **bash 子进程不响应 SIGTERM**：若 bash 内有不可中断的子进程，SIGTERM 无效，wait(2s) 必然超时。但 SIGKILL 应该有效 — 除非进程处于 D 状态（不可中断睡眠，如等待 IO）

### 设计方案

**核心修复**：将 `tm.kill()` 中的阻塞 wait 移出事件循环

**方案 A（最小改动，推荐）**：直接删除 `kill()` 中的 `process.wait(timeout=2)` 和随后的 `process.kill()`。

依据：
- reader thread 的 `finally` 已处理 `proc.wait(timeout=1)` 和 exit code 采集
- `os.close(session._fd)` 之后，reader thread 的 `os.read()` 会立即得到 OSError 并退出
- reader thread 退出后 process 自然被 GC/wait 回收，不会产生僵尸进程

`kill()` 只需：
```python
os.close(session._fd)   # master fd 关闭 → reader thread 立即退出
session.process.terminate()  # 发 SIGTERM（可选：让进程有机会清理）
# 不 wait！reader thread 会自然处理
```

**同步修复：notify 顺序问题**

`_terminal_on_stop` 必须先 await notify 再 kill（需要让 `on_stop` 支持 async，或在 `handle_session_delete` 中拆分）：

```python
# 正确顺序
await tm.async_notify_exit(terminal_id)   # 先通知客户端，connections 还在
tm.kill(terminal_id)                      # 再清理
```

这需要 `_terminal_on_stop` 变为 async，或者将 notify 提升到 `handle_session_delete` 中。

### 已确认决策

**Q1 已确认**：直接移除 `kill()` 中的 `process.wait()` 和随后的 `process.kill()`。潜在的僵尸进程问题和 mutbot 退出问题留待后续处理，不在本次修复范围内。`on_stop` 异步改造也暂缓。

### 已确认决策

**Q2 已确认**：只要终端内有正在运行的进程（即 PTY 未自然退出），kill 就会无限等待。这明确了根本原因：`process.wait()` 在进程存活时永远阻塞事件循环，而进程不会自动退出。移除 `process.wait()` 后，reader thread 检测到 master fd 关闭会立即退出，问题消除。

---

## Bug 2：Mac 宿主终端标题被修改（OSC 序列穿透）

### 症状

运行 mutbot server 的 Mac 宿主终端（Terminal.app / iTerm2 等）的标题被改为 `C:\Windows\System32\conhost.exe(bash)` 或类似 Windows 相关内容。mutbot UI 内的 tab 标题不受影响（始终为"Terminal 1"）。浏览器侧无问题，**焦点是宿主终端如何收到了这个 OSC 序列**。

### 代码调查结论

已排查所有可能将数据写到宿主终端（server process 的 fd 0/1/2）的路径：

**已确认不存在泄漏的路径**：

| 路径 | 结论 |
|------|------|
| reader thread → PTY 数据 | 只写 scrollback + WebSocket，无 stdout/stderr 输出 |
| `logging.basicConfig` StreamHandler | 格式为纯文本，无 escape 序列 |
| server.py 的 LogStoreHandler + FileHandler | 写内存/文件，不写 stderr |
| `print()` 调用（server.py 关闭消息，__main__ banner） | 纯文本 |
| uvicorn 访问日志 | ANSI 颜色码，无 OSC 标题序列 |
| PTY fd 管理 | slave_fd 在 fork 后立即关闭，master_fd 只在 reader thread 读取 |

**`_spawn_unix` fd 流向**（确认正确）：
```
parent: fd 0,1,2 → PTY_HOST_SLAVE（宿主终端，不变）
        fd N (master_fd) → 由 reader thread 读取

child (fork → exec):
    setsid() → 脱离宿主终端
    dup2(slave_fd, 0/1/2) → bash stdin/stdout/stderr 均 = slave_fd
    close_fds=True → 关闭其他所有 fd
    exec(bash) → bash 只看到 slave_fd，写出的数据 → master_fd → reader thread
```

**结论**：代码层面没有已知泄漏路径。宿主终端收到 OSC 序列的机制目前**不明**，需要实机调试确认。

### 可能的泄漏机制（待验证）

1. **uvicorn 第三方库写了 stderr**：`uvicorn[standard]` 包含 httptools/uvloop/websockets，这些 C 扩展库可能在内部通过 `write(2, ...)` 直接写 stderr，绕过 Python logging。

2. **宿主 shell（zsh）的 preexec 钩子**：如果用户在宿主终端的 `.zshrc` 里有 `preexec() { print -Pn "\e]0;$1\a" }` 这样的钩子，zsh 会在每次运行命令时把命令名设为终端标题。当 mutbot 启动时 zsh 可能先将标题设成了某个值，然后 mutbot 内部的某个操作触发了 zsh 更新标题到一个奇怪的值。

3. **macOS Terminal.app 进程跟踪**：Terminal.app 默认跟踪前台进程并自动更新标题。当 mutbot 的 `subprocess.Popen` 创建 bash 子进程时，Terminal.app 可能检测到 bash 并更新标题。`C:\Windows\System32\conhost.exe` 的出现可能是 Terminal.app 检测到了某个进程名时的异常显示。

4. **scrollback 中有旧 Windows session 的 OSC 序列**：scrollback buffer 是内存中的，若 mutbot server 没有重启，可能保留了之前连过 Windows session 的 OSC 序列，replay 时 xterm.js 处理，通过某种方式影响了宿主终端。

### 诊断方法

**步骤 1：隔离 stdout/stderr**

```bash
python -m mutbot > /tmp/mutbot_stdout.log 2> /tmp/mutbot_stderr.log
```

- 若标题**仍然改变** → OSC 序列不来自 server 的 stdout/stderr，来自其他机制（可能是 Terminal.app 进程跟踪或宿主 shell 钩子）
- 若标题**不再改变** → 确认来自 server 的某个 stdout/stderr 输出，再检查 log 文件内容找具体序列

**步骤 2（如步骤 1 发现问题）：定位具体写入**

```bash
sudo dtruss -p <mutbot_pid> -t write 2>&1 | grep "\\\\e]"
```

或在 Python 层加追踪：

```python
# 临时调试代码，放到 __main__.py 最顶部
import sys, traceback

class _TitleTracer:
    def __init__(self, orig): self._orig = orig
    def write(self, s):
        if '\x1b]' in s:
            traceback.print_stack(file=self._orig)
            self._orig.write(f"[OSC DETECTED]: {repr(s)}\n")
        return self._orig.write(s)
    def __getattr__(self, a): return getattr(self._orig, a)

sys.stdout = _TitleTracer(sys.stdout)
sys.stderr = _TitleTracer(sys.stderr)
```

### 防御性修复

无论泄漏路径如何，在 reader thread 中**过滤 PTY 输出中的 OSC 序列**，防止任何标题序列传播到 xterm.js 和 scrollback：

在 `_on_pty_output`（terminal.py:228）中，存入 scrollback 之前过滤 OSC：

```python
import re
# 过滤 OSC 序列：\e]...\x07 或 \e]...\e\\（OSC string terminator）
_OSC_RE = re.compile(rb'\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)')

def _strip_osc(data: bytes) -> bytes:
    return _OSC_RE.sub(b'', data)
```

**注意**：这会同时过滤掉 xterm.js 本来可以处理的 OSC 序列（如颜色主题、超链接），是一个保守做法。如果只需过滤标题序列（OSC 0/1/2），可以更精确：

```python
# 只过滤 OSC 0/1/2（标题相关）
_TITLE_OSC_RE = re.compile(rb'\x1b\][012];[^\x07\x1b]*(?:\x07|\x1b\\)')
```

### 已确认决策

**Q3（更新）**：`onTitleChange` 暂不处理（浏览器目前无问题）。重点是找宿主终端泄漏路径。

**Q4（更新）**：穿透机制未从代码层确认。需先做诊断步骤 1（重定向 stdout/stderr）确认来源。同时加入防御性 OSC 过滤作为保底措施。

**Q8（已取消）**：跳过诊断步骤，直接实施防御性 OSC 过滤。

### 待定问题

（无）

---

## Bug 3：VIM 在 web 终端中完全卡住

### 症状

- VIM 在 web 终端中完全无法输入（完全卡住，无乱码）
- 同一环境下在 tmux 中正常工作
- 表现为所有键盘输入被丢弃

### 根本原因

**主因：`term.write("", callback)` 可靠性问题 → inputMuted 永不解除**

前端 `inputMuted` 机制：
```javascript
let inputMuted = true;  // WS 连接时设为 true

// 收到 0x03（scrollback replay complete）后解除
if (bytes[0] === 0x03) {
    term.write("", () => {       // 等 xterm.js 处理完所有待写数据
        inputMuted = false;      // 然后解除
        sendResize(...);
    });
    return;
}

// 键盘输入
term.onData((data) => {
    if (inputMuted) return;      // 如果还在 mute，输入被丢弃！
    ws.send(...);
});
```

问题：`term.write("", callback)` 的回调在某些情况下不能保证触发。若 callback 未被调用，`inputMuted` 永远为 `true`，所有键盘输入被静默丢弃，表现为"完全卡住"。

这解释了为什么 VIM 能显示内容（输出正常），但无法输入。

**次因（可能叠加）：`0x03` 发送静默失败**

```python
# routes.py:1012-1015
try:
    await websocket.send_bytes(b"\x03")
except Exception:
    pass  # 静默忽略！客户端永远收不到 0x03
```

如果 `0x03` 未发送，`inputMuted` 也永远不会解除。

**为什么 tmux 中正常工作**：tmux 不经过 WebSocket，没有 `inputMuted` 机制，自然不受此问题影响。

**次要原因：TERM 环境变量未显式设置**

`_spawn_unix` 未设置 `env` 参数，PTY shell 继承 server 进程的环境。若 server 启动时 `TERM` 不是 `xterm-256color`（如 `TERM=dumb` 或未设置），VIM 会使用错误的终端能力，导致渲染和输入序列异常。tmux 会显式设置 `TERM=screen-256color`，因此 tmux 中正常。

### 设计方案

**修复 1（主因）：替换 `term.write("", callback)` 为可靠的延迟机制**

```typescript
if (bytes[0] === 0x03) {
    // 用 requestAnimationFrame 保证 xterm.js 渲染队列已处理
    // 避免 term.write("", cb) 回调不触发的问题
    requestAnimationFrame(() => {
        inputMuted = false;
        sendResize(termRef.current?.rows ?? rows, termRef.current?.cols ?? cols);
    });
    return;
}
```

或者如果需要确保 xterm.js 的 write buffer 也处理完：
```typescript
term.write("", () => {
    requestAnimationFrame(() => {
        inputMuted = false;
        sendResize(...);
    });
});
```

**修复 2（次因）：`0x03` 发送失败时关闭 WS**

```python
# routes.py
try:
    await websocket.send_bytes(b"\x03")
except Exception:
    await websocket.close()  # 让客户端重连，重连后会重新发 0x03
    return
```

**修复 3：显式设置 TERM 环境变量**

```python
def _spawn_unix(self, session: TerminalSession, cwd: str) -> None:
    import pty, subprocess, os
    shell = os.environ.get("SHELL", "/bin/bash")
    master_fd, slave_fd = pty.openpty()

    env = {**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}

    proc = subprocess.Popen(
        [shell],
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        cwd=cwd,
        preexec_fn=os.setsid,
        close_fds=True,
        env=env,
    )
    os.close(slave_fd)
    ...
```

---

## Bug 4：多客户端连接时尺寸未取最小值

### 症状

多个客户端连接同一个 terminal 时，终端尺寸以最后连接的客户端为准，而非所有客户端中最小的（tmux 的做法）。这导致其他客户端看到内容溢出或截断。

### 根本原因

当前的 `resize()` 方法直接应用新尺寸，不考虑其他客户端：

```python
def resize(self, term_id: str, rows: int, cols: int) -> None:
    session = self._sessions.get(term_id)
    session.rows = rows; session.cols = cols
    # 直接设置 PTY 尺寸，不考虑其他客户端
    fcntl.ioctl(fd, termios.TIOCSWINSZ, ...)
```

`_connections` 里有多个 client，但每个 client 的尺寸没有被记录。

### 设计方案

在 `TerminalSession` 中记录每个 client 的 reported size，resize 时取所有 client 的最小值：

**数据结构变更**（terminal.py）：

```python
@dataclass
class TerminalSession:
    ...
    # client_id → (rows, cols)
    _client_sizes: dict[str, tuple[int, int]] = field(default_factory=dict, repr=False)
```

**resize 时更新并取最小值**：

```python
def resize(self, term_id: str, rows: int, cols: int, client_id: str | None = None) -> None:
    session = self._sessions.get(term_id)
    if session is None or not session.alive:
        return

    if client_id is not None:
        session._client_sizes[client_id] = (rows, cols)

    # 取所有 client 尺寸的最小值（tmux behavior）
    if session._client_sizes:
        eff_rows = min(r for r, _ in session._client_sizes.values())
        eff_cols = min(c for _, c in session._client_sizes.values())
    else:
        eff_rows, eff_cols = rows, cols

    session.rows = eff_rows
    session.cols = eff_cols
    # 设置 PTY 尺寸...
```

**detach 时移除该 client 的尺寸记录并重新计算**：

```python
def detach(self, term_id: str, client_id: str) -> None:
    ...
    session = self._sessions.get(term_id)
    if session:
        session._client_sizes.pop(client_id, None)
        # 重新计算并应用最小尺寸（若还有其他 client）
        if session._client_sizes and session.alive:
            self._apply_min_size(session)
```

**WS handler 需要传 client_id 给 resize()**（routes.py）：
```python
elif msg_type == 0x02 and len(raw) >= 5:
    rows = int.from_bytes(raw[1:3], "big")
    cols = int.from_bytes(raw[3:5], "big")
    tm.resize(term_id, rows, cols, client_id=str(client_id))
```

**初始连接时也注册 client size**：在 WS handler attach 之后，把初始尺寸注册进去（当前 `rows_param/cols_param` 只用于 resize，也需要记录到 client_sizes）。

### 已确认决策

**Q5 已确认**：
- detach 时重新计算剩余 clients 的最小尺寸并立即应用
- 当所有 client 都断开时，**保留最后一次的尺寸**（不重置），供下次重连使用
- `fcntl.ioctl(TIOCSWINSZ)` 自动触发 SIGWINCH，shell 自行重绘

实现：`detach()` 中，若 `_client_sizes` 变为空，不调用 `_apply_min_size`；否则调用。`session.rows/cols` 始终保存最新有效尺寸。

---

## Bug 5：Terminal Scrollback Buffer 重启后丢失

### 症状

服务端重启后，所有 terminal 的历史输出（scrollback buffer）丢失，重连后只能看到空白终端。

### 已确认决策

**Q6 已确认**：按 `session_id` 存储，而非 `terminal_id`（terminal_id 每次重启都变化，session_id 不变）。

**Q7 已确认**：在 `TerminalSession` 结束时（`on_stop`）序列化到 session 文件中，利用现有的 `session.serialize()` / `storage.save_session_metadata()` 机制，无需额外的持久化基础设施。

### 设计方案

**存储方式**：scrollback 作为 `TerminalSession` 的一个字段 `scrollback_b64: str`，通过现有序列化流程自动写入 session JSON 文件。

**序列化时机**：`_terminal_on_stop` 中，在 `tm.kill()` 之前先保存 scrollback 到 session 字段：

```python
@mutobj.impl(TerminalSession.on_stop)
def _terminal_on_stop(self: TerminalSession, sm: SessionManager) -> None:
    tm = sm.terminal_manager
    if tm is not None and self.config:
        terminal_id = self.config.get("terminal_id")
        if terminal_id and tm.has(terminal_id):
            # 先保存 scrollback 到 session 字段（序列化时自动写盘）
            scrollback_bytes = tm.get_scrollback(terminal_id)
            if scrollback_bytes:
                import base64
                self.scrollback_b64 = base64.b64encode(scrollback_bytes).decode()
            # 然后再 kill
            tm.kill(terminal_id)
    self.status = "stopped"
    # SessionManager.stop() 之后会调用 _persist(session)，自动写盘
```

**恢复时机**：`_terminal_on_create` 中，创建新 terminal 后检查 `session.scrollback_b64`，若有则注入到新 terminal 的 scrollback buffer：

```python
@mutobj.impl(TerminalSession.on_create)
def _terminal_on_create(self: TerminalSession, sm: SessionManager) -> None:
    tm = sm.terminal_manager
    # ... 创建 terminal ...
    # 恢复历史 scrollback
    if self.scrollback_b64:
        import base64
        old_scrollback = base64.b64encode(self.scrollback_b64.encode())
        # 注入到 TerminalSession 的 scrollback buffer
        # （tm 需要提供 inject_scrollback 方法）
        tm.inject_scrollback(term.id, base64.b64decode(self.scrollback_b64))
        self.scrollback_b64 = ""  # 清空，避免重复累积
```

**`TerminalSession` 新增字段**（session.py）：

```python
class TerminalSession(Session):
    display_name = "Terminal"
    display_icon = "terminal"
    scrollback_b64: str = ""   # 持久化的 scrollback，base64 编码
```

`serialize_session` 会自动包含此字段（非空时写入 JSON）。

**数据量**：64KB scrollback → base64 约 88KB，在 session JSON 中可接受。

**`inject_scrollback` 方法**（terminal.py）：

```python
def inject_scrollback(self, term_id: str, data: bytes) -> None:
    """将历史 scrollback 注入到新 terminal 的 buffer（prepend）。"""
    session = self._sessions.get(term_id)
    if session is None:
        return
    with session._scrollback_lock:
        # 拼接：历史在前，新输出在后
        combined = bytearray(data) + session._scrollback
        # 超出 SCROLLBACK_MAX 时截断旧数据
        if len(combined) > SCROLLBACK_MAX:
            combined = combined[-SCROLLBACK_MAX:]
        session._scrollback = combined
```

---

## 实施步骤清单

### Bug 1：Kill Terminal 无限等待 [待开始]

- [ ] **Task 1.1**：移除 `kill()` 中的阻塞 wait
  - [ ] `terminal.py:386-394`：删除 `session.process.terminate()` / `session.process.wait(timeout=2)` / `except Exception: process.kill()` 整段，仅保留 `os.close(session._fd)` 和 `session.process.terminate()`（保留 terminate 让进程有机会清理，但不 wait）
  - 状态：⏸️ 待开始

- [ ] **Task 1.2**：修复 notify/kill 顺序问题（避免 0x04 丢失）
  - [ ] `routes.py:handle_session_delete`：在 `await sm.stop(session_id)` 之前，先取 terminal_id 并 `await tm.async_notify_exit(terminal_id)`
  - [ ] `routes.py:handle_session_delete_batch`：同上，循环中先 notify 再 stop
  - [ ] `session_impl.py:_terminal_on_stop`：移除 `loop.create_task(tm.async_notify_exit(...))` fire-and-forget（已在 routes 层处理）
  - 状态：⏸️ 待开始

### Bug 2：OSC 序列穿透宿主终端 [待开始]

- [ ] **Task 2.1**：在 reader thread 中过滤 PTY 输出的 OSC 标题序列
  - [ ] `terminal.py:_on_pty_output` 入口处添加过滤：用 `re.compile(rb'\x1b\][012];[^\x07\x1b]*(?:\x07|\x1b\\)')` 过滤 OSC 0/1/2（标题序列），不影响其他 OSC（超链接、颜色等）
  - [ ] 过滤后的数据再存入 scrollback 并广播
  - 状态：⏸️ 待开始

### Bug 3：VIM 完全无法输入 [待开始]

- [ ] **Task 3.1**：设置 PTY 环境变量
  - [ ] `terminal.py:_spawn_unix`：spawn 时传 `env={**os.environ, "TERM": "xterm-256color", "COLORTERM": "truecolor"}`
  - 状态：⏸️ 待开始

- [ ] **Task 3.2**：修复 `0x03` 发送失败导致 inputMuted 永不解除
  - [ ] `routes.py:1012-1015`：`send_bytes(b"\x03")` 失败时改为 `await websocket.close(); return`，而非静默 `pass`
  - 状态：⏸️ 待开始

- [ ] **Task 3.3**：修复 `term.write("", callback)` 回调不可靠
  - [ ] `TerminalPanel.tsx:156-168`：将 0x03 handler 中的 `term.write("", () => { inputMuted = false; ... })` 改为 `requestAnimationFrame(() => { inputMuted = false; sendResize(...); })`
  - 状态：⏸️ 待开始

### Bug 4：多客户端尺寸未取最小值 [待开始]

- [ ] **Task 4.1**：`TerminalSession` 添加 `_client_sizes` 字段
  - [ ] `terminal.py:TerminalSession dataclass`：添加 `_client_sizes: dict[str, tuple[int, int]] = field(default_factory=dict, repr=False)`
  - 状态：⏸️ 待开始

- [ ] **Task 4.2**：`resize()` 方法改为按 client 记录并取最小值
  - [ ] `terminal.py:resize()`：添加 `client_id: str | None = None` 参数；若有 client_id，更新 `_client_sizes`；取所有 sizes 的 min 作为实际 PTY 尺寸
  - 状态：⏸️ 待开始

- [ ] **Task 4.3**：`detach()` 时清理 client size 并重新计算
  - [ ] `terminal.py:detach()`：移除 client_id 对应的 `_client_sizes` 条目；若仍有其他 client，重新计算 min 并应用；若所有 client 断开，保留最后尺寸（不操作 PTY）
  - [ ] 抽取 `_apply_min_size(session)` helper 方法，供 resize 和 detach 共用
  - 状态：⏸️ 待开始

- [ ] **Task 4.4**：WS handler 传 client_id 并注册初始尺寸
  - [ ] `routes.py:websocket_terminal`：attach 后，用 `rows_param/cols_param` 调用 `tm.resize(term_id, rows_param, cols_param, client_id=str(client_id))` 注册初始 size（替换原来的直接 `tm.resize`）
  - [ ] 0x02 消息处理：`tm.resize(term_id, rows, cols, client_id=str(client_id))`
  - 状态：⏸️ 待开始

### Bug 5：Scrollback Buffer 重启后丢失 [待开始]

- [ ] **Task 5.1**：`TerminalSession` session 类型新增 `scrollback_b64` 字段
  - [ ] `src/mutbot/session.py`（或 session_impl.py 中 TerminalSession 定义处）：添加 `scrollback_b64: str = ""`
  - 状态：⏸️ 待开始

- [ ] **Task 5.2**：`TerminalManager` 添加 `inject_scrollback()` 方法
  - [ ] `terminal.py`：按设计方案添加 `inject_scrollback(term_id, data: bytes)` 方法（prepend 历史数据，截断到 SCROLLBACK_MAX）
  - 状态：⏸️ 待开始

- [ ] **Task 5.3**：`_terminal_on_stop` 中保存 scrollback 到 session 字段
  - [ ] `session_impl.py:_terminal_on_stop`：`tm.kill()` 之前，`tm.get_scrollback(terminal_id)` 取数据，base64 编码后写入 `self.scrollback_b64`
  - 注意：`SessionManager.stop()` 在 `on_stop()` 返回后会自动调用 `_persist(session)`，无需手动写盘
  - 状态：⏸️ 待开始

- [ ] **Task 5.4**：`_terminal_on_create` 中恢复历史 scrollback
  - [ ] `session_impl.py:_terminal_on_create`：创建 terminal 后，若 `self.scrollback_b64` 非空，decode 并调用 `tm.inject_scrollback(term.id, data)`，然后清空 `self.scrollback_b64 = ""`
  - 状态：⏸️ 待开始

## 测试验证

（实施后填写）


### 源码
- `src/mutbot/runtime/terminal.py:360-398` — `kill()` 含阻塞 wait，顺序问题
- `src/mutbot/runtime/terminal.py:175-226` — reader thread，finally 已 `proc.wait(timeout=1)`
- `src/mutbot/runtime/terminal.py:258-290` — `async_notify_exit`，依赖 `_connections` 未被清空
- `src/mutbot/runtime/session_impl.py:225-239` — `_terminal_on_stop`，fire-and-forget task + 同步 kill
- `src/mutbot/runtime/session_impl.py:717-736` — `SessionManager.stop()`，async 方法
- `src/mutbot/web/routes.py:1011-1015` — 发送 `0x03`，异常被静默忽略
- `src/mutbot/web/routes.py:1049-1053` — WS handler 处理 resize，未传 client_id
- `src/mutbot/web/routes.py:973-1060` — terminal WebSocket handler 完整流程
- `src/mutbot/web/server.py:115-121` — `_shutdown_cleanup`，同步 kill_all 在 async 中
- `src/mutbot/web/server.py:281-299` — shutdown 超时保护逻辑
- `frontend/src/panels/TerminalPanel.tsx:95-170` — inputMuted 机制、`0x03` 处理、`term.write` callback
- `frontend/src/panels/TerminalPanel.tsx:246-256` — `onData` handler，被 inputMuted 控制
