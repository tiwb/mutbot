# Session 面板与工具卡片 UX 改进 设计规范

**状态**：✅ 已完成
**日期**：2026-03-03
**类型**：功能设计

## 背景

Agent session 面板存在多项 UX 问题：工具参数显示不友好、键盘快捷键缺失、工具停止状态显示错误、workspace 创建/加载流程异常等。本规范统一处理 6 项改进。

## 设计方案

### S1: 工具参数展示优化

**需求**：工具的 arguments 展开后应显示为真正的列表（每行一个 key-value），单行值可滚动，很长的值可以弹框浏览。无参数的工具不显示参数区域。

**现状**：
- 展开后用 `JSON.stringify(data.input, null, 2)` 在 `<pre>` 中一次性渲染
- 空参数工具展开后显示 `Arguments: {}`

**方案**：

**参数列表渲染**：替换 JSON dump，改为 key-value 列表：
```
┌──────────────────────────────────────┐
│ ▾ define_module(path="src/foo.py")   │
├──────────────────────────────────────┤
│  path       src/foo.py               │
│  content    (长文本，可点击展开弹框)    │
│  options    ["a", "b", "c"]          │
└──────────────────────────────────────┘
```

- 每个参数一行：左侧 key（dimmed 色），右侧 value
- **短值**（≤120 字符）：直接显示，单行，横向可滚动（`overflow-x: auto; white-space: nowrap`）
- **长值**（>120 字符）：截断显示 + 右侧 "⤢" 展开按钮，点击弹出全屏 modal 查看完整内容
- **结构化值**（object/array）：JSON 格式化显示，同样应用长度判断规则

**弹框**：
- Modal overlay，深色背景，`<pre>` 展示完整值
- 右上角关闭按钮 + ESC 关闭 + 点击蒙层关闭
- 标题显示参数名

**空参数处理**：
- `Object.keys(data.input).length === 0` 时，不渲染 Arguments 区域
- 折叠状态预览也改为只显示工具名，不带 `()`

### S2: 移除 Session "ended" 状态

**需求**：Session 不应有 ended 状态。当前 session 被 stop 后后端设为 `"ended"`，前端加载时显示为 ended。

**现状**：
- `session_impl.py:681`：`session.status = "ended"` 在 `stop()` 方法中
- `routes.py:904`：`session.status not in ("", "stopped")` 决定是否立即启动 bridge — "ended" 不在排除列表中，导致已停止的 session 在 WS 连接时尝试重启 agent
- 前端 `SessionListPanel`：`getStatusDisplay()` 只认识 "running" 和 "stopped"，其他值用 `status-default` 样式

**方案**：

**后端**：
- `session_impl.py` stop 方法：`session.status = "ended"` → 改为 `session.status = "stopped"`
- `routes.py:904` 条件已包含 `"stopped"`，无需改动

**前端**：
- 无需改动（`getStatusDisplay` 已正确处理 "stopped"）

### S3: Ctrl+W 关闭当前 Session Tab

**需求**：捕获 Ctrl+W 键盘事件关闭当前活动的 session tab。当全部标签页都被关闭后，不再拦截 Ctrl+W，让浏览器执行默认行为（关闭浏览器标签页）。

**方案**：
- 在 `App.tsx` 添加全局 `keydown` 事件监听（`useEffect`）
- 捕获 `Ctrl+W`（或 `Cmd+W` macOS）
- **有活动 tab 时**：`e.preventDefault()` 阻止浏览器关闭标签页，调用 FlexLayout model 的 `doAction(Actions.deleteTab(activeTabId))`
- **无活动 tab 时**：不调用 `e.preventDefault()`，让浏览器正常关闭标签页

**实现要点**：
- 获取当前活动 tab：`modelRef.current.getActiveTabset()?.getSelectedNode()?.getId()`
- 如果 `tabId` 存在，`e.preventDefault()` + `doAction(Actions.deleteTab(tabId))`
- 如果 `tabId` 不存在（无 tab），不拦截，浏览器默认行为生效

### S4: 停止的工具显示正确状态 — 区分取消与错误

**需求**：被 stop/cancel 的工具在前端仍显示为运行状态（绿点 + 闪烁），应该显示为已取消。取消状态应与错误状态有视觉区分。

