# Web UI 体验改进批次 设计规范

**状态**：✅ 已完成
**日期**：2026-02-24
**类型**：Bug修复 / 功能改进

## 1. 背景

Web 前端在日常使用中积累了多个体验问题，涉及消息重复显示、Session 管理、终端生命周期、面板交互和滚动条行为。本文档统一设计这批改进，共 7 项。

## 2. 设计方案

### 2.1 Bug: 刷新页面后用户消息显示两遍

**现象**：页面刷新后，Agent Session 中用户发送的消息在消息列表中出现两次。

**根因分析**：

`AgentPanel.tsx` 中的消息发送与回放流程：

1. **正常发送**（无刷新）：
   - `handleSend()` 立即将用户消息添加到本地 `messages` state（第 240-248 行）
   - 设置 `lastSentTextRef.current = text` 用于去重（第 239 行）
   - WebSocket 发送 `{ type: "message", text }` 到后端
   - 后端 `AgentBridge.send_message()` 将 `user_message` 事件录入 JSONL 文件
   - 后端通过 `event_callback` 广播 `user_message` 给所有连接的 WS 客户端（多客户端同步）
   - 发送客户端收到回显，`lastSentTextRef` 去重机制跳过（第 157-159 行）→ 不重复

2. **刷新后**：
   - `messageCache`（内存 Map）丢失 → `hadCache = false`
   - `lastSentTextRef` 重置为空字符串
   - `replayedRef`（`useRef<Set>`）重置为空 Set
   - `fetchSessionEvents()` 从 JSONL 返回历史事件，包含 `user_message` → **第 1 份**
   - WebSocket 连接建立后，如果 Agent 仍在处理中，后端 `event_callback` 可能再次推送相关事件，或者 `fetchSessionEvents` 与 WS 事件流之间存在重叠 → **第 2 份**
   - `lastSentTextRef` 为空，无法去重

**修复方案**：

在事件回放阶段引入基于事件 ID 的去重机制：

- **后端**：`record_event()` 和 `send_message()` 为每个事件分配唯一 `event_id`（UUID 或递增序号），写入 JSONL
- **前端**：`handleEvent()` 维护 `processedEventIds: Set<string>`，处理前检查 `event_id` 是否已处理
- 无论事件来自 `fetchSessionEvents()` 回放还是 WebSocket 实时广播，同一 `event_id` 只处理一次
- 对于没有 `event_id` 的旧事件（向后兼容），fallback 到现有的 `lastSentTextRef` 去重逻辑

### 2.2 Bug: Session 列表点击 Terminal 创建新实例

**现象**：在侧边栏 Session 列表中点击已结束的 Terminal Session，会创建一个全新的 Terminal Session，而不是复用已有 tab。

**根因分析**：

`App.tsx` 的 `handleSelectSession()`（第 410-458 行）中，对 ended terminal session 有特殊处理（第 445-453 行）：

```typescript
if (session.type === "terminal" && session.status === "ended" && workspace) {
  createSession(workspace.id, "terminal", { shell_command: shellCommand }).then(...)
  return;
}
```

这段代码在检查 `existingNodeId` **之后**执行，但问题是：当 session 是 ended terminal 且 tab 未打开时，直接创建了新 session，而不是以已结束状态重新打开。更关键的是，如果 tab 已存在但对应的是 ended session，`existingNodeId` 检查通过会激活已有 tab，这是正确的。但如果 tab 不存在（比如用户之前关了 tab），就会走到 createSession 逻辑。

**修复方案**：

1. 移除 ended terminal 的自动创建行为
2. ended terminal session 点击时：
   - tab 已存在 → `selectTab()` 激活（现有逻辑已覆盖）
   - tab 不存在 → `addTabForSession(session)` 打开面板，以过期状态显示
3. 在 TerminalPanel 中，如果初始 terminal 已过期（收到 4004），显示重建提示（配合 2.4）

### 2.3 新功能: 新建 Session 菜单添加图标

**现象**：tabset "+" 按钮的下拉菜单（`AddSessionDropdown`，App.tsx 第 148-231 行）中，Agent/Document/Terminal 选项为纯文本，缺少类型图标。

**修复方案**：

复用 `App.tsx` 中已有的 `TabIcon` 组件（第 39-66 行），在菜单按钮中添加图标：

```tsx
<button onClick={() => handleSelect("agent")}>
  <TabIcon type="agent" /> Agent
</button>
```

CSS 调整 `.add-session-menu button`：
- 添加 `display: flex; align-items: center; gap: 8px;`

### 2.4 改进: 终端过期不自动创建新实例

**现象**：终端 PTY 进程过期（WebSocket 收到 4004 关闭码）后，`TerminalPanel.tsx` 立即自动创建新终端（第 149-155 行），用户无法查看之前的终端输出也无法控制是否重建。

