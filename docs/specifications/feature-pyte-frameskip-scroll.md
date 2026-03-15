# pyte 跳帧渲染与服务端滚动 设计规范

**状态**：✅ 已实施
**日期**：2026-03-15
**类型**：功能设计

## 背景

### 问题根因（已验证）

Claude Code 在对话较长时，每次 resize 或 TUI 更新都会重新输出完整对话历史。实测数据：

- 一次 resize 触发 **~250KB** 输出（11.6 秒）
- 一次工具调用触发 **454KB** 输出（88 KB/s）
- 输出以纯文本 + SGR 颜色为主，无复杂光标定位
- 对话越长，重绘数据量越大

网络带宽不足以实时传输这些数据，导致前端分批接收、反复渲染 → 闪烁。

### 之前的尝试

1. **raw bytes + scrollback replay**：实时输出正确，但 scrollback 溢出截断后破坏终端状态
2. **pyte dirty diff**：pyte Screen 无法正确建模 Claude Code 的超屏输出，历史混乱
3. **当前状态**：已回退到 raw bytes 实时输出 + 不发送 on_connect 快照（空屏连接）

### 为什么选择 pyte 方案

- **跳帧能力**：454KB 数据 feed 完后只需发送一屏 diff（~5-10KB），根本性降低传输量
- **通用性**：适用于所有终端程序，不需要逐个适配
- **已有基础**：pyte 依赖已添加，ansi_render 模块已实现，TerminalManager 已有 pyte 集成框架

## 设计方案

### 核心架构

```
PTY 输出 → feed pyte HistoryScreen（维护完整屏幕 + 历史）
         → 渲染定时器（50-100ms）计算 dirty diff → 发送 ANSI 帧给前端
         → 多次 PTY flush 自然合并为一次渲染帧（跳帧）

前端 xterm.js（scrollback=0）只显示服务端发来的当前屏幕
前端触摸滚动 → 发 scroll 消息给后端 → pyte 翻页 → dirty diff → ANSI 帧
```

### 跳帧渲染器

将现有的 "每次 flush 立即广播" 改为 "feed + 延迟渲染"：

- `_flush_output`：feed pyte，标记 `_render_pending[term_id] = True`，不立即发送
- 独立渲染定时器（`_RENDER_INTERVAL = 80ms`）：检查 pending 标记，计算 dirty diff，广播 ANSI 帧，清空 dirty 和 pending
- 如果 80ms 内有多次 flush（Claude Code 重绘 454KB 分成多个 chunk），它们全部 feed 给 pyte，但只产生一帧 diff — 这就是跳帧

80ms 选择理由：人眼对 ~12fps 以上不感知闪烁，80ms ≈ 12.5fps。比 16ms 的 flush 间隔长，足够合并多次 flush。

### 滚动协议

前端 xterm.js 的 scrollback 设为 0，滚动完全由服务端控制。

**消息协议**：

| 方向 | type | 字段 | 说明 |
|------|------|------|------|
| 前端→后端 | `scroll` | `lines: int` | 正数=向上滚动（看历史），负数=向下 |
| 后端→前端 | （binary） | ANSI 帧 | dirty diff 渲染结果 |

后端收到 scroll 消息后：
- 直接从 `history.top` deque + `screen.buffer` 按行级偏移组装可见行
- `offset=0` 显示 live screen，`offset=N` 从底部上移 N 行
- 调用 `render_lines()` 渲染组装的行列表，直接广播（不经渲染定时器，避免延迟）

**滚动状态广播**：

每次滚动后广播 JSON 消息，前端据此渲染自定义滚动条：

```json
{"type": "scroll_state", "offset": 5, "total": 1000, "visible": 40}
```

前端据此渲染自定义滚动条（CSS overlay thin thumb，2s 自动淡出）。用户输入时自动 scroll_to_bottom。

### 前端滚动改造

当前触摸滚动实现（`TerminalPanel.tsx:375-510`）：
- touchmove → 计算速度 → `term.scrollLines(lines)` → xterm 内部滚动
- touchend → 惯性动画 → 逐帧 `scrollLines()`

改造为：
- touchmove → 计算 deltaY → `sendScroll(-lines)` → 发消息给后端（取反：上滑=正 deltaY→负 lines=向下滚）
- touchend → 惯性动画 → 逐帧 `sendScroll(-lines)`
- 后端返回渲染帧 → `term.write(frame)` 更新显示

惯性滚动的流畅性取决于 RTT。局域网 1-5ms 无感，远程可能需要客户端预测（后续优化，不在本次范围）。

**PC 端滚动**：wheel 事件在捕获阶段（`capture: true`）拦截并 `stopPropagation`，阻止 xterm.js 将滚轮转为鼠标序列。`sendScroll(-lines)` 取反发送。

### xterm.js 配置变更