**现状**：
- `agent_cancelled` 事件仅设置 `setAgentStatus("idle")`，不更新 `toolCallMapRef` 中未完成的工具卡片
- ToolCallCard 判断运行状态：`isRunning = data.result === undefined`
- 未收到 `tool_exec_end` 的工具永远显示为 running
- ToolCallCard 只有三种视觉状态：running（绿色）、success（绿色 ✓）、error（红色 ✗）

**方案**：

**新增 cancelled 状态**：在 `ToolGroupData` 中新增 `isCancelled?: boolean` 字段。

**AgentPanel 处理 agent_cancelled**：
```typescript
} else if (eventType === "agent_cancelled") {
  setAgentStatus("idle");
  pendingTextRef.current = "";
  // 标记所有未完成的工具为已取消
  if (toolCallMapRef.current.size > 0) {
    const pending = new Map(toolCallMapRef.current);
    toolCallMapRef.current.clear();
    setMessages((prev) =>
      prev.map((m) => {
        if (m.type === "tool_group" && pending.has(m.data?.toolCallId)) {
          return { ...m, data: { ...m.data!, result: "(cancelled)", isCancelled: true } };
        }
        return m;
      }),
    );
  }
}
```

**ToolCallCard 视觉区分**：
- **Running**：绿色边框 + 闪烁 ● — `isRunning`（当前行为不变）
- **Success**：绿色 ✓ — `!isRunning && !isError && !isCancelled`
- **Error**：红色 ✗ — `isError && !isCancelled`
- **Cancelled**：灰色/黄色 ⊘ — `isCancelled`
  - 边框色 `var(--text-dim)`（灰色）
  - 状态图标用 `\u2298`（⊘ 禁止符）或 `\u2716`
  - CSS class: `.tool-card.cancelled`

### S5: 创建 Workspace 后直接打开

**需求**：创建 workspace 后应直接进入该 workspace。

**现状**：`App.tsx:991-994` 的 `onCreated` 回调执行 `setWorkspaces(prev => [...prev, ws]); location.hash = ws.name; setWorkspace(ws);`。但实际行为是 hash 变为空 `#`，留在 WorkspaceSelector 页面。

**根因分析**：
1. `setWorkspaces(prev => [...prev, ws])` — React 批量更新，state 尚未 commit
2. `location.hash = ws.name` — 同步触发 hashchange 事件
3. hashchange handler（L189-214）从闭包中读取 `workspaces`，此时新 workspace 尚未在列表中
4. `workspaces.find(w => w.name === wsName)` 返回 `undefined`
5. handler 将 hash 清空（L211: `location.hash = ""`）+ `setWorkspace(null)`
6. 最后 `setWorkspace(ws)` 被 `setWorkspace(null)` 覆盖

**方案**：`onCreated` 中不通过 hash 路由，直接设置 workspace state 并延迟设 hash：
```typescript
onCreated={(ws) => {
  setWorkspaces((prev) => [...prev, ws]);
  setWorkspace(ws);
  // 延迟设置 hash，确保 workspaces state 已包含新 workspace
  requestAnimationFrame(() => { location.hash = ws.name; });
}}
```

### S6: Hash 指向 Workspace 时显示连接中状态

**需求**：当 URL 有 `#workspaceName` 但 workspace 尚未加载时，不应显示 WorkspaceSelector，应显示 "Connecting to workspace..." 加载状态。

**现状**：
- `App.tsx:960`：`if (!workspace)` → 一律显示 `WorkspaceSelector`
- App RPC 连接 → `workspace.list` 返回 → hash 匹配 → `setWorkspace(target)`
- 在 RPC 连接完成前或列表加载中，用户看到的是 WorkspaceSelector 而非加载状态

**方案**：
- 新增判断条件：当 `!workspace && location.hash.replace(/^#\/?/, "")` 非空时，显示加载状态而非 WorkspaceSelector
- 加载状态 UI：与 WorkspaceSelector 同样的全屏布局，标题 "MutBot"，下方显示 "Connecting to workspace..."（带 spinner）
- 如果 RPC 连接失败或 workspace 不存在（hashchange handler 清空了 hash），自然 fallback 回 WorkspaceSelector

## 关键参考