**当前行为**：
```typescript
if (event.code === 4004) {
  term.write("\r\n\x1b[33m[Terminal expired, creating new...]\x1b[0m\r\n");
  termIdRef.current = null;
  ownsTermRef.current = true;
  init();  // 立即创建新 PTY
  return;
}
```

**修复方案**：

1. 收到 4004 时，不调用 `init()`，改为切换到"已过期"UI 状态
2. 新增 state: `const [expired, setExpired] = useState(false)`
3. 4004 处理逻辑：
   ```typescript
   if (event.code === 4004) {
     term.write("\r\n\x1b[33m[Terminal process has ended]\x1b[0m\r\n");
     setExpired(true);
     return;
   }
   ```
4. 渲染逻辑：在终端容器底部叠加提示条
   ```tsx
   {expired && (
     <div className="terminal-expired-bar">
       <span>Terminal process has ended.</span>
       <button onClick={handleRecreate}>Create New Terminal</button>
     </div>
   )}
   ```
5. `handleRecreate` 回调：
   ```typescript
   const handleRecreate = useCallback(() => {
     setExpired(false);
     termIdRef.current = null;
     ownsTermRef.current = true;
     // 触发 init() — 需要在 useEffect 外暴露 init 或用 ref
   }, []);
   ```

**实现细节**：
- `init()` 函数目前在 `useEffect` 闭包内，需要提取为可从外部调用的形式（通过 ref 或提升到组件作用域）
- 提示条固定在终端面板底部，不遮挡终端历史输出
- 样式与终端深色主题协调：`#1e1e1e` 背景，`var(--accent)` 链接色

**CSS 样式**：
```css
.terminal-expired-bar {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 8px 16px;
  background: rgba(30, 30, 30, 0.95);
  border-top: 1px solid var(--border);
  font-size: 13px;
  color: var(--text-dim);
}

.terminal-expired-bar button {
  background: transparent;
  border: 1px solid var(--accent);
  color: var(--accent);
  padding: 4px 12px;
  border-radius: 2px;
  cursor: pointer;
  font-size: 13px;
}

.terminal-expired-bar button:hover {
  background: var(--accent);
  color: white;
}
```

### 2.5 改进: Session 重命名同步

**现象**：在 flexlayout tab 标题上双击可以重命名 tab（flexlayout 内置功能），但 Session 列表中的名称不会同步更新。

**根因分析**：

- flexlayout 的 tab rename 只修改 `Model` 中的 tab node `name` 属性
- Session 对象的 `title` 存储在 `sessions` state 中，与 flexlayout Model 完全独立
- 后端没有 session rename/update API（`routes.py` 中无 PATCH/PUT session endpoint）
- `SessionManager` 中也没有 `update`/`rename` 方法

**修复方案**：

#### 后端

1. `session.py` 的 `SessionManager` 添加 `rename()` 方法：
   ```python
   def rename(self, session_id: str, new_title: str) -> Session | None:
       session = self._sessions.get(session_id)
       if not session:
           return None
       session.title = new_title
       session.updated_at = datetime.now(timezone.utc).isoformat()
       self._persist(session)
       return session
   ```

2. `routes.py` 添加 `PATCH /api/sessions/{session_id}` endpoint：
   ```python
   @router.patch("/api/sessions/{session_id}")
   async def update_session(session_id: str, body: dict):
       _, sm = _get_managers()
       if "title" in body:
           session = sm.rename(session_id, body["title"])
           if not session:
               raise HTTPException(404)
           return _session_dict(session)
       raise HTTPException(400, "no updatable fields")
   ```

#### 前端

1. `api.ts` 添加：
   ```typescript
   export async function renameSession(sessionId: string, title: string) {
     const res = await authFetch(`${BASE}/api/sessions/${sessionId}`, {
       method: "PATCH",
       headers: { "Content-Type": "application/json" },
       body: JSON.stringify({ title }),
     });
     return res.json();
   }
   ```

2. `App.tsx` 的 `handleAction` 中拦截 `RENAME_TAB` 动作：
   ```typescript
   if (action.type === Actions.RENAME_TAB) {
     const nodeId = action.data?.node;
     const newName = action.data?.text;
     if (nodeId && newName) {
       // 找到 tab node → 提取 sessionId
       let sessionId: string | null = null;
       model.visitNodes((node) => {
         if (node.getId() === nodeId && node.getType() === "tab") {
           sessionId = (node as TabNode).getConfig()?.sessionId ?? null;
         }
       });
       if (sessionId) {
         renameSession(sessionId, newName).then(() => {
           setSessions((prev) =>
             prev.map((s) => (s.id === sessionId ? { ...s, title: newName } : s))
           );
         });
       }
     }
     return action; // 允许 flexlayout 执行重命名
   }
   ```

