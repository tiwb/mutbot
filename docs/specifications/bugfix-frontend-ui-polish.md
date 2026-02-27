# 前端 UI 修正与完善 设计规范

**状态**：✅ 已完成
**日期**：2026-02-27
**类型**：Bug修复 + 功能改进

## 1. 背景

前端存在多项 UI 问题和改进需求，涉及 tabbar 样式、思考指示器、自动滚动、Close All 功能和界面国际化（中文→英文）。

## 2. 设计方案

### 2.1 Tabbar 背景色 — 最大化状态修正

**问题**：面板最大化后，tabbar 背景色不正确。

**根因分析**：flexlayout-react 最大化时在 tabset 容器上添加 `.flexlayout__tabset_maximized` 类。当前 CSS（`index.css:232`）只设置了 border 和 `background-image: none`，但没有覆盖 tabbar 背景色。最大化后 flexlayout 内部可能给 tabset 容器施加了额外的 background，导致 tabbar 外层 `.flexlayout__tabset-tabbar_outer` 的 `var(--tab-inactive-bg)` 被覆盖或冲突。

**修复方案**：为 `.flexlayout__tabset_maximized` 内的 tabbar 显式设置背景色：

```css
.flexlayout__tabset_maximized .flexlayout__tabset-tabbar_outer {
  background: var(--tab-inactive-bg) !important;
}

.flexlayout__tabset_maximized .flexlayout__tab {
  background: var(--bg) !important;
}
```

### 2.2 工作状态栏重设计

#### 2.2.1 问题分析

**问题 A — 状态频繁切换导致闪烁**：

当前 `AgentStatus` 有三个值：`"idle" | "thinking" | "tool_calling"`。后端在一个 turn 中会反复推送 `agent_status` 事件：

```
thinking → tool_calling → thinking → tool_calling → thinking → idle
```

Virtuoso Footer 只在 `agentStatus === "thinking"` 时渲染（`MessageList.tsx:126`），导致 Footer 在 thinking/tool_calling 之间反复 mount/unmount，造成：
1. 视觉上指示器闪烁
2. Footer 高度反复变化，干扰 Virtuoso 的 scroll follow 机制（见 2.3）

**问题 B — dots 在文字前面**：应移到后面。

**问题 C — 水平位置不对齐**。

#### 2.2.2 设计方案 — 状态栏移出滚动区域

参考 Claude Code 的设计：AI 工作时底部始终有一条状态栏显示工作时间和 token 消耗，工作结束后显示总耗时。

**核心决策**：将工作状态指示器从 Virtuoso 内部（Footer）移到滚动区域外部，作为 `AgentPanel` 中 `MessageList` 和 `ChatInput` 之间的独立组件。

**当前布局**：
```
┌─ AgentPanel ──────────────────────┐
│ [agent-header]                    │
│ ┌─ MessageList (Virtuoso) ──────┐ │
│ │ messages...                   │ │
│ │ [Footer: thinking indicator]  │ │  ← 在滚动区域内，反复 mount/unmount
│ └───────────────────────────────┘ │
│ [ChatInput]                       │
└───────────────────────────────────┘
```

**新布局**：
```
┌─ AgentPanel ──────────────────────┐
│ [agent-header]                    │
│ ┌─ MessageList (Virtuoso) ──────┐ │
│ │ messages...                   │ │  ← 纯消息，无 Footer
│ └───────────────────────────────┘ │
│ [AgentStatusBar]                  │  ← 滚动区域外，固定位置
│ [ChatInput]                       │
└───────────────────────────────────┘
```

**AgentStatusBar 组件**：

```tsx
interface AgentStatusBarProps {
  isBusy: boolean;
}

function AgentStatusBar({ isBusy }: AgentStatusBarProps) {
  const [elapsed, setElapsed] = useState(0);
  const startTimeRef = useRef(0);

  useEffect(() => {
    if (isBusy) {
      startTimeRef.current = Date.now();
      setElapsed(0);
      const timer = setInterval(() => {
        setElapsed(Math.floor((Date.now() - startTimeRef.current) / 1000));
      }, 1000);
      return () => clearInterval(timer);
    }
  }, [isBusy]);

  if (!isBusy) return null;

  return (
    <div className="agent-status-bar">
      <span className="status-spinner" />
      <span>Working {elapsed > 0 ? `${elapsed}s` : ""}</span>
    </div>
  );
}
```

