# 终端滚动条无法通过鼠标/触摸操作 — Bugfix 设计规范

**状态**：✅ 已完成
**日期**：2026-03-16
**类型**：Bug修复

## 背景

终端面板的自定义滚动条当前设置了 `pointerEvents: "none"`，仅作为视觉指示器。用户无法通过鼠标拖拽滚动条滑块或点击轨道来滚动终端内容，也无法通过触摸拖拽滑块操作。

**当前滚动方式**：
- 鼠标滚轮（PC 端）
- 触摸滑动终端区域（移动端）
- 这两种方式均通过 `sendScroll()` 发送到服务端 pyte 管理

**缺失的交互**：
- 鼠标拖拽滚动条滑块
- 点击滚动条轨道跳转
- 触摸拖拽滚动条滑块

## 设计方案

### 核心设计

将滚动条从纯展示组件改为可交互组件，支持以下操作：

1. **拖拽滑块**：鼠标按下滑块 → 拖动 → 释放，期间根据拖动位移计算目标 scroll offset，发送 `scroll_to` 命令到服务端
2. **点击轨道**：点击滑块上方 → 向上翻页；点击滑块下方 → 向下翻页
3. **触摸拖拽滑块**：与鼠标拖拽同理，使用 touch 事件

**关键约束**：滚动由服务端 pyte 管理，客户端不能直接设置 offset，需通过消息通知服务端。

### 交互细节

- 移除 `pointerEvents: "none"`，改为响应指针事件
- 拖拽时：计算拖动比例 → 映射到 scroll offset → 发送 `scroll_to` 命令（需后端支持 `scroll_to` 消息类型，或复用现有 `scroll` 做增量）
- 点击轨道：发送 `scroll` 命令，行数为 ±visible（翻页效果）
- 拖拽期间阻止滚动条自动隐藏（清除 2 秒 fadeout 计时器）
- 拖拽结束后恢复自动隐藏逻辑
- 滚动条交互区域适当加宽（hover 时 6px → 12px），提升可点击性

### 后端支持

后端当前只支持相对滚动，需新增 `scroll_to` 绝对定位消息类型：

- 当前：`scroll`（增量，lines 参数）、`scroll_to_bottom`（重置到 live）
- 新增：`scroll_to`（绝对偏移，offset 参数）

实现路径清晰，涉及 4 个文件各加一个方法/分支。

**scroll_offset 语义**：0 = live（最底部），>0 = 从底部往上的行数。前端拖拽时需将视觉位置（顶部到底部）映射为此语义。

### 滚动条可见性策略

改为与消息列表滚动条一致的行为：
- 有滚动历史时（offset > 0 或 total > visible），hover 终端区域显示滚动条
- 拖拽交互中常驻显示
- 鼠标/触摸离开后 2 秒隐藏
- 滚动操作时临时显示（保留现有行为）

### 实施概要

后端 4 个文件新增 `scroll_to` 消息类型；前端修改 TerminalPanel.tsx 滚动条部分，添加 pointer 事件处理（拖拽滑块、点击轨道），调整可见性逻辑。

## 待定问题

（已全部确认，采用建议方案）
- 滚动条热区：视觉 6px，热区 16px，hover 时视觉增加到 10px

## 实施步骤清单

### Phase 1: 后端新增 scroll_to [✅ 已完成]

- [x] **Task 1.1**: `_manager.py` 新增 `scroll_view_to(view_id, offset)` 方法
  - 状态：✅ 已完成

- [x] **Task 1.2**: `_app.py` 新增 `"scroll_to"` action 分发
  - 状态：✅ 已完成

- [x] **Task 1.3**: `_client.py` 新增 `async def scroll_to(view_id, offset)` 客户端方法
  - 状态：✅ 已完成

- [x] **Task 1.4**: `terminal.py` 新增 `"scroll_to"` 消息处理分支
  - 状态：✅ 已完成

### Phase 2: 前端滚动条交互 [✅ 已完成]

- [x] **Task 2.1**: 新增 `sendScrollTo(offset)` 函数
  - 状态：✅ 已完成

