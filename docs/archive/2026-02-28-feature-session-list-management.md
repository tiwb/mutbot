# Session 列表管理增强 设计规范

**状态**：✅ 已完成
**日期**：2026-02-28
**类型**：功能设计

## 1. 背景

当前 session 管理存在以下问题：

1. **启动新 workspace 自动创建 session，但无法便捷删除**：workspace.sessions 列表会不断增长
2. **Session 列表无手动排序**：当前按 status 排序（active 在前），用户无法自行调整顺序
3. **无多选操作**：只能逐个右键删除 session，效率低
4. **Session 状态写死**：当前仅 `"active"` / `"ended"` 两种状态，stop 后永远是 `"ended"`，但 Agent 和 Terminal 的运行状态应由子类自行管理
5. **面板激活与列表选中不同步**：在 flexlayout 中切换 tab 不会更新 Session 列表的高亮状态

## 2. 设计方案

### 2.1 Session 状态模型重构

**核心变更**：`session.status` 是一个开放的字符串字段，可以是任何值。基类 `Session` 默认值为空字符串 `""`。各子类自行设置有意义的状态值。框架负责状态的同步（广播给前端）和持久化。

#### 后端变更

**Session 基类**：
```python
class Session(mutobj.Declaration):
    status: str = ""  # 默认空（原来默认 "active"）
```

**子类自行定义状态语义**：
- `AgentSession`：Agent 工作中设为 `"running"`，空闲时设为 `""`
- `TerminalSession`：进程运行中设为 `"running"`，进程退出设为 `"stopped"`
- `DocumentSession`：无特殊状态，保持 `""`

**状态设置方式**：各子类、菜单扩展、或其他业务逻辑通过 `session.update()` 或直接赋值 `session.status = "..."` 设置状态。框架通过广播 `session_updated` 事件同步给前端，通过 `_persist()` 持久化到磁盘。

**移除统一的 session.stop()**：`SessionManager.stop()` 不再作为统一抽象。各 Session 子类通过菜单扩展（已支持）定义自己的停止/清理行为。现有的 `EndSessionMenu` / `EndSessionListMenu` 由子类各自提供替代菜单。

**移除写死的 "ended" 赋值**：
- `SessionManager.stop()` 不再强制设置 `session.status = "ended"`
- `_session_from_dict()` 反序列化时不再默认 `status = "ended"`，使用字段原始值

#### 前端变更

前端直接使用 `session.status` 字段显示状态 badge，不再区分"持久化状态"和"运行时状态"。

**已知状态列表与显示映射**：

| status 值 | 显示文本 | 样式 |
|-----------|---------|------|
| `""` (空) | 不显示 badge | — |
| `"running"` | Running | 绿色指示点 |
| `"stopped"` | Stopped | 灰色指示点 |
| 其他未知值 | 原始文本 | 默认灰色文本 |

```typescript
function getStatusDisplay(status: string): { text: string; className: string } | null {
  if (!status) return null;  // 空状态不显示
  const known: Record<string, { text: string; className: string }> = {
    running: { text: "Running", className: "status-running" },
    stopped: { text: "Stopped", className: "status-stopped" },
  };
  return known[status] ?? { text: status, className: "status-default" };
}
```

### 2.2 Session 列表排序

#### 列表数据源

Session 列表严格以 `workspace.sessions`（workspace JSON 文件中的 ID 列表）为准。不从磁盘扫描 session 文件，不自动排序。

#### 用户手动排序

用户可以在 Session 列表中拖拽调整顺序。前端拖拽完成后，更新 `workspace.sessions` 的顺序并持久化。

- **前端**：SessionListPanel 支持拖拽排序（HTML5 Drag and Drop）
- **后端**：新增 RPC `workspace.reorder_sessions`，接受新的 session ID 列表顺序

```python
@workspace_rpc.method("workspace.reorder_sessions")
async def handle_reorder_sessions(params: dict, ctx: RpcContext) -> dict:
    """更新 workspace 中的 session 排列顺序。"""
    new_order = params.get("session_ids", [])
    # 校验 ID 集合一致后，替换 workspace.sessions
    ...
```

