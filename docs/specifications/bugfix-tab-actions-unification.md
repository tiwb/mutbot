# Tab 操作统一与修复 设计规范

**状态**：✅ 已完成
**日期**：2026-02-24
**类型**：Bug修复 / 重构

## 1. 背景

Tab 右键菜单的重命名、关闭等操作与内置行为（双击重命名、X 按钮关闭）使用了不同的代码路径，导致行为不一致和严重 Bug：

1. **右键重命名使用 `window.prompt()`**（`App.tsx:837`），而 flexlayout-react 已内置双击触发的 inline rename 功能，体验割裂
2. **"Close Panel Only" 后重新打开 Terminal 显示 "Terminal process has ended"**，因为 TerminalPanel 在 unmount 时仍然删除了 PTY
3. **右键菜单 "Close" 不弹确认对话框**，因为 `model.doAction()` 绕过了 `onAction` 回调
4. **多处重复逻辑**：sessionId 解析、close 确认流程、rename 同步逻辑在多处重复实现

## 2. 设计方案

### 2.1 核心概念：关闭 vs 结束

本次修复明确区分两个操作语义：

| 操作 | 含义 | 适用场景 |
|------|------|---------|
| **关闭（Close）** | 关闭面板/Tab，session 保持 active | X 按钮、右键 Close、Close Others |
| **结束（End Session）** | 结束 session，status 变为 ended | Sidebar "End Session"、Tab 右键 "End Session" |

**设计原则**：
- 所有 **关闭** 操作统一只关闭面板，不触发 session 结束，不弹确认对话框
- **结束** 是独立的显式操作，弹确认对话框后调用 `stopSession()`
- Terminal 和 Agent session 遵循相同模式
- PTY 生命周期由后端 session 管理，前端不直接操作

### 2.2 根因分析

#### 问题 1：右键重命名与双击重命名不一致

- **现状**：右键 Rename → `window.prompt()` → `model.doAction(Actions.renameTab())` + 手动 `renameSession()` API 调用
- **双击**：flexlayout-react 内置 inline input → 触发 `Actions.RENAME_TAB` → `handleAction` 拦截并同步 backend
- **根因**：右键菜单绕开了 flexlayout 内置的 inline rename，用了独立实现
- **代码位置**：`App.tsx:829-859`

#### 问题 2：Close Panel Only 后 Terminal 无法重新打开

- **流程还原**：
  1. 用户创建 Terminal → TerminalPanel 挂载，`ownsTermRef = true`（因为没有 initialId）
  2. Terminal 创建后，`onTerminalCreated` 将 terminalId 写入 tab config
  3. 用户点 X → 确认对话框 → 选 "Close Panel Only"
  4. `handleTerminalCloseCancel()` 调用 `Actions.deleteTab()` 关闭 tab
  5. TerminalPanel unmount 时，检查 `ownsTermRef.current === true` → **调用 `apiDeleteTerminal()` 删除 PTY**
  6. Session 仍为 "active" 状态
  7. 用户点击 sidebar 重新打开 → 新 tab 带相同 terminalId → 连接已删除的 PTY → WS 返回 4004 → "Terminal process has ended"
- **根因**：TerminalPanel cleanup 在所有 unmount 场景下都会删除 PTY（`TerminalPanel.tsx:292-296`），没有区分 "关闭面板" 和 "结束会话"
- **代码位置**：`TerminalPanel.tsx:292-296`

#### 问题 3：右键 Close 不弹确认对话框

- **现状**：右键 Close → `model.doAction(Actions.deleteTab(nodeId))`（`App.tsx:862-866`）
- **关键发现**：`model.doAction()` **直接操作 model**，不触发 Layout 组件的 `onAction` 回调。`onAction` 仅在 flexlayout-react 内部 UI 事件（如点击 X 按钮）时触发
- **根因**：右键菜单的 close 绕过了 `handleAction` 中的 DELETE_TAB 拦截逻辑
- **新方案**：关闭统一不需要确认（见 2.1），所以此问题通过重新定义"关闭"语义来解决，而非修复拦截逻辑
- **代码位置**：`App.tsx:862-866`（右键）vs `App.tsx:518-567`（X 按钮）

