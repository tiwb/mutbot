# Terminal 移动端 UX 增强 设计规范

**状态**：✅ 已完成
**日期**：2026-03-10
**类型**：功能设计

## 背景

Terminal 面板在以下方面存在体验问题：
1. 滚动条样式与应用其他区域不一致（agent panel 的 message-list-scroller 使用 `scrollbar-width: thin` 窄滚动条，terminal 的 xterm.js v6 scrollbar 使用默认宽度且无圆角）
2. 移动端无法通过触摸手势滚动终端内容
3. 移动端缺少键盘输入方案——无法有效向终端发送按键

## 设计方案

### 一、滚动条样式统一

**问题**：xterm.js v6 使用自定义滚动条（`.xterm-scrollable-element > .scrollbar > .slider`），通过内联样式设定宽度（默认 14px），与应用其他区域的窄滚动条风格不一致。

**方案**：通过 CSS 覆盖 xterm.js 滚动条的宽度和圆角：

```css
/* 缩窄 xterm 滚动条，与应用其他区域统一 */
.terminal-panel .xterm .xterm-scrollable-element > .scrollbar.vertical {
  width: 8px !important;
}

.terminal-panel .xterm .xterm-scrollable-element > .scrollbar.vertical > .slider {
  width: 8px !important;
  border-radius: 4px;
  left: 0 !important;
}
```

- 宽度从默认 14px 缩减到 8px（比全局 10px 略窄，符合"稍微窄一些"的要求）
- slider 添加 `border-radius: 4px` 实现圆角
- 颜色通过 xterm Terminal 的 `theme.scrollbarSliderBackground` / `scrollbarSliderHoverBackground` 配置，使用与 `--scrollbar-bg` 一致的值

### 二、移动端触摸滚动

**问题**：xterm.js v6 的自定义滚动区域不响应 touch 事件，移动端用户无法滑动浏览终端历史输出。

**方案**：监听 terminal 容器的 touch 事件，将纵向滑动转换为 xterm 的 `scrollLines()` 调用：

- `touchstart`：记录起始 Y 坐标，停止正在进行的惯性滚动
- `touchmove`：计算 deltaY，按行高换算调用 `term.scrollLines()`，同时跟踪速度
- `touchend`：如果松手时速度足够大，启动惯性动画（FRICTION=0.92 减速）
- 设置阈值区分"滚动"和"点击"，避免误触
- 当用户触摸滚动时阻止 `touchmove` 默认行为，防止页面整体滚动

### 三、移动端键盘输入

**需求**：移动端需要简洁高效的终端输入方式。

#### 布局

终端底部始终显示输入栏（输入框 + ↵ 回车按钮 + ▼/▲ 切换快捷键面板）：

```
┌─────────────────────────────┐
│         Terminal Output      │
│                              │
├─────────────────────────────┤
│ [输入框             ] [↵] [▼]│  ← 始终可见
├─────────────────────────────┤
│ ┌─────┬─────┬─────┬─────┐  │  ← ▼ 展开时
│ │ Esc │ Tab │Back │ Del │  │
│ ├─────┼─────┼─────┼─────┤  │
│ │Ct+C │Ct+D │Ct+Z │Ct+L │  │
│ ├─────┼─────┼─────┼─────┤  │
│ │Ct+A │Ct+E │  ↑  │Enter│  │
│ ├─────┼─────┼─────┼─────┤  │
│ │ ⚙  │  ←  │  ↓  │  →  │  │
│ └─────┴─────┴─────┴─────┘  │
└─────────────────────────────┘
```

#### 输入栏

- 输入框 + ↵ 按钮 + ▼/▲ 按钮
- ↵ 按钮：有内容时发送内容 + 回车，空内容时发送纯回车
- 发送后自动清空输入框，保持焦点
- `font-size: 16px` 防止 iOS 自动缩放
- 发送时自动 `scrollToBottom`

#### 快捷键面板（4x4）

- ▼/▲ 按钮切换快捷键面板显示/隐藏
- 默认收起
- 左下角 ⚙ 按钮进入编辑模式（类似切输入法），编辑模式下变为"保存"按钮
- 快捷键配置持久化到 `localStorage`
- 按键触发触觉反馈（`navigator.vibrate`）

