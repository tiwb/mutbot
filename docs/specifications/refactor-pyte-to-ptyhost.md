# pyte 下沉到 ptyhost 设计规范

**状态**：✅ 已完成
**日期**：2026-03-15
**类型**：重构

## 背景

### 问题

mutbot server 重启后，pyte 屏幕状态丢失。首次客户端连接时需从 ptyhost 获取 scrollback（raw bytes）并重新 feed 给 pyte 重建状态。实测数据：

- scrollback 大小：10MB（已达上限）
- pyte feed 耗时：28.5 秒
- 总连接耗时：28.7 秒

用户在开发 mutbot 时需要频繁重启 server，每次重启后等待近 30 秒才能看到终端内容，严重影响开发体验。

### 根因

数据源（PTY 进程）和状态机（pyte Screen）分属不同进程：

```
ptyhost 进程                         mutbot 进程
┌─────────────────┐                ┌──────────────────┐
│ PTY process     │                │ pyte Screen      │
│ scrollback 10MB │  ──WebSocket──→│ HistoryScreen    │
│ (raw bytes)     │                │ Stream + Decoder  │
└─────────────────┘                └──────────────────┘
```

mutbot 重启 → pyte 丢失 → 必须从 ptyhost 拉 10MB raw bytes 重建。这不是性能问题，是**职责错位**。

### 为什么 scrollback 这么大

ptyhost 存储的是 raw PTY 输出字节流。Claude Code 的行为特殊：每次全量重绘（resize、工具调用结束）输出整个对话历史（250-450KB），包含大量重复的 ANSI 控制序列。这些冗余数据在 raw bytes 层面无法压缩，但经过 pyte 消化后只是屏幕状态的增量更新。

**pyte 本质上是一个压缩器**——把冗余字节流压缩为语义化的屏幕状态。当前架构把这个压缩器放在了错误的位置。

## 设计方案

### 核心思路

将 pyte 从 mutbot 移到 ptyhost，让数据源和状态机在同一进程。

```
ptyhost 进程
┌──────────────────────────────────┐
│ PTY + pyte HistoryScreen         │  ← 数据源和状态机共存
│ 渲染定时器 (80ms)                │  ← feed + render 零延迟
│ (HistoryScreen = scrollback)     │  ← raw bytearray 彻底消除
└────────────┬─────────────────────┘
             │ ANSI 帧（KB 级）
mutbot 进程（转发 + 连接管理）
┌────────────┴─────────────────────┐
│ 转发 ANSI 帧给所有客户端          │
│ 转发 scroll/resize 命令给 ptyhost │
│ resize 控制权判断                 │
└────────────┬─────────────────────┘
             │
前端（无状态）
┌────────────┴─────────────────────┐
│ xterm.js (scrollback=0)          │
│ 收 ANSI 帧 → term.write()       │
│ 发 scroll/resize 命令            │
└──────────────────────────────────┘
```

### 消除的内容

| 消除项 | 说明 |
|--------|------|
| `_scrollback: bytearray` (10MB) | pyte HistoryScreen 替代 |
| scrollback replay (28.5 秒) | `get_snapshot()` 返回几 KB |
| base64 编码传输 10MB | 不再需要 |
| mutbot 侧 pyte 实例 | 全部移到 ptyhost |
| mutbot 侧渲染定时器 | 移到 ptyhost |

### ptyhost 协议变更

#### 输出格式变更

| 现在 | 改后 |
|------|------|
| binary frame: `[term_id 16B][raw PTY bytes]` | binary frame: `[term_id 16B][view_id 8B][ANSI frame]` |

ptyhost 不再转发 raw bytes，而是在内部 feed pyte、计算 dirty diff、渲染为 ANSI 帧后推送。帧已包含 BSU/ESU (Synchronized Update) 包裹。每帧带 view_id 标识，mutbot 据此路由到对应客户端。

#### View 抽象

引入 **TermView** 作为核心抽象——一个 view 是对终端屏幕+历史的一个视口，拥有独立的滚动位置。

```python
class TermView:
    id: str           # view_id
    term_id: str
    scroll_offset: int = 0   # 相对于 bottom 的偏移。0 = live，>0 = scrolled
```