```typescript
const term = new Terminal({
  scrollback: 0,        // 禁用 xterm 内部 scrollback
  // 其他配置不变
});
```

### pyte HistoryScreen 配置

```python
screen = _SafeHistoryScreen(cols, rows, history=50000, ratio=0.001)
```

- `_SafeHistoryScreen`：继承 `pyte.HistoryScreen`，简化 `after_event` 避免 dict 迭代 RuntimeError
- `history=50000`：top/bottom 各 25000 行，足够长对话
- `ratio=0.001`：极小值（不使用 `prev_page/next_page`，滚动通过直接读取 `history.top` 实现行级精确控制）

### resize 处理

resize 时 pyte Screen 需要同步 resize。Claude Code 会在 resize 后重新输出所有内容，pyte 自然更新。

对于普通程序，resize 前应将当前可见行备份到 history（pyte 默认 resize 不保护历史）：

```python
async def resize(self, term_id, rows, cols, client_id=None):
    screen = self._screens.get(term_id)
    if screen:
        # resize 前保存可见行到 history top
        for i in range(screen.lines):
            screen.history.top.append(screen.buffer[i].copy())
        screen.resize(rows, cols)
    result = await self._client.resize(term_id, rows, cols)
    return result
```

### on_connect 快照

连接/重连时，用 `render_full(screen)` 发送当前屏幕快照。如果 pyte 实例不存在（服务器重启后），**先**从 ptyhost 获取 scrollback 并 feed 给 pyte，**再** attach 注册回调，避免 scrollback 和实时数据重叠导致内容重复。

### 数据流总结

```
实时输出：
  PTY → _on_pty_output → 缓冲 16ms → _flush_output → feed pyte → 标记 pending
  渲染定时器 80ms → 检查 buffer 是否为空（涌入检测）→ dirty diff → ANSI 帧 → 广播

滚动：
  前端 touch/wheel → scroll 消息 → 后端 history.top + buffer 行级组装 → render_lines → 广播

连接：
  on_connect → 获取 scrollback → 创建 pyte + feed → attach → render_full → 屏幕快照

resize：
  前端 resize → 后端 resize PTY + pyte Screen → Claude Code 重绘 → pyte feed → 跳帧渲染
```

## 关键参考

### 源码

- `mutbot/src/mutbot/runtime/terminal.py` — TerminalManager（`_flush_output`, `_on_pty_output`, `resize`, `attach`）
- `mutbot/src/mutbot/runtime/ansi_render.py` — `render_dirty()`, `render_full()`, `render_lines()`
- `mutbot/frontend/src/panels/TerminalPanel.tsx` — 触摸滚动（375-510），xterm 配置（87-122），binary 数据处理
- `mutbot/frontend/src/mobile/TerminalInput.tsx` — 移动端输入

### pyte API

- `pyte.HistoryScreen(cols, rows, history, ratio)` — 带历史的虚拟终端
- `screen.dirty` — 变化行集合（set of row indices）
- `screen.history.top` — 滚出屏幕顶部的历史行（deque）
- `screen.history.position` — 当前历史位置
- `prev_page()` / `next_page()` — 历史翻页

### 实测数据

- resize 触发 ~250KB 输出（18-24 KB/s，11.6秒）
- 工具调用触发 454KB 输出（88 KB/s，5秒）
- 当前 flush 间隔 16ms，每次 flush 数据量小（几十到几百字节）
- 控制序列：alt_screen=0, clear=0, home=0, pos=0, erase=0（纯文本+SGR）

### 已知问题

- **移动端部分输入法中文输入失效**：pyte 接管渲染后，某些输入法的 IME composition 与 xterm.js 隐藏 textarea 不兼容，无法输入中文（英文正常）。换用其他输入法可正常工作。原因可能是 pyte 渲染帧中的光标定位序列（`\x1b[row;colH`）或光标隐藏/显示（`\x1b[?25l/h`）干扰了 IME 组合过程。改动前（raw PTY 直传 xterm.js）该输入法可正常工作。

- **PC 端鼠标滚轮行为待验证**：Claude Code 启用终端鼠标模式后，xterm.js 会将滚轮事件翻译为鼠标序列发给 PTY，导致滚轮变成"翻历史输入"而非滚动终端。已添加 `capture: true` + `stopPropagation` 在捕获阶段拦截 wheel 事件，但尚未在 PC 端实测验证。

- **大屏幕刷新仍可能闪烁**：已实施"数据涌入时推迟渲染"策略（render 时检查 output_buffer 非空则跳过），但效果待实际使用中验证和调优。

### 相关规范

- `feature-terminal-resize-control.md` — resize 控制权（已实施）
- `bugfix-terminal-rendering-flicker.md` — 闪烁问题原始分析
- `feature-server-side-virtual-terminal.md` — 多客户端视口（后续）
