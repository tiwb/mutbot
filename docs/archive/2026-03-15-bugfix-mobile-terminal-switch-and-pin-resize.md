# 移动端 Terminal 切换无效 & Pin Resize 不立即生效

**状态**：✅ 已完成（问题一已修复；问题二无法复现，关闭）

> **注**：手机断开重连后终端无画面的问题已合并到 `bugfix-terminal-content-loss-on-disconnect.md`，根因相同（`setRpc(null)` 导致 xterm 被 dispose）。
**日期**：2026-03-15
**类型**：Bug修复

## 背景

移动端发现两个相关 bug：
1. **Terminal 间切换无效**：在移动端顶部 tab 栏点击不同的 Terminal session，终端内容不变。但切换后刷新页面有效，Agent 之间切换也正常。
2. **Pin Resize to Me 不立即生效**：右键菜单点击 "Pin Resize to Me" 后，终端尺寸不会立刻改变，但某些后续操作（如输入、resize 等）会使其生效。

## 问题一：Terminal 间切换无效

### 根因分析

**核心原因**：`TerminalPanel` 的主 `useEffect` 依赖数组**刻意省略了 `sessionId`**。

```typescript
// TerminalPanel.tsx:540-543
// eslint-disable-next-line react-hooks/exhaustive-deps -- initialId only used
// for ref initialization on mount; re-running the effect on prop change would
// destroy and recreate the terminal (handleRecreate manages recreation).
}, [workspaceId, rpc]);  // ← 不包含 sessionId
```

**对比 AgentPanel**（切换正常）：

```typescript
// AgentPanel.tsx:430
}, [sessionId, rpc];  // ← 包含 sessionId，切换时自动重新初始化
```

**桌面端 vs 移动端差异**：

| 端 | 实现方式 | 切换行为 |
|---|---|---|
| 桌面端 (PanelFactory) | 每个 Terminal 在独立的 FlexLayout tab 中 | 切换 tab → 组件卸载/重新挂载 → 正常 |
| 移动端 (MobileLayout) | 条件渲染同一个 `<TerminalPanel>`，只变 props | `sessionId` prop 变了，但 useEffect 不触发 → **无效** |

**具体故障链**：

1. 用户点击另一个 Terminal tab → `activeSessionId` 更新
2. `TerminalPanel` 的 `sessionId` prop 改变
3. useEffect 不依赖 `sessionId`，**不会重新执行**
4. `chRef.current` 仍指向旧 channel，`termIdRef` 仍指向旧 terminal
5. xterm 实例显示旧 terminal 的内容
6. **刷新有效**：因为整个组件重新挂载，useEffect 首次运行

### 设计方案

**方案 A**（推荐）：在 `MobileLayout` 中给 `TerminalPanel` 添加 `key={activeSession.id}`

React 的 key 变化会导致组件完全卸载并重新挂载，触发 useEffect 首次运行。这是最简洁的修复，不需要改动 TerminalPanel 内部逻辑。

```tsx
<TerminalPanel
  key={activeSession.id}  // ← 加这一行
  ref={termPanelRef}
  sessionId={activeSession.id}
  ...
/>
```

**优点**：一行修复，不影响桌面端，不修改 TerminalPanel 设计。
**代价**：每次切换 terminal 都会销毁旧的 xterm 实例并创建新的。但移动端同一时间只显示一个 terminal，可以接受。

**方案 B**：在 TerminalPanel 中添加 sessionId 依赖

像 AgentPanel 一样在 useEffect 中加入 `sessionId`。但原设计者已考虑过这点并明确排除（注释说 "re-running the effect on prop change would destroy and recreate the terminal"），改动可能影响桌面端。

## 问题二：Pin Resize to Me 不立即生效

### 根因分析

点击 "Pin Resize to Me" 的完整流程：

1. **前端** (TerminalPanel.tsx:624-628)：发送 `set_resize_mode` 消息
2. **后端** (terminal.py:605-618)：
   - 设置 `_follow_me[term_id] = client_id`
   - 广播 `resize_owner` → 前端更新 `followMe` 状态（菜单 checked 更新✓）
   - 尝试用该客户端的 `_client_sizes[term_id][client_id]` 来 resize PTY
3. **前端** (TerminalPanel.tsx:209-212)：收到 `resize_owner`，更新 `followMe` state

**"不立即生效" 的可能原因**：

- 后端 `set_resize_mode` 处理后调用 `tm.resize(term_id, new_rows, new_cols)`（无 `client_id` 参数），这会跳过 `_client_sizes` 记录和 controller 检查，直接 resize PTY。如果 PTY 已经是该尺寸，xterm 不会有可见变化
- 但如果另一个客户端之前控制了尺寸（PTY 当前是另一个客户端的 size），`resize_owner` 广播后并不会立即让所有客户端重新 fit。被 pin 的客户端需要等到下一次 `fit()` 触发（如 container resize、窗口旋转等）才会实际 resize

### 状态

经测试多次操作（auto→pin、PC pin→移动端 pin→PC 再 pin）均正常。暂无法稳定复现。已在 `set_resize_mode` 处理中添加诊断日志（`pin_resize:` 前缀），记录 `_client_sizes` 快照和 resize 结果，等待下次复现时通过日志定位。

## 实施步骤清单

- [x] **Task 1**: 在 MobileLayout 的 TerminalPanel 添加 `key={activeSession.id}`
  - 状态：✅ 已完成

## 关键参考

### 源码
- `mutbot/frontend/src/panels/TerminalPanel.tsx:540-543` — useEffect 依赖数组（不含 sessionId）
- `mutbot/frontend/src/panels/TerminalPanel.tsx:545-549` — handleRecreate 回调
- `mutbot/frontend/src/panels/TerminalPanel.tsx:209-212` — resize_owner 消息处理
- `mutbot/frontend/src/panels/TerminalPanel.tsx:612-630` — Pin Resize 菜单项
- `mutbot/frontend/src/panels/AgentPanel.tsx:430` — AgentPanel useEffect 依赖（含 sessionId）
- `mutbot/frontend/src/mobile/MobileLayout.tsx:355-367` — 移动端 TerminalPanel 渲染
- `mutbot/src/mutbot/runtime/terminal.py:599-625` — set_resize_mode 后端处理
- `mutbot/src/mutbot/runtime/terminal.py:278-283` — _get_resize_controller 逻辑
- `mutbot/src/mutbot/runtime/terminal.py:225-254` — resize 方法（含 controller 检查）

### 相关规范
- `docs/specifications/feature-terminal-resize-control.md` — resize 控制设计规范