一个终端可以有多个 view。所有 view 共享同一个 pyte HistoryScreen（屏幕状态是终端的客观事实），但各自的 scroll_offset 独立。

**offset 模型：bottom-relative**

scroll_offset 是相对于底部（当前 screen.buffer 最后一行）的行偏移。offset=0 表示看实时屏幕，offset=N 表示从底部往上 N 行。

选择 bottom-relative 的原因：
- `scroll_to_bottom` 天然是 offset=0，无需知道 history 总长度
- 用户大部分时间在 offset=0（live），这是最常见路径
- 向上滚动后返回底部总是一步到位

已知局限：当用户处于 scrolled 状态（offset>0）时，如果 Claude Code 做全量重绘推入大量新行，offset 指向的内容会发生变化（底部移动了但 offset 数值不变）。这与当前实现行为一致。实际影响有限——用户大多在输出稳定后才翻看历史，且 scrolled view 在用户不操作时冻结（不推送 dirty diff），只在下次 scroll 命令时才重新渲染。

**Phase 1**：mutbot 为每个终端创建一个 view，所有客户端共享。同步滚动，行为与当前一致。
**Phase 2 扩展**：mutbot 为每个客户端创建独立 view。per-client 滚动，ptyhost 代码无需修改。

#### 命令变更

| 命令 | 变更 | 说明 |
|------|------|------|
| `create` | 扩展 | 创建 PTY + pyte Screen + Stream + Decoder |
| `resize` | 扩展 | resize PTY + pyte Screen |
| `scrollback` | **删除** | 被 view snapshot 替代 |
| `kill` | 扩展 | 清理 pyte 实例和所有 view |
| `create_view` | **新增** | `{cmd: "create_view", term_id}` → `{view_id}` — 创建视图（初始 offset=0） |
| `destroy_view` | **新增** | `{cmd: "destroy_view", view_id}` — 销毁视图 |
| `snapshot` | **新增** | `{cmd: "snapshot", view_id}` → binary — 返回该 view 当前可见内容的 ANSI 帧 |
| `scroll` | **新增** | `{cmd: "scroll", view_id, lines: int}` — 滚动指定 view |
| `scroll_to_bottom` | **新增** | `{cmd: "scroll_to_bottom", view_id}` — view 回到 live |

#### 渲染规则

- **live view**（offset=0）：渲染定时器计算 dirty diff，推送给所有 live view。`render_dirty()` 只算一次，N 个 live view 共享同一帧。
- **scrolled view**（offset>0）：不接收 dirty diff。仅在 `scroll` 命令时按需渲染（`render_scrolled()`），推送给该 view。
- **scroll_to_bottom**：offset 归零，推送一次全屏快照使 view 衔接到 live 状态。

### ptyhost 内部架构

#### 新增模块

从 mutbot 迁移以下内容到 ptyhost：

- `ansi_render.py` → `ptyhost/ansi_render.py`（render_dirty / render_full / render_lines）
- `_SafeHistoryScreen` 类 → `ptyhost/_screen.py`

#### TerminalProcess 扩展

```python
@dataclass
class TerminalProcess:
    # 现有字段...
    id: str
    rows: int
    cols: int
    process: Any = None
    reader_thread: threading.Thread | None = None
    alive: bool = True
    exit_code: int | None = None
    _fd: int | None = None

    # 删除：_scrollback / _scrollback_lock

    # 新增：pyte 相关
    screen: _SafeHistoryScreen | None = None
    stream: pyte.Stream | None = None
    decoder: codecs.IncrementalDecoder | None = None
    views: dict[str, TermView] = field(default_factory=dict)
```

#### 渲染管线

```
PTY reader thread → _on_output(term, data)
  → 投递到事件循环 (call_soon_threadsafe)
  → feed pyte (decoder.decode + stream.feed)
  → 标记 render_pending

渲染定时器 (80ms)
  → 检查 pending
  → flush 剩余 buffer
  → dirty_frame = render_dirty(screen)
  → for each live view (offset==0): push (term_id, view_id, dirty_frame)
```