#### 右键菜单重命名入口

除 tab 双击重命名外，在以下两处右键菜单中也添加 "Rename" 选项：

1. **Session 列表右键菜单**（`SessionListPanel.tsx` 的 `getContextMenuItems()`）：
   - 新增 "Rename" 菜单项
   - 点击后进入内联编辑模式（将 `.session-title` 替换为 `<input>`）
   - 编辑完成（回车或失焦）后调用 `renameSession()` API
   - 需要 `SessionListPanel` 新增 `onRenameSession` prop，由 `App.tsx` 传入

2. **Tab 标签右键菜单**（`App.tsx` 的 `getTabContextMenuItems()`）：
   - 新增 "Rename" 菜单项
   - 点击后触发 flexlayout 内置的重命名行为：`model.doAction(Actions.renameTab(nodeId))`
   - 后续流程复用 `handleAction` 中的 `RENAME_TAB` 拦截逻辑


### 2.6 改进: 面板分割拖拽实时预览

**现象**：拖拽 flexlayout 面板分割器时，只有蓝色指示线跟随鼠标移动，面板大小要等松开鼠标才改变。希望拖拽过程中面板大小实时跟随。

**根因分析**：

这是 flexlayout-react 的默认行为。库内部的 `Splitter` 组件在 `onDragMove` 中只移动指示线的 CSS 位置，`updateLayout()`（实际调整面板权重）仅在 `onDragEnd`（鼠标释放）时调用。

**修复方案**：

flexlayout-react（当前版本 0.8.18）提供了 `realtimeResize` prop，在 `<Layout>` 组件上启用即可：

```tsx
// App.tsx 第 832 行
<Layout
  model={model}
  factory={factory}
  onModelChange={handleModelChange}
  onRenderTabSet={onRenderTabSet}
  onRenderTab={onRenderTab}
  onAction={handleAction}
  realtimeResize={true}   // 启用实时拖拽预览
/>
```

**工作原理**（源码确认）：
- `realtimeResize={true}` 时，`Splitter.onDragMove` 在每次 pointermove 事件中都调用 `updateLayout()`，即时 dispatch `Actions.adjustWeights()` 更新面板权重
- `realtimeResize={false}`（默认）时，`updateLayout()` 只在 `onDragEnd` 调用

**性能注意**：库文档警告 "this can cause resizing to become choppy when tabs are slow to draw"。当前面板中 xterm.js 终端和 Monaco Editor 渲染开销较大，如果实时拖拽出现卡顿，可能需要：
- 对 `onModelChange` 中的 `updateWorkspaceLayout()` 网络请求做 debounce（拖拽过程中频繁触发 model change）
- 如果卡顿严重，可回退为默认行为

### 2.7 改进: 聊天面板滚动条自动隐藏

**现象**：聊天框的滚动条始终可见，希望在鼠标未指向对应聊天面板时自动隐藏，且隐藏/显示过程有渐变过渡。

**当前样式**（`index.css` 第 240-260 行）：
```css
::-webkit-scrollbar-thumb {
  background: var(--scrollbar-bg);  /* rgba(121,121,121,0.4) — 始终可见 */
}
```

**修复方案**：

使用 CSS `scrollbar-color` 属性实现渐变过渡（Chrome 125+、Firefox 均支持 transition）：

```css
.message-list {
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: transparent transparent;
  transition: scrollbar-color 0.3s ease;
}

.agent-panel:hover .message-list {
  scrollbar-color: var(--scrollbar-bg) transparent;
}
```

同时保留 webkit scrollbar 伪元素作为 fallback（不支持 transition 但可实现 hover 显隐）：

```css
.message-list::-webkit-scrollbar-thumb {
  background: transparent;
}

.agent-panel:hover .message-list::-webkit-scrollbar-thumb {
  background: var(--scrollbar-bg);
}

.agent-panel:hover .message-list::-webkit-scrollbar-thumb:hover {
  background: var(--scrollbar-hover);
}
```

防止内容跳动：
```css
.message-list {
  scrollbar-gutter: stable;
}
```

注意：此改动仅针对 `.message-list`（聊天面板），不影响全局滚动条样式。全局 `::-webkit-scrollbar` 规则保持不变。

## 3. 已确认的设计决策

| 决策项 | 结论 |
|--------|------|
| 消息去重方案 | 后端分配 `event_id`，前端按 ID 去重 |
| 终端过期重建 UI | 底部条形提示，不遮挡终端历史输出 |
| 滚动条渐变效果 | `scrollbar-color` + transition，不引入额外 JS 库，旧浏览器 fallback 为瞬间切换 |
| 已结束 Terminal 点击行为 | 直接打开面板显示已结束状态 + 重建链接，无需确认对话框 |

