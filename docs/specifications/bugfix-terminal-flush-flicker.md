# 终端 flush 策略导致长历史闪烁 设计规范

**状态**：🔄 实施中（验证阶段）
**日期**：2026-03-17
**类型**：Bug修复

## 背景

`bugfix-terminal-rendering-flicker.md`（✅ 已完成）通过 pyte diff + BSU/ESU 解决了基础闪烁问题。但用户反馈：**当 Claude Code 聊天历史变长后，闪烁再次出现**。短历史时正常，随使用时间增长逐渐恶化。

### 已排除的因素

- **ConPTY 不传递 BSU/ESU**：已验证 BSU/ESU 确实到达 ptyhost（通过日志确认）
- **pyte 不识别 BSU/ESU**：已在 `_SafeHistoryScreen` 中实现 `synchronized` 标志
- **前端问题**：VS Code 使用相同技术栈（ConPTY + xterm.js）不闪烁，问题在 ptyhost

### 关键观察

1. **Claude Code 的 BSU/ESU 粒度**：ink 框架每次 render cycle 包裹一对 BSU/ESU，一次全量重绘包含多对 BSU/ESU（~10 对/次），每对间隔仅 ~0.2ms
2. **BSU/ESU 对总在同一个 flush batch 内完成**：`was_synchronized` 始终为 `False`，ptyhost 的 BSU/ESU 优化逻辑从未生效
3. **VS Code 不闪烁的原因**：PTY 在同一进程内，数据通过本地管道瞬时到达，xterm.js 在一个 animation frame 内处理完所有数据

## 设计方案

### 根因分析

ptyhost 的 flush 策略在长历史场景下导致大 burst 被拆分为多帧：

**旧策略**（首次数据后 16ms 必 flush + 32KB 立即 flush）：

```
Claude Code 全量重绘 200KB：
  chunk1(65KB) → buf=65KB > 32KB → 立即 FLUSH#1 → RENDER#1 ← 中间态！
  chunk2(65KB) → buf=65KB > 32KB → 立即 FLUSH#2 → RENDER#2 ← 中间态！
  chunk3(70KB) → buf=70KB > 32KB → 立即 FLUSH#3 → RENDER#3 ← 最终态
```

32KB 大小上限是闪烁的直接触发器。历史越长 → 重绘输出越大 → 越容易超过 32KB → 越多中间态渲染。

此外，pyte `stream.feed()` 是同步阻塞事件循环的。feed 大块数据时（5-10ms），reader 线程继续通过 `call_soon_threadsafe` 堆积回调，形成级联 flush。

### 修复方案：静默期 flush + 时间保底

**核心思路**：不再按大小强制 flush，而是等 PTY 输出停止后再 flush。用时间保底防止屏幕卡住。

**新策略**：

1. **静默期 16ms**：每次新数据到达**重置** 16ms 定时器。数据停止 16ms 后认为一次输出结束，触发 flush
2. **保底上限 300ms**：首次数据到达启动 300ms 保底定时器。超过 300ms 强制 flush，防止持续输出时屏幕卡住
3. **删除 32KB 大小上限**：不再因数据量触发提前 flush

```
Claude Code 全量重绘 200KB（burst 持续 ~50ms）：
  chunk1(65KB) → 启动16ms静默期 + 启动300ms保底
  chunk2(65KB) → 重置16ms静默期
  chunk3(70KB) → 重置16ms静默期
  （数据停止）
  16ms 后 → FLUSH(200KB 全部) → RENDER ← 一帧完整态 ✓
```

**对交互输入无影响**：单个按键回显是一个小 chunk，16ms 静默后自然 flush。

### 永久诊断日志

临时诊断日志已清理，保留以下永久日志点（`mutbot.ptyhost` logger）用于长期监控：

| 日志 | 级别 | 触发条件 | 用途 |
|------|------|----------|------|
| `Flush max delay` | WARNING | 300ms 保底触发 | 检测持续输出导致的强制 flush |
| `Large feed` | INFO | feed > 32KB | 检测大块数据进入 pyte |
| `Large frame` | INFO | 帧 > 4KB | 检测大帧渲染（可能引起闪烁） |

## 实施步骤清单

### Phase 1: 诊断日志 [✅ 已完成]

- [x] **Task 1.1**: 添加时序诊断日志
  - [x] reader 线程：每次读取的字节数、时间戳、BSU/ESU 计数
  - [x] 事件循环：数据到达时的缓冲区状态、burst 检测
  - [x] flush/render 路径：数据量、耗时、synchronized 状态
  - 状态：✅ 已完成

### Phase 2: flush 策略优化 [✅ 已完成]

- [x] **Task 2.1**: flush 定时器改为静默期模式
  - [x] `_on_data_from_pty` 每次新数据重置 16ms 定时器
  - 状态：✅ 已完成

- [x] **Task 2.2**: 32KB 大小上限替换为 300ms 时间保底
  - [x] 删除 `_FLUSH_MAX_BYTES`
  - [x] 新增 `_FLUSH_MAX_DELAY = 0.3` + `_flush_max_expired` 保底回调
  - [x] `_flush_and_feed` 被调用时清理保底定时器
  - [x] `kill()` 清理保底定时器
  - 状态：✅ 已完成

