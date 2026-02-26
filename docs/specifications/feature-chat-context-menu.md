# 聊天消息右键菜单与显示模式 设计规范

**状态**：✅ 已完成
**日期**：2026-02-26
**类型**：功能设计
**来源**：TASKS.md T5

## 1. 背景

当前 `MessageList` 的右键菜单使用前端自建的 `ContextMenu` 组件，硬编码 Copy / Select All 两个操作，不区分消息类型。需要：
1. 右键菜单按消息类型（user / assistant text / tool_group / error）提供不同菜单项
2. Assistant 文本消息支持「复制 Markdown 源码」和「切换 Markdown 渲染/源码显示」
3. 显示模式偏好持久化到 localStorage
4. 使用服务端 Menu 系统（`RpcMenu`），保持架构统一且可扩展

### 现状分析

| 组件 | 文件 | 职责 |
|------|------|------|
| `RpcMenu` | `components/RpcMenu.tsx` | 服务端驱动的菜单组件，支持 context mode 和 dropdown mode |
| `Menu` | `mutbot/menu.py` | 菜单基类（Declaration 子类），mutobj 自动发现 |
| `MenuRegistry` | `mutbot/runtime/menu_impl.py` | 按 category 查询、执行菜单项 |
| `MessageList` | `components/MessageList.tsx` | 消息列表，当前用自建 ContextMenu |
| `Markdown` | `components/Markdown.tsx` | react-markdown 渲染，Shiki 代码高亮 |
| `CodeBlock` | `components/CodeBlock.tsx` | Shiki 语法高亮的代码块 |

**消息类型**（`MessageList.tsx:6-10`）：
```typescript
type ChatMessage =
  | { id: string; role: "user"; type: "text"; content: string }
  | { id: string; role: "assistant"; type: "text"; content: string }
  | { id: string; role: "assistant"; type: "tool_group"; data: ToolGroupData }
  | { id: string; role: "assistant"; type: "error"; content: string }
```

**RpcMenu 系统工作流**：
1. 前端 `<RpcMenu category="..." context={...}>` 挂载时调用 `menu.query` RPC
2. 服务端 `MenuRegistry` 按 category 找到所有 `Menu` 子类，通过 `check_visible(context)` 过滤
3. 前端渲染菜单项，用户点击后：
   - 有 `client_action`：前端 `onClientAction` 处理，不走服务端
   - 无 `client_action`：调用 `menu.execute` RPC 由服务端处理

## 2. 设计方案

### 2.1 菜单 category 与 context

**category**：`"MessageList/Context"`

**context**（前端传入）：
```typescript
{
  message_role: "user" | "assistant",
  message_type: "text" | "tool_group" | "error",
  markdown_mode: "rendered" | "source"  // 当前显示模式，用于动态菜单名
}
```

### 2.2 服务端 Menu 子类定义

在 `mutbot/builtins/menus.py` 中新增以下 Menu 子类，均为 `client_action` 模式（纯前端操作）：

| Menu 子类 | client_action | display_order | 可见条件 |
|-----------|---------------|---------------|----------|
| `CopySelectionMenu` | `"copy_selection"` | `"0edit:0"` | 始终可见 |
| `SelectAllMenu` | `"select_all"` | `"0edit:1"` | 始终可见 |
| `CopyMarkdownMenu` | `"copy_markdown"` | `"1markdown:0"` | assistant + text |
| `ToggleMarkdownModeMenu` | `"toggle_markdown_mode"` | `"1markdown:1"` | assistant + text |

**分组效果**（order 前缀不同自动产生分隔线）：
```
  复制选中文本
  全选
  ─────────────
  复制 Markdown 源码
  切换为源码显示 / 切换为渲染显示
```

**可见性控制**：`CopyMarkdownMenu` 和 `ToggleMarkdownModeMenu` 通过 `check_visible` 检查 context 中 `message_role == "assistant"` 且 `message_type == "text"`。

**动态名称**：`ToggleMarkdownModeMenu` 通过 `dynamic_items` 根据 `markdown_mode` context 返回不同 display_name（"切换为源码显示" / "切换为渲染显示"）。

