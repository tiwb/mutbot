# 终端可靠性修复 设计规范

**状态**：✅ 已完成
**日期**：2026-03-07
**类型**：Bug修复

## 背景

终端功能存在两个独立的可靠性问题：

1. **服务器重启后 scrollback 丢失** — PTY 进程退出时不保存 scrollback，仅在 `on_stop()` 调用时才持久化
2. **多客户端不同尺寸导致输出混乱** — PTY 缩小后内容被重排，大窗口客户端看到错乱输出

> 服务器 graceful shutdown 阻塞问题已移至 `bugfix-graceful-shutdown.md` 独立迭代。

## Bug 1：Scrollback 持久化缺失

### 症状

服务器重启后，终端面板没有历史输出，显示空白终端。

### 根因分析

Scrollback 数据存在两个位置，仅在 `on_stop()` 时同步：

```
内存: TerminalManager._sessions[term_id]._scrollback  ← PTY 输出实时追加
磁盘: TerminalSession.scrollback_b64                   ← 仅 on_stop() 时写入
```

**丢失场景**：
- PTY 进程自然退出（用户 `exit`）→ 读线程结束 → scrollback 留在内存 → 没有触发 `on_stop()`
- 服务器异常退出（crash / kill -9）→ `_shutdown_cleanup()` 未执行 → scrollback 丢失

### 设计方案

分两步解决：读线程退出时拷贝 scrollback 到 session 对象，标记 dirty 触发定时持久化。

**Step 1：读线程退出时拷贝 scrollback**

在 `tm.create()` 时传入 `on_dead(term_id, scrollback_bytes)` 回调，由 `session_impl.py` 注册。读线程 finally 块中调用：

```python
# terminal.py reader thread finally block
finally:
    session.alive = False
    session.exit_code = exit_code
    if session._on_dead:
        session._on_dead(session.id, bytes(session._scrollback))
    self._notify_process_exit(term_id, exit_code)
```

`on_dead` 在读线程中执行，只做一次赋值（GIL 保护原子性），不调用 `_persist()`：

```python
# session_impl.py _terminal_on_create 中注册
def on_dead(term_id, scrollback):
    self.scrollback_b64 = base64.b64encode(scrollback).decode()
    sm.mark_dirty(self.id)
```

**Step 2：SessionManager 定时持久化**

新增 dirty 标记 + 定时写盘机制：

```python
class SessionManager:
    _dirty: set[str]  # dirty session IDs

    def mark_dirty(self, session_id: str) -> None:
        """标记 session 需要持久化（线程安全，任何线程可调用）。"""
        self._dirty.add(session_id)

    async def _persist_dirty_loop(self):
        """定时持久化 dirty sessions（event loop 中运行）。"""
        while True:
            await asyncio.sleep(5)
            dirty = self._dirty.copy()
            self._dirty.clear()
            for sid in dirty:
                session = self._sessions.get(sid)
                if session:
                    self._persist(session)
```

**线程安全**：
- `on_dead` 在读线程中只做 `scrollback_b64` 赋值 + `_dirty.add()`（均为 GIL 保护的原子操作）
- `_persist()` 在 event loop 线程中执行，与 `on_stop` 串行，无竞争
- `set.add()` 在 CPython 中是线程安全的

**与 shutdown 路径的一致性**：
- 正常关闭：`_shutdown_cleanup()` → `session_manager.stop(sid)` → `on_stop()` → `_persist()`（同现有路径）
- shutdown 前 PTY 先被 kill → 读线程退出 → `on_dead` 拷贝 scrollback → `on_stop` 中 `tm.get_scrollback()` 已无数据但 `scrollback_b64` 已就绪 → `_persist()` 写盘
- PTY 自然退出 + 服务器未关闭 → `on_dead` 拷贝 + `mark_dirty` → 5 秒内写盘
- 服务器 crash → 最多丢失最近 5 秒内未持久化的 dirty sessions


## Bug 2：多客户端尺寸输出混乱

### 症状

同一 PTY 被两个不同大小的终端面板连接时，大窗口的输出混乱（行错位、内容重排）。

### 根因分析

当前策略取所有客户端的**最小尺寸**设为 PTY 大小。当小窗口连接时，PTY 缩小，shell 收到 `SIGWINCH` 重绘输出——这些重绘内容以小尺寸格式发给所有客户端，大窗口看到的是为小尺寸格式化的内容。

```
Client A: 30 rows × 120 cols  ← PTY 原本 120 cols
Client B: 20 rows × 80 cols   ← 连接后 PTY 缩为 80 cols
                                  Client A 收到 80 cols 格式的输出 → 混乱
```

这与 tmux 的行为不同。tmux 的默认行为：PTY 取最小尺寸，但大窗口客户端的**多余区域用点（·）填充**，终端内容限制在左上角。用户看到的是一个小终端 + 灰色填充区域，不会混乱。