### 源码
- `mutbot/frontend/src/components/ToolCallCard.tsx` — 工具卡片渲染，`formatArgsPreview()`，JSON.stringify 参数展示，ToolGroupData 接口
- `mutbot/frontend/src/panels/AgentPanel.tsx` — Session WebSocket 连接管理，事件处理（tool_exec_start/end, agent_cancelled），状态点
- `mutbot/frontend/src/panels/SessionListPanel.tsx` — Session 列表，`getStatusDisplay()`，状态指示（running/stopped）
- `mutbot/frontend/src/App.tsx` — Workspace 连接管理，tab 管理，FlexLayout model，onCreated 回调（L991-994），hashchange handler（L189-214），workspace 判断（L960）
- `mutbot/frontend/src/components/WorkspaceSelector.tsx` — Workspace 选择器，handleCreate（L155-158）
- `mutbot/frontend/src/components/DirectoryPicker.tsx` — 目录选择器，handleCreate（L88-111）
- `mutbot/frontend/src/index.css` — `.status-dot`（L565-575），`.tool-card`（L1201-1256），`.session-status`（L482-507）
- `mutbot/src/mutbot/runtime/session_impl.py:681` — `session.status = "ended"` 设置
- `mutbot/src/mutbot/web/agent_bridge.py:76-87` — agent status → session status 映射
- `mutbot/src/mutbot/web/routes.py:885-944` — Session WebSocket 端点，延迟启动逻辑

### 相关规范
- `docs/specifications/feature-frontend-ui-polish.md` — 前一轮 UI 优化（已完成），包含断连提示、typing dots 等

## 实施步骤清单

### 阶段一：简单后端 + 前端修复 [✅ 已完成]

- [x] **Task 1.1**: S2 — 后端移除 ended 状态
  - `session_impl.py:681`：`session.status = "ended"` → `"stopped"`
  - 状态：✅ 已完成

- [x] **Task 1.2**: S5 — 修复 workspace 创建后不打开的竞争问题
  - `App.tsx` onCreated 回调：先 `setWorkspace(ws)` 再 `requestAnimationFrame` 延迟设 hash
  - 状态：✅ 已完成

- [x] **Task 1.3**: S6 — Hash 存在时显示连接中状态
  - `App.tsx` 在 `!workspace` 判断分支中，检查 hash 是否非空，显示 "Connecting to workspace..." 加载页
  - 状态：✅ 已完成

- [x] **Task 1.4**: S3 — Ctrl+W 关闭 session tab
  - `App.tsx` 新增全局 keydown 监听，有 tab 时 preventDefault + deleteTab，无 tab 时放行
  - 状态：✅ 已完成

### 阶段二：工具取消状态 [✅ 已完成]

- [x] **Task 2.1**: S4 — ToolGroupData 新增 isCancelled 字段
  - `ToolCallCard.tsx` 中 `ToolGroupData` 接口新增 `isCancelled?: boolean`
  - 状态：✅ 已完成

- [x] **Task 2.2**: S4 — AgentPanel 处理 agent_cancelled 标记未完成工具
  - `AgentPanel.tsx` 中 `agent_cancelled` 事件处理：遍历 toolCallMapRef 标记为 cancelled
  - 状态：✅ 已完成

- [x] **Task 2.3**: S4 — ToolCallCard 视觉区分 cancelled 状态
  - 四态渲染逻辑（running/success/error/cancelled），cancelled 用灰色 ⊘
  - `index.css` 新增 `.tool-card.cancelled` 样式
  - 状态：✅ 已完成

### 阶段三：工具参数展示重构 [✅ 已完成]

- [x] **Task 3.1**: S1 — 空参数处理
  - ToolCallCard 展开时不渲染 Arguments 区域（`Object.keys(input).length === 0`）
  - `formatArgsPreview()` 空参数返回空字符串而非 `"()"`
  - 状态：✅ 已完成

- [x] **Task 3.2**: S1 — key-value 列表渲染替换 JSON dump
  - 替换 `JSON.stringify` 为 key-value 行列表（ArgRow 组件）
  - 短值单行显示（横向可滚动），结构化值 JSON 格式化
  - 状态：✅ 已完成

- [x] **Task 3.3**: S1 — 长值截断 + 弹框查看
  - 超 120 字符的值截断 + "⤢" 展开按钮
  - 新增 ArgModal 组件：overlay + pre + ESC/蒙层关闭
  - 对应 CSS 样式
  - 状态：✅ 已完成

### 阶段四：构建验证 [✅ 已完成]

- [x] **Task 4.1**: 前端构建验证
  - `npm --prefix mutbot/frontend run build` 通过，无编译错误
  - 状态：✅ 已完成
