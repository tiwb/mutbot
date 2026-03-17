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

### 诊断日志

实施过程中添加了时序诊断日志（`mutbot.ptyhost.diag` logger），用于验证 flush 策略的效果：

| 日志标签 | 触发条件 | 长期保留 |
|----------|----------|----------|
| `[READ #N]` | 每次 PTY 读取 | 否（验证后删除） |
| `[BURST END]` | burst 结束时 | 有条件保留（仅大 burst） |
| `[BUF +NB]` | 每次数据到达 | 否 |
| `[FLUSH]` | 每次 flush | 否 |
| `[FLUSH MAX]` | 300ms 保底触发 | 是（关键信号） |
| `[RENDER]` | 每次渲染 | 否 |
| `[RENDER_TIMER]` | 渲染定时器触发 | 否 |

验证完成后清理：保留 `[FLUSH MAX]` 和有条件的 `[BURST END]`，删除其余逐条日志。

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

### Phase 3: 验证 [⏸️ 待开始]

- [ ] **Task 3.1**: 基础功能验证
  - [ ] 终端交互响应正常（输入不卡顿）
  - [ ] Claude Code spinner 动画正常
  - [ ] resize 后屏幕正确
  - 状态：⏸️ 待开始

- [ ] **Task 3.2**: 闪烁验证
  - [ ] 积累长历史后触发 Claude Code 全量重绘
  - [ ] 观察是否仍有闪烁
  - [ ] 日志中确认：大 burst 被完整 flush 而非拆分
  - 状态：⏸️ 待开始

- [ ] **Task 3.3**: 保底机制验证
  - [ ] 确认正常使用中 `[FLUSH MAX]` 不被触发
  - [ ] 确认极端持续输出场景（>300ms）保底机制生效
  - 状态：⏸️ 待开始

### Phase 4: 日志清理 [⏸️ 待开始]

- [ ] **Task 4.1**: 清理诊断日志
  - [ ] 保留 `[FLUSH MAX]`（WARNING 级别）
  - [ ] `[BURST END]` 加条件（仅 total > 1KB 或 chunks > 2）
  - [ ] 删除其余逐条日志
  - 状态：⏸️ 待开始

## 关键参考

### 源码

- `mutbot/src/mutbot/ptyhost/_manager.py` — flush 策略（`_on_data_from_pty`、`_flush_max_expired`）
- `mutbot/src/mutbot/ptyhost/_screen.py` — `_SafeHistoryScreen`（`synchronized` 标志）
- `mutbot/src/mutbot/ptyhost/_app.py` — `_WebSocketLogHandler`（日志转发到 mutbot）

### 相关规范

- `bugfix-terminal-rendering-flicker.md` — 基础闪烁修复（BSU/ESU + pyte diff，✅ 已完成）
- `refactor-pyte-to-ptyhost.md` — pyte 下沉到 ptyhost（✅ 已完成）

### 历史调查

- BSU/ESU 到达验证：recall 会话 `0cefd87d`，消息 #3-#15
- ink BSU/ESU 实现：`src/ink.tsx` 每次 render cycle 包裹一对 BSU/ESU（PR #866）
- xterm.js WriteBuffer 12ms 时间片 + RenderDebouncer rAF 合并