工作中显示：`◌ Working 5s`（旋转 spinner + 计时器）
空闲时不渲染（`return null`），高度为 0，不占空间。

**Spinner CSS**：
```css
.status-spinner {
  width: 12px;
  height: 12px;
  border: 1.5px solid var(--text-dim);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
```

**AgentPanel 传参简化**：
- MessageList：移除 `agentStatus` 和 `toolName` props，移除 Footer 配置
- AgentStatusBar：只接收 `isBusy={agentStatus !== "idle"}`

**CSS**：
```css
.agent-status-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 16px;
  color: var(--text-dim);
  font-size: 12px;
  flex-shrink: 0;
}
```

### 2.3 自动滚动到底部

#### 2.3.1 问题分析

**现象**：用户发送消息后，新内容到达时不总是滚动到底部。

**根因**：

1. **Footer 反复 mount/unmount 是主因**：`agentStatus` 在 thinking ↔ tool_calling 之间切换时，Footer 反复出现/消失。Virtuoso 计算 "是否在底部" 基于 content height，Footer 消失→content 变短→Footer 出现→content 变长→Virtuoso 错过 follow 时机。

2. **`followOutput="smooth"` 作为字符串时**：Virtuoso 内部先检查 atBottom 状态再决定是否跟随，边界情况下可能不触发。

#### 2.3.2 设计方案

**层次一：彻底消除 Footer 对滚动的干扰（2.2 已解决）**

状态指示器移出 Virtuoso（2.2.2），MessageList 移除 Footer 配置。Virtuoso 内部只有纯消息列表，高度变化只由消息内容驱动，不再受状态切换干扰。这是最根本的修复。

**层次二：`followOutput` 改为回调**

```tsx
followOutput={(isAtBottom: boolean) => (isAtBottom ? "smooth" : false)}
```

回调形式给予明确控制：`isAtBottom` 为 true 时平滑跟随，否则不跟随。`atBottomThreshold` 保持 `150` 不变（不宜过大影响体验）。

**层次三：发送消息时强制滚动**

用户发送消息后，无论当前滚动位置，都应重新进入"follow"模式并滚到底部。

实现：
- `AgentPanel` 新增 `scrollSignal` state（递增计数器），`handleSend` 时 +1
- `MessageList` 新增 `scrollToBottomSignal` prop
- `MessageList` 内部用 `useEffect` 监听 signal 变化，执行：
  ```ts
  useEffect(() => {
    if (scrollToBottomSignal > 0) {
      setAtBottom(true);
      requestAnimationFrame(() => {
        virtuosoRef.current?.scrollToIndex({ index: "LAST", align: "end", behavior: "smooth" });
      });
    }
  }, [scrollToBottomSignal]);
  ```

**保证**：当用户处于 follow 模式（`atBottom === true`，下箭头按钮不可见）时，通过上述三层措施，新内容始终会触发平滑滚动到底部。可以有短暂延迟（smooth 动画），但不会停在非底部位置。

### 2.4 Tabbar Close All 按钮

**需求**：Tabbar 右键菜单中增加"Close All"选项，关闭当前 tabset 中的所有 tab。

**实现方案**：在 `App.tsx` 的 `handleTabClientAction` 中添加 `close_all` action：

```ts
} else if (action === "close_all") {
  let parentId: string | null = null;
  model.visitNodes((node) => {
    if (node.getId() === nodeId && node.getType() === "tab") {
      parentId = node.getParent()?.getId() ?? null;
    }
  });
  if (!parentId) return;
  const toClose: string[] = [];
  model.visitNodes((node) => {
    if (node.getType() === "tab" && node.getParent()?.getId() === parentId) {
      toClose.push(node.getId());
    }
  });
  for (const id of toClose) {
    model.doAction(Actions.deleteTab(id));
  }
}
```

后端菜单注册需添加 `close_all` 的 `client_action` 菜单项。

### 2.5 全英文界面

**需求**：将前端所有中文用户可见文本改为英文。

**变更清单**：

