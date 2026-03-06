# 前端 UI 体验优化（6项） 设计规范

**状态**：✅ 已完成
**日期**：2026-03-02
**类型**：功能设计

## 背景

TASKS.md 中积累了 6 项前端 UI/UX 改进需求，涉及连接状态提示、聊天气泡样式、Welcome 页面、流式消息体验和布局对齐等。逐一设计方案。

## 设计方案

### T1: WebSocket 断连时 Session 列表标题栏提示

**需求**：WebSocket 连接断开后，Session 列表标题栏上显示断连指示，让用户知道服务器无法连接，但不阻塞其他操作。

**方案**：
- `SessionListPanel` 新增 `connected` prop（boolean）
- `App.tsx` 跟踪 workspace RPC 的连接状态，传入 `SessionListPanel`
- 标题栏 `<h1>Sessions</h1>` 旁增加断连图标（红色闪烁圆点 + tooltip），仅在 `connected === false` 时显示
- 不弹窗、不阻塞操作，仅作为视觉提示

**WorkspaceRpc 连接状态追踪**：`WorkspaceRpc` 已有 `onOpen/onClose` 回调。在 `App.tsx` 中新增 `wsConnected` state，由 workspace RPC 的 `onOpen`/`onClose` 回调驱动。

**视觉**：标题栏右侧（菜单按钮旁）显示一个小红点 + "Disconnected" tooltip。compact 模式下也显示。

### T2: Agent 聊天气泡背景不透明

**需求**：agent 的聊天气泡背景是透明的，背景有花纹/图片时文字难以辨认。气泡应该不透明。

**现状**：
- `--bg-msg-assistant: #1f1f1f`（与 `--bg` 相同，颜色本身 OK）
- `.message-bubble.assistant.text` 设置了 `background: var(--bg-msg-assistant)`
- `.message-bubble.assistant.tool-group` 设为 `background: transparent`

**问题根因**：当聊天区域有背景图片/花纹时，`--bg-msg-assistant` 虽然是实色但视觉上可能被父容器的背景图覆盖。同时 tool-group 气泡明确使用了 `transparent`。

**方案**：
- 保持 `--bg-msg-assistant: #1f1f1f` 不变（颜色与 `--bg` 一致，视觉效果 OK）
- 确保 `.message-bubble.assistant.text` 的 `background` 是实色不透明（当前已是）
- `.message-bubble.assistant.tool-group` 也改为 `background: var(--bg-msg-assistant)` 使其不透明（工具卡片内部自有背景，外层气泡不透明不影响显示）

### T3: Welcome 页面不要有中文

**需求**：关闭所有 Tab 后显示的 Welcome 页面不要有中文。

**现状**：`WelcomePage.tsx` 中有两段中文描述：
- `— 开始体验 AI 助手`
- `— 在命令行下工作`

**方案**：改为英文：
- `— Start an AI agent session`
- `— Open a command-line terminal`

### T4: 刷新工作区时 tab 已恢复但 Welcome 仍可见

**需求**：刷新工作区时，tab 会自动恢复，但 welcome 还能看到。

**现状**：`App.tsx:1027` 用 `!hasOpenTabs` 控制 Welcome 显示。`hasOpenTabs` 由 `handleModelChange` 中的 `modelHasTabs()` 更新。问题在于：workspace 从 hash 路由恢复 layout 时（`createModel(target.layout)`），此时 model 已有 tab，但 `hasOpenTabs` 初始值是 `false`，直到 `handleModelChange` 被触发才更新。而 `handleModelChange` 只在用户操作或 layout 变化时触发，首次渲染后 model 不会立即触发 change。

**方案**：在 `createModel()` 恢复 layout 后，立即检查 model 是否有 tab 并同步 `hasOpenTabs` 状态。具体：在两处 `modelRef.current = createModel(target.layout)` 之后，加一行同步：
```ts
hasOpenTabsRef.current = modelHasTabs(modelRef.current);
setHasOpenTabs(hasOpenTabsRef.current);
```

### T5: 空消息气泡显示打字动画（三个点）

**需求**：当 agent 发送消息时，如果流式消息尚未到达，显示的是一个空的聊天气泡。希望在消息为空时，气泡内显示三个点的动画（typing indicator）。

**现状**：`response_start` 事件创建一个 `content: ""` 的 assistant text 消息。在 `text_delta` 到达之前，渲染为一个空气泡。

**方案**：在 `MessageList.tsx` 的 `renderBubble()` 中，当 assistant text 消息的 `content` 为空字符串时，显示 typing dots 动画替代空白。