#### 测试 session 隔离

自动测试不操作用户的 workspace，因此测试创建的 session 不会进入 `workspace.sessions` 列表。无需额外的过滤机制。

### 2.3 多选删除

#### 多选交互

- **Ctrl+Click**：切换单个 session 的选中状态（加选/取消选）
- **Shift+Click**：范围选择（从上次选中的 session 到当前 click 的 session）
- **普通 Click**：单选（清除其他选中，选中当前项，同时激活面板）

> **注**：键盘快捷键（Delete/Ctrl+A/Escape）已移除。未来快捷键由 Menu `display_shortcut` 声明，前端自动生效，无需额外代码。

#### 选中状态

```typescript
// SessionListPanel 内部状态
const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
const lastClickedRef = useRef<string | null>(null);  // Shift+Click 锚点
```

`selectedIds` 是多选状态，`activeSessionId` 是激活面板对应的 session。两者独立：
- **普通 Click**：设为唯一选中项 + 激活面板
- **Ctrl+Click**：切换选中状态，不激活面板
- **Shift+Click**：范围选中，不激活面板

#### 批量删除

- **右键菜单**：通过 Menu 框架扩展。选中多个 session 后右键，显示 "Delete Sessions" 菜单项（使用现有 `SessionList/Context` category）
- 后端新增 `session.delete_batch` RPC

```python
@workspace_rpc.method("session.delete_batch")
async def handle_session_delete_batch(params: dict, ctx: RpcContext) -> dict:
    """批量删除 sessions。"""
    session_ids = params.get("session_ids", [])
    # 逐个 delete + 从 workspace.sessions 移除 + 广播
    ...
```

#### 菜单集成

`DeleteSessionMenu` 改为感知多选状态：
- 前端在调用 `menu.execute` 时，`params` 中传入 `session_ids` 列表
- 单选时传单个 ID，多选时传所有选中的 ID
- 菜单 execute 根据 ID 数量调用 `session.delete` 或 `session.delete_batch`

### 2.4 面板激活与列表选中同步

#### activeSessionId 语义

`activeSessionId` 表示当前用户正在操作的 session。全局唯一，最多一个。

- 多面板（多个 tabset）情况下，只有用户最后交互的面板对应的 session 是 active
- 如果能检测到浏览器窗口失焦（`document.hidden` 或 `visibilitychange`），可以将 activeSessionId 设为 null

#### Tab 切换同步

监听 flexlayout 的 `onAction`，在 `SELECT_TAB` action 中提取 tab 对应的 sessionId，更新 `activeSessionId`：

```typescript
// App.tsx handleAction 中增加
if (action.type === Actions.SELECT_TAB) {
  const nodeId = (action as any).data?.tabNode;
  if (nodeId) {
    let tabNode: TabNode | null = null;
    model.visitNodes((node) => {
      if (node.getId() === nodeId && node.getType() === "tab") {
        tabNode = node as TabNode;
      }
    });
    if (tabNode) {
      const sessionId = resolveSessionId(tabNode, sessions);
      if (sessionId) setActiveSessionId(sessionId);
    }
  }
}
```

#### 浏览器失焦处理

```typescript
useEffect(() => {
  const handleBlur = () => setActiveSessionId(null);
  const handleFocus = () => {
    // 从当前 active tab 恢复 activeSessionId
    const activeTabset = model.getActiveTabset();
    const selectedNode = activeTabset?.getSelectedNode();
    if (selectedNode?.getType() === "tab") {
      const sessionId = resolveSessionId(selectedNode as TabNode, sessions);
      if (sessionId) setActiveSessionId(sessionId);
    }
  };
  window.addEventListener("blur", handleBlur);
  window.addEventListener("focus", handleFocus);
  return () => {
    window.removeEventListener("blur", handleBlur);
    window.removeEventListener("focus", handleFocus);
  };
}, [sessions]);
```

#### 视觉区分