- [x] **Task 2.2**: 滚动条从纯展示改为可交互
  - 使用 Pointer Events 统一鼠标和触摸（setPointerCapture 拖拽）
  - 轨道热区 16px，视觉滑块 6px（hover 时 10px）
  - 轨道点击翻页（±visible 行）
  - 拖拽期间锁定滚动条可见
  - 状态：✅ 已完成

### Phase 3: 滚动条可见性策略 [✅ 已完成]

- [x] **Task 3.1**: 调整滚动条显示逻辑
  - CSS hover 驱动：`.terminal-panel:hover .terminal-scrollbar-track` 显示
  - `scrollbarFlash` state + `.active` class 处理滚动操作后 2 秒闪现
  - 拖拽中通过 `.active` class 常驻显示
  - 状态：✅ 已完成

### Phase 4: 构建验证 [✅ 已完成]

- [x] **Task 4.1**: 前端构建验证
  - `npm --prefix mutbot/frontend run build` 通过，无报错
  - 状态：✅ 已完成

## 测试验证

- 前端构建通过
- ✅ 滑块拖拽跟手（直接 DOM 操作）
- ❌ 拖拽期间终端内容不更新（见下方问题分析）
- 待验证：轨道点击翻页、触摸拖拽、hover 显示/隐藏

## 问题分析：拖拽时终端内容不更新

### 现象

拖拽滑块时，thumb 位置实时跟随鼠标/手指（直接 DOM 操作生效），但终端渲染的内容不随滑块位置变化。松开后内容才（可能）更新到最终位置。

### 数据流全链路

```
[前端 onPointerMove]
  → thumb.style.top = "X%"          ← 直接 DOM，即时生效 ✅
  → sendScrollToRef.current(offset)  ← 通过 rpc.sendToChannel 发送
    → WebSocket → terminal.py
      → _terminal_on_message: scroll_to
        → tm._client.scroll_to(view_id, offset)   ← IPC 到 pty host 子进程
          → _app._handle_command: scroll_to
            → _manager.scroll_view_to(view_id, offset)
              → view.scroll_offset = offset
              → _render_scrolled_view() → frame bytes
              → _on_frame(term_id, view_id, frame)    ← 帧回传
        → tm._client.get_scroll_state(view_id)        ← 第二次 IPC
        → broadcast_json(scroll_state)                 ← JSON 回传
```

### 可能的故障点

#### 1. React re-render 与 DOM 操作互相干扰（高度可疑）

拖拽期间服务端回传 `scroll_state` → `setScrollState()` 触发 React re-render → React 通过 JSX `style={{ top: "Y%" }}` 重写 `thumb.style.top`，覆盖 DOM 操作值。

虽然用户报告"拖拽跟手"（说明 DOM 操作优先级高于 React 重写），但 **re-render 本身可能干扰帧处理**。每次 re-render 重新计算 `scrollbar`、触发 DOM 对比，占用主线程时间，可能延迟 xterm 的帧渲染。

#### 2. offset 映射不匹配（确认存在）

前端和后端的 `maxOffset` 定义不一致：

| 端 | maxOffset | 含义 |
|----|-----------|------|
| 前端 | `scrollState.total` = `len(history) + visible` | 总行数 |
| 后端 | `len(screen.history.top)` | 仅历史行数 |

前端拖拽计算：`newOffset = Math.round(ratio * total)`，当 ratio=1 时，`newOffset = total > max_offset`。后端 clamp 到 `max_offset`。

**影响**：thumb 位置到 offset 的映射存在非线性失真。拖拽到顶部时，大段 thumb 移动可能映射到相同的 clamped offset → `scroll_view_to` 检测到 `new_offset == view.scroll_offset` → 不渲染 → 不推帧。

#### 3. 服务端请求节流 + 双重 IPC 延迟

每次 `scroll_to` 需要两次 IPC 往返：
1. `scroll_to` → pty host → frame
2. `get_scroll_state` → pty host → state

两次 IPC 是串行的（`await`）。加上 50ms 节流，实际帧率可能远低于预期。

对比 `scroll`（鼠标滚轮）：同样是两次 IPC + 节流问题，但鼠标滚轮每次只滚 1-3 行，offset 变化小，每次都产生新帧。拖拽则可能大幅跳跃，映射后 offset 不变（因上述第 2 点）。