```tsx
// 在 case "text" 的 assistant 分支中：
if (!msg.content) {
  return <div className="typing-dots"><span/><span/><span/></div>;
}
```

CSS typing dots 动画：三个小圆点依次颜色明暗变化（类似主流聊天软件的输入指示器），不跳动。

### T6: Working 状态栏文字与聊天气泡对齐

**需求**：Working 的状态表示栏，文字的位置与聊天气泡对齐，视觉效果更好。

**现状**：`AgentStatusBar` 的 padding 是 `4px 16px`。聊天气泡在 `.message-row` 中有 `margin: 2px 16px`，加上 `.avatar-col` 宽度 `32px` 和 `gap: 8px`，所以气泡内容实际左侧偏移约 `16px + 32px + 8px = 56px`。

**方案**：将 `.agent-status-bar` 的 `padding-left` 改为 `56px`（`16px margin + 32px avatar + 8px gap`），使文字与 assistant 气泡内容左对齐。

## 关键参考

### 源码
- `mutbot/frontend/src/App.tsx` — 主应用，Welcome 显示逻辑（L1027）、workspace RPC 连接（L213-298）、hasOpenTabs 状态（L79-81）
- `mutbot/frontend/src/components/WelcomePage.tsx` — Welcome 页面，中文描述（L29, L38）
- `mutbot/frontend/src/panels/SessionListPanel.tsx` — Session 列表面板，标题栏（L346-371）
- `mutbot/frontend/src/components/MessageList.tsx` — 消息列表，renderBubble（L288-328）
- `mutbot/frontend/src/components/AgentStatusBar.tsx` — Working 状态栏，padding（CSS L658）
- `mutbot/frontend/src/panels/AgentPanel.tsx` — Agent 面板，response_start 创建空消息（L109-131）
- `mutbot/frontend/src/index.css` — 全局样式
  - L7: `--bg-msg-assistant: #1f1f1f`
  - L654-662: `.agent-status-bar` padding
  - L816-820: `.message-bubble.assistant.text` 样式
  - L822-826: `.message-bubble.assistant.tool-group` 透明背景
  - L2608-2656: Welcome 页面样式
- `mutbot/frontend/src/lib/workspace-rpc.ts` — WorkspaceRpc，onOpen/onClose 回调

## 实施步骤清单

### 阶段一：简单修改 [✅ 已完成]

- [x] **Task 1.1**: T3 — Welcome 页面中文改英文
  - 修改 `WelcomePage.tsx` 两处中文描述
  - 状态：✅ 已完成

- [x] **Task 1.2**: T2 — tool-group 气泡背景改为不透明
  - 修改 `index.css` 中 `.message-bubble.assistant.tool-group` 的 `background`
  - 状态：✅ 已完成

- [x] **Task 1.3**: T6 — Working 状态栏 padding-left 对齐气泡
  - 修改 `index.css` 中 `.agent-status-bar` 的 `padding`
  - 状态：✅ 已完成

### 阶段二：逻辑修改 [✅ 已完成]

- [x] **Task 2.1**: T4 — 修复刷新后 Welcome 残留
  - 在 `App.tsx` 两处 `createModel(target.layout)` 后同步 `hasOpenTabs`
  - 状态：✅ 已完成

- [x] **Task 2.2**: T5 — 流式输出时 typing dots 动画
  - `MessageList` 新增 `isStreaming` prop，基于 `agentStatus !== "idle"` 判断
  - `renderBubble()` 在 streaming + 空内容时显示 typing dots
  - `index.css` 添加 `.typing-dots` 样式（三圆点颜色明暗渐变动画）
  - 状态：✅ 已完成

- [x] **Task 2.3**: T1 — WebSocket 断连状态提示
  - `App.tsx` 新增 `wsConnected` state + workspace RPC `onOpen/onClose` 回调
  - `SessionListPanel` 新增 `connected` prop + 红色脉冲圆点指示
  - `index.css` 添加 `.sidebar-disconnected-dot` 样式（红色脉冲动画）
  - 状态：✅ 已完成

### 阶段三：验证 [✅ 已完成]

- [x] **Task 3.1**: 前端构建验证
  - `npm run build` 通过，无编译错误
  - 状态：✅ 已完成

### 阶段三：验证 [待开始]

- [ ] **Task 3.1**: 前端构建验证
  - `npm --prefix mutbot/frontend run build` 确认无编译错误
  - 状态：⏸️ 待开始