| 状态 | 样式 |
|------|------|
| 激活（active panel） | 左侧高亮条 + 背景色 |
| 选中（multi-select） | 浅色背景 |
| 激活 + 选中 | 两者叠加 |

## 3. 待定问题

（已全部解决，无待定问题）

## 4. 实施步骤清单

### 阶段一：Session 状态模型重构 [✅ 已完成]

- [x] **Task 1.1**: 后端 — Session 基类 status 默认值改为空字符串
  - [x] `session.py` 中 `Session.status` 默认值从 `"active"` 改为 `""`
  - [x] `_session_from_dict()` 反序列化默认值从 `"ended"` 改为 `""`（保留磁盘原始值）
  - 状态：✅ 已完成

- [x] **Task 1.2**: 后端 — 移除 SessionManager.stop() 中的统一 status 赋值
  - [x] `stop()` 方法不再设置 `session.status = "ended"`
  - [x] 保留 runtime 资源清理逻辑（Agent bridge stop、PTY kill、log handler 移除）
  - [x] `stop()` 仅作为内部资源清理方法，不影响 session 状态
  - 状态：✅ 已完成

- [x] **Task 1.3**: 后端 — 子类自行管理状态
  - [x] AgentSession：AgentBridge 开始工作时设 `"running"`，idle 时设 `""`
  - [x] TerminalSession：创建 PTY 时设 `"running"`，PTY 退出时设 `"stopped"`
  - [x] 通过 `session_updated` 广播同步给前端
  - [x] SessionManager 新增 `set_session_status()` 和 `_maybe_broadcast_updated()`
  - [x] AgentBridge 新增 `session_status_fn` 回调，在 `_broadcast_status` 中同步
  - 状态：✅ 已完成

- [x] **Task 1.4**: 前端 — 更新状态显示逻辑
  - [x] 实现 `getStatusDisplay()` 函数：已知状态映射 + 未知状态默认显示
  - [x] SessionListPanel 使用新的状态显示逻辑（绿色/灰色指示点）
  - [x] 移除 `status === "ended"` 相关的 CSS 类和排序逻辑
  - 状态：✅ 已完成

- [x] **Task 1.5**: 后端 — 移除 EndSessionMenu / EndSessionListMenu
  - [x] 删除 `menus.py` 中的 `EndSessionMenu` 和 `EndSessionListMenu`
  - [x] 前端移除 `ConfirmDialog` 和 `pendingEndSession` 相关逻辑
  - [x] 移除 routes.py 中 `session_ended` action 处理
  - 状态：✅ 已完成

### 阶段二：Session 列表排序 [✅ 已完成]

- [x] **Task 2.1**: 前端 — 按 workspace.sessions 顺序渲染列表
  - [x] SessionListPanel 移除按 status 排序的逻辑
  - [x] 按传入的 sessions prop 的原始顺序（即 workspace.sessions 顺序）渲染
  - 状态：✅ 已完成

- [x] **Task 2.2**: 前端 — 拖拽排序
  - [x] SessionListPanel 支持 HTML5 Drag and Drop 拖拽重排（full + compact 模式）
  - [x] 拖拽完成后调用 `workspace.reorder_sessions` RPC 持久化新顺序
  - [x] 前端通过 `onReorderSessions` 回调乐观更新本地状态
  - [x] 拖拽视觉反馈（dragging 半透明 + drag-over 蓝色边框）
  - 状态：✅ 已完成

- [x] **Task 2.3**: 后端 — `workspace.reorder_sessions` RPC
  - [x] 新增 handler：接受 `session_ids` 列表，校验 ID 集合一致后替换 `workspace.sessions`
  - [x] 持久化更新后的 workspace
  - 状态：✅ 已完成

### 阶段三：多选删除 [✅ 已完成]

- [x] **Task 3.1**: 前端 — 多选状态管理
  - [x] SessionListPanel 增加 `selectedIds` state 和 `lastClickedRef`
  - [x] 实现 Ctrl+Click 切换选中、Shift+Click 范围选中、普通 Click 单选
  - [x] 实现选中项的视觉样式（区别于激活样式）
  - [x] 右键菜单自动将未选中项加入选中集
  - 状态：✅ 已完成