### Phase 3: 多 View resize 循环修复 [⏸️ 待开始]

Per-Client View + Per-View Viewport 重构后，引入了新的闪烁源：**多客户端 resize 无限循环**。

#### 根因

`853684c` 在前端 `pty_resize` handler 中加了 `fit()` 调用：

```tsx
// TerminalPanel.tsx - pty_resize handler
serverResizing = true;
termRef.current.resize(c, r);
serverResizing = false;
if (fitRef.current) {
  fitRef.current.fit();  // ← 问题所在
}
```

`fit()` 在 `serverResizing = false` 之后调用，触发 `sendResize` 把自己的容器尺寸发回服务器。当两个不同尺寸的客户端连接同一终端（Auto 模式，无 `last_input_client`），形成无限循环：

```
桌面(114×63) sendResize → PTY=114×63 → broadcast pty_resize
  → 手机收到 → fit() → sendResize(70×29) → PTY=70×29 → broadcast pty_resize
    → 桌面收到 → fit() → sendResize(114×63) → 循环（~250次/秒）
```

日志实证（`server-20260317_165452`）：

```
16:55:25,144  resize 2e1b4234: 70x29  (client=c56f33b4)
16:55:25,150  resize 2e1b4234: 114x63 (client=3229ea25)
16:55:25,175  resize 2e1b4234: 70x29  (client=c56f33b4)
16:55:25,180  resize 2e1b4234: 114x63 (client=3229ea25)
...  （~4ms 一次，持续数秒）
```

后果：每次 resize → pyte 全屏 dirty → 8.1KB × 4 views → send buffer overflow → 连接断开。

#### 分析：Auto Size 在多 View 下不再必要

Per-View Viewport 之前，PTY 只有一个尺寸、一个共享 view，Auto Size 让「最后打字的客户端」控制 PTY 尺寸是合理的。

Per-View Viewport 之后：
- 每个客户端有独立 viewport，PTY 大于容器时走 viewport 裁剪
- PTY 尺寸可以保持在最大客户端的尺寸，小屏客户端通过 viewport 看裁剪后的内容
- `resize()` 已经通过 `set_viewport(view_id, rows, cols)` 更新每个客户端的 viewport（`terminal.py:201-204`）
- **不同尺寸的客户端不应该再争夺 PTY 尺寸**

`fit()` 的原始意图是页面刷新后重新抢夺 PTY 尺寸，但 Per-View Viewport 已经处理了尺寸不匹配的情况，这行代码变成了纯粹的 bug 源。

#### 修复方案

前端 `pty_resize` handler 中移除 `fit()` 调用。`term.resize(c, r)` 保留（xterm.js 需要知道 PTY 实际尺寸以正确渲染滚动区域等），但不再回发 resize 请求。

- [ ] **Task 3.1**: 移除 `pty_resize` handler 中的 `fit()` 调用
  - [ ] 删除 `TerminalPanel.tsx` 中 `pty_resize` 分支的 `fit()` 及注释
  - 状态：⏸️ 待开始

- [ ] **Task 3.2**: 验证修复效果
  - [ ] 两个不同尺寸的浏览器同时连接，确认无 resize 循环
  - [ ] 日志中无密集 resize 记录
  - [ ] 单客户端 resize 仍正常工作
  - [ ] 页面刷新后终端显示正确（viewport 接管）
  - 状态：⏸️ 待开始

### Phase 4: 日志清理 [✅ 已完成]

- [x] **Task 4.1**: 清理诊断日志
  - [x] `Flush max delay` WARNING 永久保留
  - [x] 新增 `Large feed` INFO（feed > 32KB）
  - [x] 新增 `Large frame` INFO（帧 > 4KB）
  - [x] 删除临时逐条日志（`[READ]`、`[BURST END]`、`[BUF]`、`[FLUSH]`、`[RENDER]` 等）
  - 状态：✅ 已完成

## 关键参考

### 源码

- `mutbot/src/mutbot/ptyhost/_manager.py` — flush 策略（`_on_data_from_pty`、`_flush_max_expired`）
- `mutbot/src/mutbot/ptyhost/_screen.py` — `_SafeHistoryScreen`（`synchronized` 标志）
- `mutbot/src/mutbot/ptyhost/_app.py` — `_WebSocketLogHandler`（日志转发到 mutbot）
- `mutbot/frontend/src/panels/TerminalPanel.tsx:237-251` — `pty_resize` handler（fit() 循环源）
- `mutbot/src/mutbot/runtime/terminal.py:178-217` — `resize()` + `set_viewport` 逻辑

### 相关规范

- `bugfix-terminal-rendering-flicker.md` — 基础闪烁修复（BSU/ESU + pyte diff，✅ 已完成）
- `refactor-pyte-to-ptyhost.md` — pyte 下沉到 ptyhost（✅ 已完成）
- `feature-per-view-viewport.md` — Per-View Viewport（✅ 已完成，引入 resize 循环）

### 历史调查

- BSU/ESU 到达验证：recall 会话 `0cefd87d`，消息 #3-#15
- ink BSU/ESU 实现：`src/ink.tsx` 每次 render cycle 包裹一对 BSU/ESU（PR #866）
- xterm.js WriteBuffer 12ms 时间片 + RenderDebouncer rAF 合并
