# 终端内容在连接断开时丢失 设计规范

**状态**：✅ 已完成
**日期**：2026-03-17
**类型**：Bug修复

## 背景

服务器重启或 WebSocket 短暂断开时，终端面板的内容会立即丢失（屏幕清空），重连后才从服务端恢复。用户体验上表现为终端"闪一下"然后重新加载。

## 根因分析

`setRpc(null)` 触发 TerminalPanel 的 useEffect 重新执行，导致 xterm 实例被销毁：

```
WS 断开 → App.tsx onClose → setRpc(null)
  → TerminalPanel useEffect 依赖 [workspaceId, rpc] 变化
  → cleanup: term.dispose() → xterm DOM 清空，内容丢失
  → effect 重新执行: rpc 为 null → openChannel 失败 → 等待
  → WS 重连 → setRpc(wsRpc) → effect 再次触发
  → 新建 xterm → 重新 openChannel → 服务端发送 scrollback 恢复
```

核心问题：**xterm 实例的生命周期与 rpc prop 强耦合**。rpc 变为 null 时 xterm 被 dispose，所有渲染内容丢失。

## 设计方案

### 核心设计

将 TerminalPanel 的 useEffect 拆分为两个独立的生命周期：

1. **xterm 实例**：只依赖 DOM container，组件挂载时创建、卸载时销毁。WS 断连不影响
2. **channel 连接**：依赖 rpc，rpc 变化时重新 openChannel，但不销毁 xterm

断连时终端保持当前显示内容不变（冻结），只是停止接收新数据。重连后 channel 恢复，数据流继续。

### 重连时 scrollback 不会重复

服务端 `on_connect` 流程：`create_view` → `get_snapshot`，snapshot 先发 clear screen 再写入完整画面。xterm 旧内容被 clear 覆盖，不会重复。视觉上从"冻结的旧画面"刷新为最新画面，比现在的"空白→恢复"体验更好。

### 首次连接 rpc 为 null

拆分后 channel useEffect 依赖 `[rpc]`，天然覆盖：挂载时 rpc=null → effect 跳过 → xterm 空白；rpc 就绪 → effect 触发 → openChannel → snapshot 恢复。与当前行为一致。

### 实施概要

- 拆分 TerminalPanel 的大 useEffect 为 xterm 生命周期（依赖 container）和 channel 生命周期（依赖 rpc）
- xterm 实例存为 ref，跨 rpc 变化保持存活
- channel 断开时显示连接状态遮罩（现有 `setConnected(false)` 逻辑），但不清空终端内容

## 关键参考

### 源码
- `mutbot/frontend/src/panels/TerminalPanel.tsx:93-623` — 大 useEffect，xterm 创建 + channel 连接混在一起
- `mutbot/frontend/src/panels/TerminalPanel.tsx:616` — `term.dispose()` 在 cleanup 中销毁 xterm
- `mutbot/frontend/src/panels/TerminalPanel.tsx:623` — useEffect 依赖 `[workspaceId, rpc]`
- `mutbot/frontend/src/panels/TerminalPanel.tsx:349-355` — channel closed 处理（`setConnected(false)`）
- `mutbot/frontend/src/App.tsx:292-298` — onClose 中 `setRpc(null)`

## 实施步骤清单

- [x] **Task 1**: 拆分 xterm 生命周期为独立 useEffect
  - [x] 提取 xterm 创建（Terminal + FitAddon + addons + open）为 mount-only useEffect，依赖 `[]`
  - [x] xterm 实例已有 `termRef`，fitAddon 已有 `fitRef`，确保跨 effect 可访问
  - [x] `term.dispose()` 仅在此 effect 的 cleanup 中执行（组件卸载时）
  - 状态：✅ 已完成

- [x] **Task 2**: 拆分 channel 连接为独立 useEffect
  - [x] channel useEffect 依赖 `[rpc]`，rpc 变化时重新 openChannel
  - [x] 内部函数（sendResize、sendScroll、handleBinaryData、handleJsonMessage）通过 ref 访问 xterm 实例
  - [x] cleanup 中只关闭 channel（cleanupChannelHandlers + session.disconnect），不 dispose xterm
  - [x] channel.closed 监听移入此 effect
  - 状态：✅ 已完成

- [x] **Task 3**: DOM 事件监听归属调整
  - [x] touch/wheel/keyboard/paste 事件监听留在 channel effect 中（它们的闭包引用 ch/rpc）
  - [x] 断连期间 DOM 事件被移除（有 Connecting 遮罩，可接受），重连后重新注册
  - 状态：✅ 已完成

- [x] **Task 4**: handleRecreate 适配
  - [x] `handleRecreate`（Restart Terminal 按钮）保持现有行为：关闭旧 channel → 重新 init
  - [x] 无需 dispose/recreate xterm，只需重建 channel（已在 initRef 逻辑中实现）
  - 状态：✅ 已完成

- [x] **Task 5**: 构建验证
  - [x] `npm run build` 通过（修复 TypeScript null narrowing）
  - 状态：✅ 已完成

- [x] **Task 6**: 功能测试
  - [x] 桌面端终端正常工作（连接、输入、滚动、resize）
  - [x] 模拟断连恢复：终端内容不丢失
  - [x] 移动端断开重连后终端画面恢复
  - 状态：✅ 已完成