### 设计方案

**前端 FitAddon 正常工作 + 服务端广播实际 PTY 尺寸 + 前端覆盖**

1. FitAddon 正常计算，每个客户端上报自己的期望 rows/cols
2. 服务端取最小值设 PTY，然后广播 `pty_resize` JSON 消息给所有客户端
3. 前端收到 `pty_resize` 后，调用 `term.resize(pty_rows, pty_cols)` 匹配 PTY 实际尺寸
4. xterm 容器 CSS 保持面板原始大小，多余空间为终端背景色
5. 面板 resize 时 FitAddon 重新计算 → 上报 → 服务端广播 → 客户端 `term.resize()`

**需要注意的回环抑制**：`term.resize()` 会触发 xterm `onResize` 事件，需要用标志位抑制服务端发起的 resize 触发二次上报。

**广播时机**：
- 客户端发送 `resize` 后 → 服务端 `tm.resize()` → 广播实际尺寸
- 客户端 detach 后 → 重新计算最小尺寸 → 广播实际尺寸

## 关键参考

### 源码
- `src/mutbot/runtime/terminal.py` — 读线程（L204-267）、`resize()`（L371-395）、`_apply_min_size()`（L397-404）
- `src/mutbot/runtime/session_impl.py` — `_terminal_on_stop()`（L236-250）、scrollback 持久化
- `src/mutbot/web/routes.py` — `_attach_terminal_channel()`（L1472-1530）、resize handler（L1298-1307）
- `frontend/src/panels/TerminalPanel.tsx` — `sendResize()`（L88-92）、FitAddon 使用

### 相关规范
- `docs/design/terminal.md` — 终端功能设计（多客户端支持章节）
- `docs/specifications/refactor-terminal-protocol.md` — 终端协议优化（刚完成）

## 实施步骤清单

### Bug 1：Scrollback 持久化缺失

- [x] **Task B1.1**: `terminal.py` — 新增 `_on_dead` 回调
  - [x] `TerminalSession` 新增 `_on_dead: Callable[[str, bytes], None] | None` 字段
  - [x] `create()` 接受 `on_dead` 参数，赋给 session
  - [x] 读线程 finally 块中调用 `session._on_dead(session.id, scrollback_copy)`
  - 状态：✅ 已完成

- [x] **Task B1.2**: `session_impl.py` — 注册 on_dead + dirty 持久化
  - [x] `_terminal_on_create` 中构建 `on_dead` 闭包，传入 `tm.create()`
  - [x] `SessionManager.__init__` 新增 `_dirty: set[str]`
  - [x] `SessionManager.mark_dirty()` — 线程安全标记
  - [x] `SessionManager._persist_dirty_loop()` — 每 5 秒持久化 dirty sessions
  - 状态：✅ 已完成

- [x] **Task B1.3**: `server.py` — 启动 persist loop
  - [x] lifespan 中 `asyncio.create_task(session_manager._persist_dirty_loop())`
  - [x] shutdown 时 cancel persist_dirty_task
  - 状态：✅ 已完成

### Bug 2：多客户端尺寸输出混乱

- [x] **Task B2.1**: 后端 — `terminal.py` resize 返回实际 PTY 尺寸
  - [x] `resize()` 返回 `tuple[int, int] | None`（实际 PTY rows/cols），无 session 或未存活时返回 `None`
  - [x] `_apply_min_size()` 返回 `tuple[int, int]`
  - [x] `detach()` 返回 `tuple[int, int] | None`
  - 状态：✅ 已完成

- [x] **Task B2.2**: 后端 — `routes.py` 广播 `pty_resize`
  - [x] `_handle_channel_json` resize 处理：调用 `tm.resize()` 后，获取实际尺寸，广播 `pty_resize` 给该 session 所有 channel
  - [x] `_detach_terminal_channel`：detach 后广播更新后的 PTY 尺寸给剩余 channel
  - [x] 新增 `_broadcast_pty_resize()` 辅助函数
  - 状态：✅ 已完成

- [x] **Task B2.3**: 前端 — `TerminalPanel.tsx` 处理 `pty_resize`
  - [x] `handleJsonMessage` 新增 `pty_resize` 处理：调用 `term.resize(cols, rows)`
  - [x] 回环抑制：用 `serverResizing` 标志位抑制服务端发起的 resize 触发 `sendResize`
  - 状态：✅ 已完成

- [x] **Task B2.4**: 更新 `docs/design/terminal.md`
  - [x] 多客户端支持章节增加 PTY 尺寸广播说明
  - [x] 协议章节增加 `pty_resize` JSON 消息
  - 状态：✅ 已完成

- [x] **Task B2.5**: 构建验证
  - [x] 前端构建通过
  - 状态：✅ 已完成