#### 问题 4：代码重复

| 逻辑 | 出现位置 | 问题 |
|------|---------|------|
| sessionId 解析（含 terminalId fallback） | `handleAction` RENAME_TAB（490-504）、DELETE_TAB（532-540）、context menu rename（841-849） | 3 处重复 |
| Terminal close 确认 | Tab X 按钮（544-553）+ sidebar close（711-730） | 2 套状态（`pendingClose` / `pendingSessionClose`）、2 套确认处理函数 |
| rename 同步逻辑 | context menu rename（850-857）、`handleAction` RENAME_TAB（505-515）、`handleRenameSession`（732-753） | 3 处独立实现 |

### 2.3 修复方案

#### Fix 1：右键重命名复用 flexlayout 内置 inline rename

- 为 `<Layout>` 组件添加 `ref`，获取 Layout 实例
- 右键 "Rename" 点击时，通过 `layoutRef.current.setEditingTab(tabNode)` 触发 inline rename
- 已有的 `handleAction` RENAME_TAB 拦截自动完成 backend 同步
- **删除** context menu 中的 `window.prompt()` 和手动 rename API 调用逻辑

```typescript
// 修改后的 Rename onClick
onClick: () => {
  let tabNode: TabNode | null = null;
  model.visitNodes((n) => {
    if (n.getId() === nodeId && n.getType() === "tab") tabNode = n as TabNode;
  });
  if (tabNode && layoutRef.current) {
    layoutRef.current.setEditingTab(tabNode);
  }
}
```

#### Fix 2：TerminalPanel 不再在 unmount 时删除 PTY

- **移除** TerminalPanel cleanup 中的 `apiDeleteTerminal()` 调用
- PTY 生命周期完全由后端 session 管理：`stopSession()` 负责清理 PTY
- 关闭面板后 PTY 保持存活，重新打开时可正常连接
- 移除 `ownsTermRef` 相关逻辑（不再需要区分 "owns" 与否）
- 保留 `ws?.close()` 清理 WebSocket 连接

```typescript
// 修改后的 cleanup
return () => {
  active = false;
  initRef.current = null;
  if (reconnectTimer) clearTimeout(reconnectTimer);
  inputDisposable.dispose();
  resizeObserver.disconnect();
  ws?.close();
  wsRef.current?.close();
  wsRef.current = null;
  term.dispose();
  termRef.current = null;
  // 不再删除 PTY，由后端 session 生命周期管理
};
```

#### Fix 3：简化 Tab 关闭流程（关闭 = 仅关闭面板）

关闭统一只关闭面板，不再需要拦截逻辑：

- **移除** `handleAction` 中 DELETE_TAB 的 Terminal 确认拦截逻辑
- **移除** `handleAction` 中 DELETE_TAB 的 Agent 自动 `stopSession()` 逻辑
- `handleAction` DELETE_TAB 直接 `return action` 允许关闭
- 右键 "Close" 使用 `model.doAction(Actions.deleteTab())` 即可（不再需要绕过问题）
- 右键 "Close Others" 直接关闭所有其他 tab
- **移除** `pendingClose` 状态和相关的 `handleTerminalCloseConfirm` / `handleTerminalCloseCancel`
- **移除** Tab close 相关的 ConfirmDialog

```typescript
// 简化后的 handleAction DELETE_TAB
if (action.type === Actions.DELETE_TAB) {
  // 关闭 = 仅关闭面板，session 保持 active
  return action;
}
```

#### Fix 4：增加 "End Session" 操作

为"结束 Session"提供明确的操作入口：

**4a. Tab 右键菜单添加 "End Session"**