#### 快捷键编辑模式

- 点击 ⚙ 按钮进入编辑模式
- 编辑模式下 ⚙ 变为"保存"按钮
- 编辑模式下点击任意格位弹出编辑弹窗：
  - 显示分类预设列表（常用 Ctrl 组合、方向键、功能键等），点选即设定
  - 列表底部有"自定义"选项，可手动输入显示名称和按键序列
  - 弹窗内有"清空此格"按钮，清空后格位显示为空白
- 空白格位在编辑模式下点击同样弹出编辑弹窗
- 点击"保存"退出编辑模式，持久化配置

- 仅移动端显示，桌面端不渲染键盘输入面板（滚动条样式修改在所有端生效）

## 实施步骤清单

### Phase 1: 滚动条样式 [✅ 已完成]

- [x] **Task 1.1**: 在 `index.css` 添加 xterm 滚动条 CSS 覆盖（8px 宽度 + 圆角）
- [x] **Task 1.2**: 在 TerminalPanel 的 Terminal 构造中配置 `scrollbarSliderBackground` 等颜色与全局 `--scrollbar-bg` 一致
  - 状态：✅ 已完成

### Phase 2: 移动端触摸滚动 [✅ 已完成]

- [x] **Task 2.1**: 在 TerminalPanel 中添加 touch 事件监听（touchstart/touchmove/touchend），将纵向滑动转换为 `term.scrollLines()`
  - 状态：✅ 已完成

### Phase 3-7: 移动端键盘输入 [✅ 已完成]

初始实现为三模式切换（⌨/✎/⚡），经用户测试后重构为简化布局：

- [x] 输入栏（输入框 + ↵ + ▼/▲）始终可见，font-size 16px 防止 iOS 缩放
- [x] 4x4 快捷键网格，左下角 ⚙ 编辑按钮，▼/▲ 切换显示
- [x] 快捷键编辑弹窗（分类预设 + 自定义 + 清空）
- [x] 发送时自动 scrollToBottom
- [x] 触摸滚动添加惯性（momentum scrolling，FRICTION=0.92）
- [x] TerminalPanel 改为 forwardRef，暴露 writeInput / focusTerminal / scrollToBottom
- [x] 删除 MobileTerminalToolbar 组件（三模式切换栏）
  - 状态：✅ 已完成

## 关键参考

### 源码
- `mutbot/frontend/src/panels/TerminalPanel.tsx` — 终端面板组件（forwardRef + TerminalPanelHandle：writeInput/focusTerminal/scrollToBottom + 惯性滚动）
- `mutbot/frontend/src/mobile/ShortcutGrid.tsx` — 4x4 快捷键网格组件（⚙ 编辑按钮 + 编辑模式）
- `mutbot/frontend/src/mobile/ShortcutEditDialog.tsx` — 快捷键编辑弹窗
- `mutbot/frontend/src/mobile/TerminalInput.tsx` — 输入栏组件（输入框 + ↵ + ▼/▲）
- `mutbot/frontend/src/mobile/MobileLayout.tsx` — 移动端布局（集成输入栏 + 快捷键面板 + 编辑模式）
- `mutbot/frontend/src/index.css:3788+` — 全部移动端终端相关样式
- `mutbot/frontend/src/lib/useMobileDetect.ts` — 移动端检测 hook

### xterm.js
- `@xterm/xterm` v6.0.0，滚动条由 `.xterm-scrollable-element > .scrollbar > .slider` 渲染
- `theme.scrollbarSliderBackground` / `scrollbarSliderHoverBackground` / `scrollbarSliderActiveBackground` 控制颜色
- `term.scrollLines(n)` / `term.scrollToBottom()` API 可编程控制滚动
- 无内置 scrollbar width 配置项，需用 CSS `!important` 覆盖内联样式

### 相关规范
- `mutbot/frontend/node_modules/@xterm/xterm/css/xterm.css:224-285` — xterm scrollbar CSS
