# 终端内容在连接断开时丢失 设计规范

**状态**：📝 设计中
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

### 实施概要

- 拆分 TerminalPanel 的大 useEffect 为 xterm 生命周期（依赖 container）和 channel 生命周期（依赖 rpc）
- xterm 实例存为 ref，跨 rpc 变化保持存活
- channel 断开时显示连接状态遮罩（现有 `setConnected(false)` 逻辑），但不清空终端内容

## 待定问题

### QUEST Q1: 重连后是否需要重新请求 scrollback
**问题**：channel 重新打开后，服务端会发送 clear screen + scrollback。如果 xterm 保留了旧内容，是否会出现重复？
**建议**：服务端 clear screen 会清除 xterm 当前内容，然后写入最新 scrollback。所以不会重复，只是视觉上有一次"刷新"。可以接受。

### QUEST Q2: 是否需要处理 rpc 从 null 到非 null 的首次连接
**问题**：组件挂载时 rpc 可能为 null（WS 尚未连接），xterm 已创建但 channel 未开。rpc 就绪后需要自动 openChannel。
**建议**：channel useEffect 依赖 rpc，rpc 从 null 变为非 null 时自动触发 openChannel。

## 关键参考

### 源码
- `mutbot/frontend/src/panels/TerminalPanel.tsx:93-623` — 大 useEffect，xterm 创建 + channel 连接混在一起
- `mutbot/frontend/src/panels/TerminalPanel.tsx:616` — `term.dispose()` 在 cleanup 中销毁 xterm
- `mutbot/frontend/src/panels/TerminalPanel.tsx:623` — useEffect 依赖 `[workspaceId, rpc]`
- `mutbot/frontend/src/panels/TerminalPanel.tsx:349-355` — channel closed 处理（`setConnected(false)`）
- `mutbot/frontend/src/App.tsx:292-298` — onClose 中 `setRpc(null)`