### 2.3 前端 MessageList 改造

1. **替换 ContextMenu 为 RpcMenu**：移除自建 `ContextMenu` 引用，改用 `<RpcMenu>` context mode
2. **右键处理**：`onContextMenu` 从 DOM 查找 `.message[data-msg-id]` 确定消息类型，设置 context state
3. **`onClientAction` 处理**：
   - `copy_selection`：`document.execCommand("copy")` 或 Clipboard API
   - `select_all`：选中当前消息元素内全部文本
   - `copy_markdown`：根据 `data-msg-id` 找到消息对象，将 `content` 写入剪贴板
   - `toggle_markdown_mode`：切换 `markdownMode` state 并写 localStorage

### 2.4 Markdown 显示模式切换

**两种模式**：
- **渲染模式**（默认）：当前的 `<Markdown>` 组件
- **源码模式**：复用 `<CodeBlock language="markdown">` 渲染原始 content

**复制粒度**：「复制 Markdown 源码」复制整条消息的完整 markdown content。

**切换粒度**：全局偏好，切换后影响所有 assistant 文本消息。

Shiki 已支持 markdown 语言高亮（`lib/shiki.ts` 的 bundledLanguages 包含 `markdown`）。

### 2.5 偏好持久化

- **localStorage key**：`"mutbot-markdown-display-mode"`
- **值**：`"rendered"` | `"source"`
- **默认值**：`"rendered"`

### 2.6 MessageList 新增 props

`MessageList` 需要接收 `rpc: WorkspaceRpc | null`，由 `AgentPanel` 传入，用于 `<RpcMenu>` 的 RPC 调用。

## 3. 实施步骤清单

### 阶段一：服务端菜单定义 [✅ 已完成]
- [x] **Task 1.1**: 在 `mutbot/builtins/menus.py` 新增 4 个 Menu 子类
  - [x] `CopySelectionMenu`（client_action, 始终可见）
  - [x] `SelectAllMenu`（client_action, 始终可见）
  - [x] `CopyMarkdownMenu`（client_action, check_visible 按消息类型）
  - [x] `ToggleMarkdownModeMenu`（dynamic_items 动态名称, check_visible 按消息类型）
  - 状态：✅ 已完成

### 阶段二：前端 MessageList 改造 [✅ 已完成]
- [x] **Task 2.1**: 消息元素添加 `data-msg-id` 属性
  - 状态：✅ 已完成

- [x] **Task 2.2**: 替换 ContextMenu 为 RpcMenu
  - [x] 移除自建 ContextMenu 引用
  - [x] 新增 `rpc` prop，由 AgentPanel 传入
  - [x] `onContextMenu` 查找消息类型，构建 context 传给 RpcMenu
  - [x] 实现 `onClientAction` 处理 4 种 client_action
  - 状态：✅ 已完成

- [x] **Task 2.3**: Markdown 显示模式切换
  - [x] 新增 `markdownMode` state，初始值从 localStorage 读取
  - [x] assistant text 消息根据 markdownMode 条件渲染 `<Markdown>` 或 `<CodeBlock>`
  - [x] `toggle_markdown_mode` action 切换 state 并写 localStorage
  - 状态：✅ 已完成

### 阶段三：样式适配 [✅ 已完成]
- [x] **Task 3.1**: 源码模式样式调整
  - [x] 源码模式复用 CodeBlock 组件，样式已内建协调
  - 状态：✅ 已完成

## 4. 测试验证

### 手动测试
- [ ] 右键 user 消息：显示 复制/全选，不显示 Markdown 相关项
- [ ] 右键 assistant text 消息：显示全部 4 项，分隔线正确
- [ ] 右键 tool_group 消息：仅显示 复制/全选
- [ ] 右键 error 消息：仅显示 复制/全选
- [ ] 切换显示模式后所有 assistant text 消息同步切换
- [ ] 菜单项名称随当前模式动态变化
- [ ] 刷新页面后偏好设置保留
- [ ] 复制 Markdown 源码后粘贴内容正确