**线程安全**：PTY reader 在独立线程中运行，pyte feed 和 render 需要同步。可选方案：
- 方案 A：reader 线程直接 feed pyte（加锁），渲染在主线程
- 方案 B：reader 线程把 data 放入 queue，主线程统一 feed + render

当前 mutbot 的方案是 B（`_output_buffers` + 事件循环调度），建议 ptyhost 沿用。

### mutbot 侧变更

#### TerminalManager 简化

删除以下内容：

- `_screens`、`_streams`、`_decoders` 字典
- `_render_handles`、`_render_pending` 字典
- `_feed_pyte()`、`_render_frame()`
- `_scroll_offsets`、`scroll_terminal()`、`scroll_to_bottom()`、`_render_scrolled()`
- `ansi_render.py` 的 import

保留/修改：

- **`_on_pty_output`**：收到的已经是带 view_id 的 ANSI diff frame，按 view_id 路由到对应客户端（删除 flush 定时器、pyte feed、render 逻辑）
- **`on_connect`**：调用 `create_view()` 获得 view_id（Phase 1 复用已有 view），再调用 `snapshot(view_id)` 得到几 KB ANSI 帧发送给客户端
- **`resize`**：发送 resize 命令给 ptyhost（ptyhost 内部同步 resize PTY + pyte），删除本地 pyte resize
- **scroll 处理**：转发 scroll 命令（带 view_id）给 ptyhost，ptyhost 返回渲染帧，mutbot 转发给对应客户端

#### PtyHostClient 扩展

新增方法：

```python
async def create_view(self, term_id: str) -> str: ...       # → view_id
async def destroy_view(self, view_id: str) -> None: ...
async def get_snapshot(self, view_id: str) -> bytes: ...
async def scroll(self, view_id: str, lines: int) -> None: ...
async def scroll_to_bottom(self, view_id: str) -> None: ...
```

删除方法：

```python
async def get_scrollback(self, term_id: str) -> bytes: ...  # 替换为 get_snapshot
```

### 前端变更

无。前端已经在接收 ANSI 帧并 `term.write()`，scroll 命令格式不变。

### 滚动行为

Phase 1 为**全局同步滚动**——mutbot 为每个终端创建一个 view，所有客户端共享该 view。行为与当前实现一致。

任何客户端发送 scroll 命令 → mutbot 转发给 ptyhost（带共享 view_id）→ ptyhost 渲染该 view 的视图 → 推送帧 → mutbot 按 view_id 路由给所有客户端。

Phase 2 扩展为 per-client 滚动时，mutbot 为每个客户端 `create_view`，各自持有独立 view_id。ptyhost 的代码和协议无需任何修改。

### ptyhost 异步架构考量

当前 ptyhost 使用 ASGI + uvicorn，具备事件循环。但核心 `TerminalManager` 的设计是同步的（`_on_output` 回调在 reader 线程中）。

渲染定时器需要在事件循环中调度。方案：

- reader 线程将 PTY 数据放入 `asyncio.Queue`
- 主事件循环消费 queue → feed pyte → 标记 pending
- `asyncio.call_later(0.08, render_frame)` 实现 80ms 渲染定时器
- 渲染帧通过 WebSocket 广播

这与当前 mutbot 侧 TerminalManager 的架构一致，是代码迁移而非重新设计。

## 待定问题

### QUEST Q1: ptyhost 的事件循环集成
**问题**：当前 ptyhost 的 `_on_output` 在 reader 线程中直接广播（`_app.py:187-194`）。迁入 pyte 后需要在事件循环中做 feed + render。如何将线程回调桥接到事件循环？
**建议**：使用 `loop.call_soon_threadsafe()` 将数据从 reader 线程投递到事件循环，事件循环中统一处理 feed、buffer、render。这是 mutbot 侧 TerminalManager 已验证的模式。

### QUEST Q2: ansi_render.py 的归属
**问题**：`ansi_render.py` 迁移到 ptyhost 后，mutbot 是否还需要保留副本？
**建议**：不保留。mutbot 不再做任何渲染。如果未来有其他模块需要渲染能力（如测试工具），可以将其提取为共享模块。