## 4. 实施步骤清单

### 阶段一：Bug 修复 [✅ 已完成]

- [x] **Task 1.1**: 修复用户消息重复显示
  - [x] 根因修复：移除 `agent_bridge.py` `send_message()` 中的重复录制（forwarder 已负责录制）
  - [x] 后端 `record_event()` 为事件分配 `event_id`（防御性去重）
  - [x] 前端 `AgentPanel.tsx` 维护 `processedEventIds: Set<string>`
  - [x] `handleEvent()` 添加 event_id 去重检查
  - [x] 向后兼容：无 event_id 的旧事件 fallback 到现有 `lastSentTextRef` 去重逻辑
  - 状态：✅ 已完成

- [x] **Task 1.2**: 修复 Terminal Session 列表点击行为
  - [x] 移除 `handleSelectSession()` 中 ended terminal 自动创建新 session 的逻辑
  - [x] ended terminal 点击时以过期状态打开面板（统一走 `addTabForSession(session)`）
  - 状态：✅ 已完成

### 阶段二：终端过期改进 [✅ 已完成]

- [x] **Task 2.1**: 终端过期不自动创建新实例
  - [x] `TerminalPanel.tsx` 新增 `expired` state
  - [x] 通过 `initRef` 暴露 init() 使其可从 useEffect 外部调用
  - [x] 4004 处理逻辑改为 `setExpired(true)` + 终端提示文字
  - [x] 渲染底部提示条 `.terminal-expired-bar` + "Create New Terminal" 按钮
  - [x] `handleRecreate` 回调调用 `initRef.current()`
  - [x] `index.css` 新增 `.terminal-expired-bar` 及按钮样式
  - [x] `.terminal-panel` 添加 `position: relative`
  - 状态：✅ 已完成

### 阶段三：Session 管理改进 [✅ 已完成]

- [x] **Task 3.1**: Session 重命名同步
  - [x] 后端 `SessionManager` 添加 `rename()` 方法
  - [x] 后端 `routes.py` 添加 `PATCH /api/sessions/{session_id}` endpoint
  - [x] 前端 `api.ts` 添加 `renameSession()` 函数
  - [x] 前端 `handleAction` 拦截 `RENAME_TAB` 动作，调用 API + 同步 sessions state
  - [x] Session 列表右键菜单添加 "Rename" 项（内联编辑 + API 调用）
  - [x] Session 列表支持双击条目进入重命名模式
  - [x] Tab 标签右键菜单添加 "Rename" 项（`window.prompt` + `Actions.renameTab`）
  - [x] `SessionListPanel` 新增 `onRenameSession` prop + inline rename UI
  - [x] `App.tsx` 添加 `handleRenameSession` 回调（API + state + flexlayout 同步）
  - [x] `index.css` 新增 `.session-rename-input` 样式
  - 状态：✅ 已完成

- [x] **Task 3.2**: 新建 Session 菜单添加图标
  - [x] `AddSessionDropdown` 菜单按钮添加 `TabIcon` 组件
  - [x] CSS `.add-session-menu button` 从 `display: block` 改为 `display: flex` + `gap: 8px`
  - 状态：✅ 已完成

### 阶段四：交互体验改进 [✅ 已完成]

- [x] **Task 4.1**: 面板分割拖拽实时预览
  - [x] `App.tsx` 的 `<Layout>` 组件添加 `realtimeResize={true}` prop
  - [x] `handleModelChange` 添加 300ms debounce（`layoutSaveTimer` ref）防止拖拽中频繁网络请求
  - 状态：✅ 已完成

- [x] **Task 4.2**: 聊天面板滚动条自动隐藏
  - [x] `.message-list` 添加 `scrollbar-width: thin` + `scrollbar-color: transparent` + `transition: 0.3s`
  - [x] `.agent-panel:hover .message-list` 显示滚动条
  - [x] webkit fallback：`::-webkit-scrollbar-thumb` 透明/hover 切换
  - [x] `scrollbar-gutter: stable` 防止内容跳动
  - 状态：✅ 已完成

## 5. 测试验证

### 手工测试场景
- [ ] 发送消息后刷新页面 → 消息不重复
- [ ] 点击已结束的 Terminal Session → 不创建新 session，显示过期状态
- [ ] "+" 菜单显示类型图标
- [ ] 终端过期后显示重建链接，点击后重建
- [ ] 双击 tab 重命名 → 侧边栏名称同步更新
- [ ] Session 列表右键菜单 "Rename" → 内联编辑后同步更新
- [ ] Tab 右键菜单 "Rename" → 触发重命名后同步更新
- [ ] 拖拽面板分割器 → 面板大小实时跟随鼠标
- [ ] 聊天面板鼠标移入 → 滚动条渐显；移出 → 渐隐