- 仅对 active session（status !== "ended"）显示
- 点击后弹确认对话框
- 确认后调用 `stopSession()` → session 变为 ended
- 同时关闭对应的 tab（如果打开的话）

```typescript
// Tab 右键菜单项
{
  label: "End Session",
  disabled: session?.status === "ended",
  onClick: () => {
    setPendingEndSession({ nodeId, sessionId });
  },
},
```

**4b. Sidebar "Close Session" 改为 "End Session"**

- `SessionListPanel.tsx` 中菜单项 label 从 "Close Session" 改为 "End Session"
- 行为不变：弹确认 → 调用 `stopSession()`

**4c. 统一确认状态**

合并 `pendingClose` 和 `pendingSessionClose` 为 `pendingEndSession`：

```typescript
// 统一状态
const [pendingEndSession, setPendingEndSession] = useState<{
  nodeId?: string;   // 有值时同时关闭 tab
  sessionId: string;
} | null>(null);

// 确认处理：结束 session
const handleEndSessionConfirm = useCallback(() => {
  if (!pendingEndSession) return;
  const { nodeId, sessionId } = pendingEndSession;
  stopSession(sessionId).then(() => {
    setSessions((prev) =>
      prev.map((s) => (s.id === sessionId ? { ...s, status: "ended" } : s)),
    );
  });
  if (nodeId) {
    const model = modelRef.current;
    if (model) model.doAction(Actions.deleteTab(nodeId));
  }
  setPendingEndSession(null);
}, [pendingEndSession]);

// 取消
const handleEndSessionCancel = useCallback(() => {
  setPendingEndSession(null);
}, []);
```

ConfirmDialog 简化为两个按钮：
- "End Session" → `handleEndSessionConfirm`
- "Cancel" → `handleEndSessionCancel`

不再需要 "Close Panel Only" 选项（因为直接点 X / Close 就是关闭面板）。

#### Fix 5：消除重复逻辑

**5a. 提取 `resolveSessionId` 工具函数**

```typescript
function resolveSessionId(
  tabNode: TabNode,
  sessions: Session[],
): string | undefined {
  const config = tabNode.getConfig();
  let sessionId = config?.sessionId as string | undefined;
  if (!sessionId && config?.terminalId) {
    const match = sessions.find(
      (s) => s.config?.terminal_id === config.terminalId,
    );
    if (match) sessionId = match.id;
  }
  return sessionId;
}
```

替换 `handleAction` RENAME_TAB（490-504）和 context menu 中的重复 sessionId 解析代码。

**5b. rename 同步由 handleAction 统一处理**

Fix 1 实施后，所有 rename 操作（双击 / 右键）都通过 flexlayout 的 `Actions.RENAME_TAB` 触发，由 `handleAction` 统一拦截同步。`handleRenameSession`（sidebar rename）保持独立但走相同的后端 API。无需额外修改。

## 3. 已确认设计决策

- **Q1 孤儿 PTY 处理**：✅ 前端只负责操作，后端统一管理 PTY 生命周期
- **Q2 `setEditingTab` internal API**：✅ 使用此 API，版本锁定在 0.8.18
- **Q3 Close 语义**：✅ 关闭 = 仅关闭面板；结束 = 显式 End Session。所有 session 类型统一处理

## 4. 实施步骤清单

### 阶段一：提取工具函数与消除重复 [已完成]

- [x] **Task 1.1**: 提取 `resolveSessionId()` 函数
  - [x] 在 `App.tsx` 中创建 `resolveSessionId(tabNode, sessions)` 函数
  - [x] 替换 `handleAction` RENAME_TAB 中的 sessionId 解析代码
  - [x] 替换 context menu rename 中的 sessionId 解析代码（将在 Task 2.3 中随 rename 重写一起删除）
  - 状态：✅ 已完成

### 阶段二：修复核心 Bug [已完成]