### QUEST Q3: ptyhost 空闲退出与 pyte 状态
**问题**：ptyhost 当前有 60 秒空闲自动退出机制（`_app.py:211-235`）。所有 mutbot 断开后 60 秒退出。重启后 pyte 状态丢失。是否需要调整？
**建议**：保持现有行为。ptyhost 空闲退出意味着没有 mutbot 连接也没有终端进程，pyte 状态已无意义。下次启动时从零开始。

## 已发现问题

### pyte resize bug（上游 bug）

**已确认**：pyte `Screen.resize()` 收缩行数时光标位置不正确。

**根因**：`resize()` 先执行 `delete_lines` + `restore_cursor`（此时 `self.lines` 仍为旧值），后更新 `self.lines`。`restore_cursor` → `ensure_vbounds` 用旧 lines 值判断边界，光标未被 clamp 到新范围内。

**症状**：resize 40→27→40 后，内容在 row 0-25，光标在 row 38，中间大段空白。用户看到"历史向上移了一段距离"。

**修复**：在 `_SafeHistoryScreen` 中覆写 `resize`，在调用 `super().resize()` 后用新 lines 值 clamp 光标：`self.cursor.y = min(self.cursor.y, self.lines - 1)`。迁移到 ptyhost 时一并修复。**已修复**（2026-03-16，直接在 mutbot 侧 `_SafeHistoryScreen` 中修复）。

### Claude Code resize 清屏行为

**已确认**：Claude Code 在 resize 时会发送清屏指令。在 PC 原生终端中，resize 后屏幕被清除，历史不可见。在 mutbot 中，结合 pyte resize bug，可能产生错误/混乱/重复的历史内容。

## 实施步骤清单

### Phase 1: ptyhost 新增 pyte 基础设施 [✅ 已完成]

- [x] **Task 1.1**: 迁移 `_SafeHistoryScreen` 到 `ptyhost/_screen.py`
  - 从 `terminal.py` 提取 `_SafeHistoryScreen` 类（含 resize bug fix）
  - 新建 `ptyhost/_screen.py`，包含 `_SafeHistoryScreen` + `TermView` dataclass
  - 状态：✅ 已完成

- [x] **Task 1.2**: 迁移 `ansi_render.py` 到 `ptyhost/`
  - 复制 `runtime/ansi_render.py` → `ptyhost/ansi_render.py`
  - 验证 import 路径正确（pyte 依赖）
  - 状态：✅ 已完成

- [x] **Task 1.3**: 扩展 `TerminalProcess`，集成 pyte
  - 删除 `_scrollback` / `_scrollback_lock` 字段
  - 新增 `screen` / `stream` / `decoder` / `views` 字段
  - `create()` 时初始化 pyte Screen + Stream + Decoder
  - `kill()` 时清理 pyte 实例和所有 view
  - 状态：✅ 已完成

### Phase 2: ptyhost 渲染管线 [✅ 已完成]

- [x] **Task 2.1**: 输出缓冲与 pyte feed
  - reader 线程 `_on_output` 改为通过 `call_soon_threadsafe` 投递到事件循环
  - 事件循环中：decoder.decode + stream.feed + 标记 render_pending
  - 引入 per-terminal output buffer（复用 mutbot 侧 `_output_buffers` 模式）
  - 状态：✅ 已完成

- [x] **Task 2.2**: 渲染定时器
  - 80ms 渲染定时器（`asyncio.call_later`）
  - 检查 pending → flush buffer → `render_dirty(screen)` → 推送给所有 live view
  - 输出帧格式：`[term_id 16B][view_id 8B][ANSI frame]`
  - 状态：✅ 已完成

### Phase 3: ptyhost View 命令 [✅ 已完成]

- [x] **Task 3.1**: `create_view` / `destroy_view` 命令
  - TerminalManager 新增 `create_view(term_id)` → view_id、`destroy_view(view_id)`
  - `_app.py` 添加命令处理
  - 状态：✅ 已完成

- [x] **Task 3.2**: `snapshot` 命令
  - 根据 view 的 offset 渲染当前可见内容（offset=0 用 `render_full`，offset>0 用 `render_lines` 组装历史行）
  - 返回 binary ANSI 帧
  - 状态：✅ 已完成

