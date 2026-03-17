# 移动端终端输入丢失与输入历史 设计规范

**状态**：✅ 已完成
**日期**：2026-03-17
**类型**：Bug修复 + 功能设计

## 背景

移动端终端（TerminalInput）使用语音输入大段文字时，实际发送的内容被截断。不确定是语音输入法的问题还是前端代码问题。当前输入链路纯前端完成，无日志可追踪，排查困难。

另外，输入丢失后无法恢复，需要增加输入历史功能作为安全网。

**范围**：仅 TerminalInput（移动端终端输入组件）。PC 端终端直接键盘输入，不受影响。ChatInput（聊天面板）不在本次范围内。

## 问题分析

### 当前输入链路

```
TerminalInput.textarea.onChange → setValue(e.target.value)  // React state
       ↓
handleSend() → onSend(value + "\r")
       ↓
TerminalPanel → 写入 pyte 终端（WebSocket channel）
```

### 可能的截断原因

1. **IME 组合事件**：语音输入法通过 `compositionstart/compositionend` 分段输入，`onChange` 在组合过程中可能获取到不完整文本
2. **发送时机**：语音输入法"完成"时可能触发 Enter 键事件，此时 React state 尚未更新到最终值（闭包捕获的是上一帧的 `value`）
3. **状态竞争**：`handleSend` 通过 `useCallback` 闭包捕获 `value`，如果 onChange 和 keyDown 在同一帧触发，`value` 可能是旧值
4. **输入法自身问题**：语音识别引擎本身截断了内容

### 排查瓶颈

当前整个输入链路无日志，无法区分是"前端没拿到完整文本"还是"拿到了但发送时丢了"。

## 设计方案

### 需求一：输入诊断日志 + IME 保护

**诊断日志**：在 `handleSend` 入口记录文本长度和前 50 字符摘要，通过前端日志系统（WebSocket 转发到后端 `mutbot.frontend` logger）追踪。

查看方式：`mutbot log query --logger mutbot.frontend`

**IME 组合保护**：TerminalInput 当前未处理 `compositionstart/compositionend` 事件。在 IME 组合过程中如果触发 Enter 或点击 Send，应延迟发送直到组合完成：

```
compositionstart → isComposing = true
compositionend   → isComposing = false
handleSend       → if isComposing, 忽略本次发送（等组合完成后用户再触发）
```

### 需求二：输入历史

**定位**：低频操作，作为输入丢失的安全网。不是核心交互，不占用 UI 常驻空间。

**存储**：
- `localStorage`，key: `terminalInput.history`
- 最近 50 条，每条 `{ text: string, timestamp: number }`
- 时间倒序（最新在前）
- 去重：相同文本只保留最新一条

**交互设计**：

长按 Send 按钮 → 模式菜单中新增"Input History"选项 → 弹出历史面板。复用已有的长按交互，无需新增手势。

```
┌─────────────────────────────┐
│ Input History        [Clear]│
│─────────────────────────────│
│ > Last sent command...      │  ← 点击填入输入框
│ > Previous command...       │
│ > Earlier command...        │
│ ...                         │
└─────────────────────────────┘
┌──────────────────┬───┬──┐
│ [输入框]         │ ⏎ │ ▲│
└──────────────────┴───┴──┘
```

- 点击历史条目 → 填入输入框（不自动发送），用户可编辑后再发送
- 历史面板内长按某条 → 删除该条
- 面板顶部 Clear 按钮清空全部历史

**Placeholder 引导**（静态提示可用操作 + 当前输入模式）：

| 模式 | Placeholder |
|------|-------------|
| 单行（Enter 发送） | `Enter to send` |
| 多行（按钮发送） | `Tap button to send` |

**全局共享**：历史不按 session 隔离，所有终端 session 共享同一份历史。

## 实施步骤清单

### Phase 1: 诊断日志 + IME 保护 [✅ 已完成]

- [x] **Task 1.1**: TerminalInput 添加 IME 组合保护
  - [x] 添加 `isComposing` ref，监听 `compositionstart/compositionend`
  - [x] `handleSend` 和 `handleKeyDown` 中检查 composing 状态，组合中不发送
  - 状态：✅ 已完成

- [x] **Task 1.2**: TerminalInput 添加发送诊断日志
  - [x] import `rlog`，在 `handleSend` 入口记录文本长度和前 50 字符
  - 状态：✅ 已完成

- [x] **Task 1.3**: 更新 placeholder 文案
  - [x] 单行模式：`Enter to send · Hold for history`
  - [x] 多行模式：`Tap send button · Hold for history`
  - 状态：✅ 已完成

### Phase 2: 输入历史 [✅ 已完成]

- [x] **Task 2.1**: 实现输入历史存储模块
  - [x] 内联在 TerminalInput.tsx，localStorage 读写、去重、上限 50 条
  - 状态：✅ 已完成

- [x] **Task 2.2**: handleSend 时保存到历史
  - [x] 发送前调用 `pushHistory(text)` 保存文本
  - 状态：✅ 已完成

- [x] **Task 2.3**: 长按输入框弹出历史面板
  - [x] textarea 区域添加长按检测（复用 pointerDown/Up 模式）
  - [x] 实现 HistoryItem 子组件（点击填入输入框，长按删除）
  - [x] 历史面板含 Header + Clear 按钮 + 滚动列表
  - 状态：✅ 已完成

- [x] **Task 2.4**: 历史面板样式
  - [x] 在 index.css 中添加 `.terminal-input-history-*` 样式
  - 状态：✅ 已完成

### Phase 3: 构建验证 [✅ 已完成]

- [x] **Task 3.1**: 前端构建验证
  - [x] `npm --prefix mutbot/frontend run build` 通过
  - 状态：✅ 已完成

## 关键参考

### 源码
- `mutbot/frontend/src/mobile/TerminalInput.tsx` — 核心修改目标，移动端终端输入组件
- `mutbot/frontend/src/mobile/TerminalInput.tsx:33-37` — handleSend，发送逻辑
- `mutbot/frontend/src/mobile/TerminalInput.tsx:78-84` — 长按 Send 按钮切模式（参考交互模式）

### 相关规范
- `mutbot/docs/specifications/feature-terminal-transport-resilience.md` — 传输可靠性设计