- [x] **Task 3.2**: ~~前端 — 键盘快捷键~~ (已移除)
  - ~~Delete 键删除选中的 session~~ — 移除，避免误操作；未来快捷键由 Menu 声明
  - ~~Ctrl+A 全选~~ — 移除
  - ~~Escape 取消选中~~ — 移除
  - 状态：✅ 已移除

- [x] **Task 3.3**: 后端 — `session.delete_batch` RPC
  - [x] 新增批量删除 handler
  - [x] 逐个 delete + 从 workspace.sessions 移除
  - [x] 广播 `session_deleted` 事件（每个被删的 session）
  - 状态：✅ 已完成

- [x] **Task 3.4**: 菜单集成 — DeleteSessionMenu 感知多选
  - [x] 前端调用 menu.execute 时传入 `session_ids` 列表
  - [x] DeleteSessionMenu.execute 根据数量返回 `session_deleted` 或 `session_deleted_batch`
  - [x] 多选时菜单显示 "Delete (N)"
  - [x] routes.py handle_menu_execute 处理 `session_deleted_batch` action
  - 状态：✅ 已完成

### 阶段四：面板激活与列表选中同步 [✅ 已完成]

- [x] **Task 4.1**: 前端 — flexlayout tab 切换同步 activeSessionId
  - [x] 在 `handleAction` 中拦截 `SELECT_TAB`
  - [x] 在 `handleAction` 中拦截 `SET_ACTIVE_TABSET`（跨 tabset 切换焦点）
  - [x] 从 tab config 提取 sessionId，更新 `activeSessionId`
  - 状态：✅ 已完成

- [x] **Task 4.2**: 前端 — 浏览器失焦时清空 activeSessionId
  - [x] 监听 window `blur`/`focus` 事件（替代 `visibilitychange`，覆盖更多场景）
  - [x] `blur` 时设 `activeSessionId = null`
  - [x] `focus` 时根据当前 flexlayout active tab 恢复
  - 状态：✅ 已完成

- [x] **Task 4.3**: 前端 — 视觉样式区分
  - [x] 激活状态：左侧高亮条 + 深色背景
  - [x] 选中状态：浅色背景（多选时）
  - [x] 更新 CSS 样式
  - 状态：✅ 已完成

---

### 实施进度总结
- ✅ **阶段一：Session 状态模型重构** - 100% 完成 (5/5任务)
- ✅ **阶段二：Session 列表排序** - 100% 完成 (3/3任务)
- ✅ **阶段三：多选删除** - 100% 完成 (4/4任务)
- ✅ **阶段四：面板激活与列表选中同步** - 100% 完成 (3/3任务)

**核心功能完成度：100%** (15/15核心任务)

## 5. 测试验证

### 单元测试
- [x] Session 基类 status 默认为空字符串（12 个测试 — TestSessionStatus）
- [x] session.status 可设置任意字符串值，持久化和恢复正确
- [x] workspace.reorder_sessions 正确更新排序（4 个测试 — TestReorderSessions）
- [x] session.delete_batch 批量删除并更新 workspace.sessions（5 个测试 — TestDeleteBatch）
- [x] session.list 按 workspace.sessions 顺序返回（2 个测试 — TestSessionListOrder）
- 执行结果：24/24 通过

### 集成测试
- [x] Agent session 工作时状态为 "running"，空闲后为 ""
- [x] Terminal session 运行时为 "running"，exit 后为 "stopped"
- [x] 前端未知状态值以默认样式显示原始文本
- [x] 拖拽排序后刷新页面，顺序保持（已修复：session.list 按 workspace.sessions 顺序返回）
- [x] 多选 session → 右键菜单 → 批量删除
- [x] 切换 flexlayout tab → 确认列表高亮同步（已修复：增加 SET_ACTIVE_TABSET 拦截）
- [x] 浏览器失焦 → 列表无高亮 → 恢复焦点 → 高亮恢复（已修复：改用 window blur/focus）