- [x] **Task 3.3**: `scroll` / `scroll_to_bottom` 命令
  - `scroll(view_id, lines)` — 更新 offset，按需渲染推送
  - `scroll_to_bottom(view_id)` — offset 归零，推送全屏快照衔接 live
  - 删除 `scrollback` 命令
  - 状态：✅ 已完成

- [x] **Task 3.4**: `resize` 命令扩展
  - resize 时同步 resize pyte Screen（已含 cursor clamp fix）
  - 标记全屏 dirty + render_pending
  - 状态：✅ 已完成

### Phase 4: PtyHostClient 适配 [✅ 已完成]

- [x] **Task 4.1**: 新增客户端方法
  - `create_view()` / `destroy_view()` / `get_snapshot()` / `scroll()` / `scroll_to_bottom()`
  - 删除 `get_scrollback()`
  - 状态：✅ 已完成

- [x] **Task 4.2**: 二进制帧解析适配
  - `_on_binary` 解析新格式：`[term_id 16B][view_id 8B][ANSI frame]`
  - 回调签名变更：`on_frame(term_id, view_id, frame)`
  - 状态：✅ 已完成

### Phase 5: mutbot TerminalManager 简化 [✅ 已完成]

- [x] **Task 5.1**: 删除 pyte 相关代码
  - 删除 `_screens` / `_streams` / `_decoders` / `_render_handles` / `_render_pending` 字典
  - 删除 `_feed_pyte()` / `_render_frame()` / `_render_scrolled()`
  - 删除 `_scroll_offsets` / `scroll_terminal()` / `scroll_to_bottom()`（改为转发）
  - 删除 `_SafeHistoryScreen` 类（已迁移）
  - `runtime/ansi_render.py` 保留（无引用，待确认后删除）
  - 状态：✅ 已完成

- [x] **Task 5.2**: 改造输出路径
  - `_on_pty_frame` 简化：收到 `(term_id, view_id, frame)` → 按 term_id 路由转发给对应客户端
  - 删除 flush 定时器、output buffer、pyte feed 逻辑
  - 状态：✅ 已完成

- [x] **Task 5.3**: 改造连接与滚动
  - `on_connect`：`create_view()` → `get_snapshot()` → 发送快照（删除 scrollback + pyte 重建逻辑）
  - scroll/scroll_to_bottom：转发给 ptyhost（带 view_id）
  - 状态：✅ 已完成

### Phase 6: 验证 [✅ 已完成]

- [x] **Task 6.1**: 单元测试通过
  - 489 tests passed（含更新后的 test_runtime_terminal.py）
  - 状态：✅ 已完成

- [x] **Task 6.2**: 功能验证
  - 终端基本交互（输入、输出、颜色、光标）
  - mutbot 重启后连接恢复（应在 1 秒内，而非 28.5 秒）
  - 滚动功能正常（上翻历史、回到底部）
  - resize 后屏幕正确
  - 多客户端同步
  - 状态：✅ 已完成

## 关键参考

### 源码

- `mutbot/src/mutbot/runtime/terminal.py` — 当前 pyte 集成（待迁出）
- `mutbot/src/mutbot/runtime/ansi_render.py` — ANSI 渲染器（待迁移）
- `mutbot/src/mutbot/ptyhost/_app.py` — ptyhost ASGI 应用（待扩展）
- `mutbot/src/mutbot/ptyhost/_manager.py` — PTY 进程管理（待扩展）
- `mutbot/src/mutbot/ptyhost/_client.py` — mutbot 侧客户端（待扩展）
- `mutbot/src/mutbot/ptyhost/__main__.py` — ptyhost 启动入口

### 相关规范

- `bugfix-terminal-rendering-flicker.md` — 闪烁修复（BSU/ESU 方案来源，✅ 已完成）
- `feature-pyte-frameskip-scroll.md` — 跳帧渲染与服务端滚动（当前架构，✅ 已实施）
- `feature-terminal-resize-control.md` — resize 控制权（不受本次重构影响）