| 文件 | 位置 | 中文 | 英文 |
|------|------|------|------|
| `ChatInput.tsx:89` | placeholder | `输入消息... (Shift+Enter 换行)` | `Type a message... (Shift+Enter for newline)` |
| `ChatInput.tsx:90` | placeholder | `输入消息... (Ctrl+Enter 发送)` | `Type a message... (Ctrl+Enter to send)` |
| `ChatInput.tsx:111,116,122` | title | `即将支持` | `Coming soon` |
| `ChatInput.tsx:135` | title | `停止` | `Stop` |
| `ChatInput.tsx:137` | button text | `停止` | `Stop` |
| `ChatInput.tsx:146` | button text | `发送` | `Send` |
| `ChatInput.tsx:162` | menu item | `按 Enter 发送` | `Send with Enter` |
| `ChatInput.tsx:168` | menu item | `按 Ctrl+Enter 发送` | `Send with Ctrl+Enter` |
| `MessageList.tsx:136` | title | `滚动到底部` | `Scroll to bottom` |
| `MessageList.tsx:205` | text | `思考中...` | `Thinking` |
| `IconPicker.tsx:118` | placeholder | `搜索图标...` | `Search icons...` |
| `IconPicker.tsx:125` | empty state | `无匹配图标` | `No matching icons` |
| `IconPicker.tsx:152` | button text | `重置为默认` | `Reset to default` |

**注**：代码注释中的中文不在本次范围内（不影响用户界面）。

## 3. 待定问题

无（Q1、Q2 已确认）。

## 4. 实施步骤清单

### 阶段一：CSS 和样式修正 [✅ 已完成]
- [x] **Task 1.1**: 修复 tabbar 最大化背景色
  - [x] 在 `index.css` 中为 `.flexlayout__tabset_maximized` 添加 tabbar 背景色规则
  - 状态：✅ 已完成

- [x] **Task 1.2**: 添加 AgentStatusBar 样式
  - [x] 添加 `.agent-status-bar` CSS（flex, gap, padding, font-size）
  - [x] 清理旧的 `.agent-status-indicator` CSS
  - 状态：✅ 已完成

### 阶段二：工作状态栏 + 滚动 [✅ 已完成]
- [x] **Task 2.1**: 创建 AgentStatusBar 组件，移出滚动区域
  - [x] 新建 `AgentStatusBar`：spinner + "Working" + 计时器
  - [x] `AgentPanel` 中插入到 MessageList 和 ChatInput 之间
  - [x] 传入 `isBusy={agentStatus !== "idle"}`
  - [x] 添加 `.agent-status-bar` CSS
  - 状态：✅ 已完成

- [x] **Task 2.2**: 清理 MessageList — 移除 Footer 和状态相关 props
  - [x] 移除 `agentStatus`、`toolName` props
  - [x] 移除 Virtuoso 的 `Footer` 配置
  - [x] 移除 `AgentStatusIndicator` 组件
  - [x] 清理 `.agent-status-indicator` CSS
  - 状态：✅ 已完成

- [x] **Task 2.3**: 修复自动滚动
  - [x] `followOutput` 改为回调 `(isAtBottom) => isAtBottom ? "smooth" : false`
  - [x] `AgentPanel` 新增 `scrollSignal` 计数器，`handleSend` 时 +1
  - [x] `MessageList` 新增 `scrollToBottomSignal` prop，useEffect 监听并执行滚动
  - 状态：✅ 已完成

### 阶段三：Close All [✅ 已完成]
- [x] **Task 3.1**: 添加 Close All 功能
  - [x] 在 `App.tsx` 的 `handleTabClientAction` 中添加 `close_all` 处理逻辑
  - [x] 后端菜单注册 `close_all` client_action 项
  - 状态：✅ 已完成

### 阶段四：界面英文化 [✅ 已完成]
- [x] **Task 4.1**: ChatInput 中文→英文
  - [x] 替换 placeholder、button text、title、menu item（8 处）
  - 状态：✅ 已完成

- [x] **Task 4.2**: MessageList 中文→英文
  - [x] 替换 "滚动到底部" → "Scroll to bottom"
  - 状态：✅ 已完成

- [x] **Task 4.3**: IconPicker 中文→英文
  - [x] 替换 placeholder、empty state、button text（3 处）
  - 状态：✅ 已完成

## 5. 测试验证

### 手动测试
- [ ] 面板最大化/恢复时 tabbar 背景色一致
- [ ] 工作状态栏：在 ChatInput 上方、滚动区域外部，工作时显示 spinner + "Working Xs"
- [ ] 工作状态栏：AI 空闲时不显示
- [ ] 发送消息后始终自动滚动到底部
- [ ] 手动滚动上方后不会被强制拉回底部（仅发送时重置）
- [ ] 状态栏出现/消失不影响滚动位置
- [ ] Close All 关闭 tabset 内所有 tab
- [ ] 界面无残余中文