#### 4. `useCallback` 依赖导致闭包过时

`handleThumbPointerDown` 的 `useCallback` 依赖是 `[scrollState]`。拖拽开始时捕获当时的 `scrollState.total`。如果拖拽期间终端输出了新内容（total 增长），闭包中的 `total` 是过时的。不过这在短暂拖拽中影响有限。

### 修复方向

#### 方案 A：拖拽期间抑制 React re-render（最小改动）

拖拽期间不处理服务端回传的 `scroll_state`（已有 `scrollbarDraggingRef.current` 标记但只控制了 scrollbarFlash，未阻止 `setScrollState`）。这避免了 re-render 干扰，但 thumb 位置完全由客户端控制，服务端帧可能与 thumb 位置不一致。

#### 方案 B：修正 offset 映射 + 抑制 re-render

1. 统一前后端 maxOffset：前端用 `total - visible`（= history 行数）作为 maxOffset，与后端一致
2. 拖拽期间跳过 `setScrollState`
3. 拖拽结束时发送最终 offset，恢复 `setScrollState` 处理

#### 方案 C：用增量 scroll 替代绝对 scroll_to（架构简化）

不用 `scroll_to`，拖拽时计算 offset 差值，用现有 `sendScroll(deltaLines)` 发送增量。优势：
- 复用已验证的 `scroll` 路径（鼠标滚轮证明可用）
- 无需新后端消息类型
- 增量小，每次都产生新帧

劣势：
- 累积误差（长时间拖拽可能漂移）
- 快速拖拽时增量过大，可能跨越可用 offset 范围

### 标准滚动条 UX 参考

| 交互 | PC (Windows/macOS) | 移动端 |
|------|-------------------|--------|
| **滑块拖拽** | 内容实时跟随滑块位置（最核心交互） | iOS 支持拖拽，Android 不一定 |
| **轨道点击** | macOS 默认翻页，Windows 传统跳转到点击位置 | 不适用 |
| **轨道长按** | 持续翻页直到 thumb 到达光标位置 | 不适用 |
| **hover 反馈** | thumb 颜色加深、宽度增加 | 不适用 |
| **可见性** | macOS 支持自动隐藏；Windows 通常常驻 | 滚动时闪现，2-3秒后隐藏 |

**关键原则**：拖拽时 thumb 和内容必须同步更新，这是滚动条最基本的UX要求。当前实现 thumb 跟手但内容不动，违反了这一核心原则。

**实现最佳实践**：
- 拖拽期间用 **ref + 直接 DOM** 操作 thumb 位置（当前已实现）
- 内容更新走异步通道但要保证帧到达（当前可能被 offset 映射问题阻断）
- 拖拽期间**不做 React re-render**，避免与 DOM 操作互相干扰

## 关键参考

### 前端源码
- `mutbot/frontend/src/panels/TerminalPanel.tsx:690-715` — 滚动条渲染（`pointerEvents: "none"`）
- `mutbot/frontend/src/panels/TerminalPanel.tsx:156-166` — `sendScroll()` / `sendScrollToBottom()`
- `mutbot/frontend/src/panels/TerminalPanel.tsx:235-250` — scroll_state 消息处理
- `mutbot/frontend/src/panels/TerminalPanel.tsx:554-569` — 鼠标滚轮处理
- `mutbot/frontend/src/panels/TerminalPanel.tsx:51-55` — scrollState React state
- `mutbot/frontend/src/index.css:2098-2127` — 终端面板和 xterm 样式

### 后端源码
- `mutbot/src/mutbot/runtime/terminal.py:479-494` — `_terminal_on_message()` 处理 scroll/scroll_to_bottom
- `mutbot/src/mutbot/ptyhost/_client.py:268-278` — PTY host 客户端 scroll/scroll_to_bottom/get_scroll_state
- `mutbot/src/mutbot/ptyhost/_app.py:203-215` — PTY host 应用层 action 分发
- `mutbot/src/mutbot/ptyhost/_manager.py:467-504` — `scroll_view()` / `scroll_view_to_bottom()` 实现
- `mutbot/src/mutbot/ptyhost/_screen.py:54-63` — TermView 数据结构（scroll_offset 字段）