- [x] **Task 2.1**: 简化 Tab 关闭流程
  - [x] 移除 `handleAction` DELETE_TAB 中的 Terminal 确认拦截逻辑
  - [x] 移除 `handleAction` DELETE_TAB 中的 Agent `stopSession()` 逻辑
  - [x] 移除 `pendingClose` 状态、`handleTerminalCloseConfirm`、`handleTerminalCloseCancel`
  - [x] 移除 Tab close 相关的 ConfirmDialog 渲染
  - 状态：✅ 已完成

- [x] **Task 2.2**: 统一 "End Session" 操作
  - [x] 合并 `pendingClose` 和 `pendingSessionClose` 为 `pendingEndSession`
  - [x] 实现 `handleEndSessionConfirm` 和 `handleEndSessionCancel`
  - [x] Tab 右键菜单添加 "End Session" 选项（仅 active session 显示）
  - [x] Sidebar "Close Session" 改为 "End Session"
  - [x] 更新 ConfirmDialog：简化为 "End Session" / "Cancel" 两个按钮
  - [x] 更新 `handleCloseSession` → `handleEndSession` 使用新状态
  - 状态：✅ 已完成

- [x] **Task 2.3**: 修复 TerminalPanel PTY cleanup
  - [x] 移除 unmount 时的 `apiDeleteTerminal()` 调用
  - [x] 移除 `ownsTermRef` 相关逻辑
  - [x] 确保 WebSocket 连接正常关闭
  - 状态：✅ 已完成

- [x] **Task 2.4**: 修复右键 Rename 使用 inline rename
  - [x] 为 `<Layout>` 添加 ref（`layoutRef`）
  - [x] 修改右键 "Rename" 调用 `layoutRef.current.setEditingTab(tabNode)`
  - [x] 删除 `window.prompt()` 和手动 rename API 调用代码
  - 状态：✅ 已完成

### 阶段三：验证与回归测试 [待手动测试]

- [ ] **Task 3.1**: 功能验证
  - [ ] 双击 tab 重命名 → inline 输入正常工作
  - [ ] 右键 tab → Rename → inline 输入，与双击行为一致
  - [ ] Sidebar 双击重命名正常
  - [ ] 重命名后 tab title + sidebar + backend 三方同步
  - [ ] Terminal tab: X 关闭 → 面板关闭，session 保持 active
  - [ ] Terminal tab: 关闭后从 sidebar 重新打开 → Terminal 可用
  - [ ] Terminal tab: 右键 Close → 与 X 关闭行为一致
  - [ ] Terminal tab: 右键 End Session → 确认 → session 结束
  - [ ] Agent tab: X 关闭 → 面板关闭，session 保持 active
  - [ ] Agent tab: 右键 End Session → 确认 → session 结束
  - [ ] Close Others → 所有其他 tab 面板关闭
  - [ ] Sidebar: End Session → 确认 → session 结束
  - 状态：⏸️ 待开始

- [ ] **Task 3.2**: TypeScript 类型检查
  - [x] 运行 `npx tsc --noEmit` 无错误
  - 状态：✅ 已完成

## 5. 测试验证

### 手动测试场景

- [ ] 右键 tab → Rename → 与双击 rename 行为一致（inline 输入）
- [ ] Terminal tab: X / 右键 Close → 仅关闭面板，session active
- [ ] Terminal tab: 关闭面板 → sidebar 重新打开 → Terminal 正常可用
- [ ] Terminal tab: 右键 End Session → 确认 → session ended
- [ ] Agent tab: X / 右键 Close → 仅关闭面板，session active
- [ ] Agent tab: 右键 End Session → 确认 → session ended
- [ ] Close Others → 所有其他面板关闭，session 不受影响
- [ ] Sidebar End Session → 确认 → session ended
- [ ] 重命名后 tab title、sidebar、backend 三方同步

## 6. 遗留问题

- `layout.setEditingTab()` 为 internal API，未来 flexlayout-react 升级需关注兼容性
