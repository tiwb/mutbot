# Agent 图标系统 & 虚拟滚动 设计规范

**状态**：✅ 已完成
**日期**：2026-02-26
**类型**：功能设计
**关联**：TASKS.md T6

## 1. 背景

### 1.1 图标系统：多处硬编码映射

图标/显示名信息分散在 4 处，新增 Agent 角色需要全部手动更新：

| 位置 | 文件 | 内容 |
|------|------|------|
| `_SESSION_DISPLAY` | `mutbot/builtins/menus.py:15` | 全限定名 → (显示名, 图标) |
| `_KIND_MAP` | `mutbot/web/routes.py:453` | 全限定名 → kind |
| `_TYPE_DISPLAY` | `mutbot/web/routes.py:475` | 全限定名 → (显示名, 图标) |
| `getSessionIcon` | `frontend/SessionIcons.tsx:53` | kind → SVG 组件（switch）|

- 图标与 kind 绑死，前端只认 5 个固定值
- 每个图标都是手写 inline SVG，维护成本高
- 违背 mutobj "子类发现，零注册" 原则

### 1.2 聊天消息列表：无虚拟滚动

当前 `MessageList.tsx` 全量渲染所有消息 + `scrollIntoView`，存在：

- **性能问题**：长对话（100+ 消息含大段代码块/Markdown）DOM 节点过多
- **无智能 auto-scroll**：始终强制滚动到底部，用户向上翻阅时被打断
- feature-web 技术选型中列入了虚拟滚动（`@tanstack/virtual`）但未实施

## 2. 设计方案

### 2.1 核心思路

1. **图标系统**：Session 子类声明 `display_icon`（ClassVar）+ 用户右键自定义 + Lucide 图标库按名渲染
2. **虚拟滚动**：引入 `react-virtuoso`，统一解决图标选择器网格和聊天消息列表两个场景

### 2.2 虚拟滚动库：react-virtuoso

选择 `react-virtuoso` 而非 `@tanstack/react-virtual`（原技术选型）的理由：

| 场景 | react-virtuoso | @tanstack/react-virtual |
|------|---------------|------------------------|
| 固定网格（图标选择器） | `VirtuosoGrid` 开箱即用 | `lanes` 模式（扁平化网格） |
| 变高列表（聊天消息） | 自动测量（ResizeObserver） | 需手动 `measureElement` ref |
| 动态高度变化（折叠/展开） | 自动检测 | 需手动 `resizeItem()` |
| **聊天 auto-scroll** | **内置 `followOutput`** | **需自行实现** |
| gzip 体积 | ~5-6KB | ~3-4KB |

`followOutput` 是决定性因素——聊天 auto-scroll 的边界情况（用户翻阅历史时不打断、新消息到达时智能判断、viewport resize 等）自行实现工作量大。

```bash
npm install react-virtuoso
```

### 2.3 图标优先级与数据流

三层优先级：**用户自定义** > **Session 子类声明** > **kind 回退默认值**

```
用户自定义 (session.config.icon)     ← 右键菜单设置，持久化
       ↓ 无则
Session 子类声明 (cls.display_icon)  ← ClassVar，不持久化
       ↓ 无则
kind 回退默认值 (KIND_FALLBACK)      ← 前端硬编码兜底
```

后端 API `_session_dict()` 返回：
```python
{
    "icon": session.config.get("icon")        # 用户自定义（可能为 None）
            or getattr(session_cls, "display_icon", "")  # 类声明
            or "",                                        # 前端用 kind 回退
}
```

### 2.4 后端：Session 子类声明图标

在 `Session` 基类上增加 ClassVar `display_name` 和 `display_icon`（不参与序列化）：

```python
class Session(mutobj.Declaration):
    display_name: ClassVar[str] = ""    # 空串时从类名推导
    display_icon: ClassVar[str] = ""    # Lucide 图标名，空串时用默认

class AgentSession(Session):
    display_name = "Agent"
    display_icon = "message-square"

class TerminalSession(Session):
    display_name = "Terminal"
    display_icon = "terminal"

class DocumentSession(Session):
    display_name = "Document"
    display_icon = "file-text"
```

```python
# builtins/guide.py
class GuideSession(AgentSession):
    display_name = "Guide"
    display_icon = "help-circle"

# builtins/researcher.py
class ResearcherSession(AgentSession):
    display_name = "Researcher"
    display_icon = "search"
```

### 2.5 后端：消除硬编码映射

- **menus.py**：删除 `_SESSION_DISPLAY`，`_session_display()` 直接读 `cls.display_name` / `cls.display_icon`
- **routes.py**：删除 `_KIND_MAP` 和 `_TYPE_DISPLAY`，`_session_kind()` 保留从类名推导的回退逻辑
- **`_session_dict()`**：增加 `icon` 字段，按优先级返回

### 2.6 后端：用户自定义图标 API

通过已有的 `session.update` RPC 更新 `config.icon` 字段：

```python
# 设置自定义图标
{ "session_id": "xxx", "config": { "icon": "rocket" } }

# 重置为默认（删除 config.icon）
{ "session_id": "xxx", "config": { "icon": null } }
```

### 2.7 前端：Lucide React 图标库

- 安装 `lucide-react`
- stroke-based 风格与现有 SVG 一致，tree-shakeable，1000+ 图标，MIT 协议

### 2.8 前端：动态图标渲染

重写 `SessionIcons.tsx`：

```typescript
import { icons } from "lucide-react";

const KIND_FALLBACK: Record<string, string> = {
  agent: "message-square",
  terminal: "terminal",
  document: "file-text",
  guide: "help-circle",
  researcher: "search",
};

export function getSessionIcon(
  kind: string,
  size = 24,
  color = "currentColor",
  iconName?: string,
) {
  const name = iconName || KIND_FALLBACK[kind] || "message-square";
  const pascal = kebabToPascal(name);
  const Icon = icons[pascal];
  if (!Icon) return <icons.MessageSquare size={size} color={color} />;
  return <Icon size={size} color={color} />;
}
```

### 2.9 前端：图标选择器组件 (IconPicker)

类似 VS Code 的图标选择器，作为弹出面板：

```
┌─────────────────────────────────┐
│ 🔍 搜索图标...                   │
├─────────────────────────────────┤
│ ☐ ☐ ☐ ☐ ☐ ☐ ☐ ☐ ☐ ☐          │
│ ☐ ☐ ☐ ☐ ☐ ☐ ☐ ☐ ☐ ☐          │
│ ☐ ☐ ☐ ☐ ☐ ☐ ☐ ☐ ☐ ☐          │
│ ...（VirtuosoGrid 虚拟滚动）     │
├─────────────────────────────────┤
│ [重置为默认]                     │
└─────────────────────────────────┘
```

**交互设计**：
- 右键 Session → "更换图标" → 弹出 IconPicker
- 不搜索时：按网格浏览全部 Lucide 图标，`VirtuosoGrid` 虚拟滚动
- 搜索时：按图标名称模糊匹配，实时过滤
- 点击图标：立即应用，关闭选择器
- "重置为默认"：清除自定义，恢复为 Session 子类声明的默认图标
- 点击外部区域关闭选择器
- Hover 图标：显示图标名称 tooltip

**定位**：选择器以 Portal 挂载，定位于触发元素附近。

### 2.10 前端：聊天消息列表虚拟滚动

用 `Virtuoso` 替换 `MessageList.tsx` 中的全量渲染：

```typescript
import { Virtuoso } from "react-virtuoso";

<Virtuoso
  data={messages}
  followOutput={(isAtBottom) => isAtBottom ? "smooth" : false}
  atBottomStateChange={(atBottom) => setIsAtBottom(atBottom)}
  itemContent={(index, msg) => renderMessage(msg, onSessionLink)}
/>
```

**核心特性**：
- **自动测量高度**：每条消息（文本、代码块、工具卡片、Markdown）高度不同，Virtuoso 通过 ResizeObserver 自动测量
- **智能 auto-scroll**：`followOutput` — 用户在底部时新消息自动滚动；用户向上翻阅时不打断
- **底部状态回调**：`atBottomStateChange` — 可用于显示 "回到底部" 按钮
- **动态高度变化**：折叠/展开工具调用卡片时自动重新测量，无需手动处理
- **初始滚动位置**：`initialTopMostItemIndex` 可设为最后一条消息

### 2.11 前端：右键菜单集成

在 Session 列表和 Tab 右键菜单中增加 "更换图标" 菜单项：

- 后端 Menu 声明 + `client_action = "change_icon"`（与 rename 等一致）
- 在 `Tab/Context` 和 `SessionList/Context` 两个 category 下各注册一个
- 前端处理 `change_icon` action，打开 IconPicker
- 选择后调用 `session.update` RPC 写入 `config.icon`

### 2.12 菜单图标统一

RpcMenu 中非 session 图标也统一改用 Lucide 图标名：
- `rename` → `pencil`
- `close` → `x`
- `stop` → `square`

## 3. 设计决策

| 项目 | 决策 |
|------|------|
| 图标库 | Lucide React（stroke 风格一致，tree-shakeable，MIT） |
| 虚拟滚动库 | react-virtuoso（`VirtuosoGrid` 图标网格 + `Virtuoso` 聊天列表，内置 `followOutput`） |
| 类属性声明 | 无类型注解的纯类属性（mutobj 不支持 ClassVar 检测） |
| 类默认图标 | 运行时从类属性读取，不持久化 |
| 用户自定义图标 | 存入 `session.config["icon"]`，持久化；未自定义时 config 中无此字段 |
| 非 session 菜单图标 | 统一改用 Lucide 图标名 |
| 图标选择器 | 弹出面板，搜索 + 网格浏览，`VirtuosoGrid` 虚拟滚动 |
| 触发位置 | Tab 右键 + Session 列表右键都支持 |
| 菜单项实现 | 后端 Menu 声明 + `client_action = "change_icon"` |

## 4. 实施步骤清单

### 阶段一：依赖安装与基础设施 [✅ 已完成]
- [x] **Task 1.1**: 安装依赖
  - [x] `npm install lucide-react react-virtuoso`
  - 状态：✅ 已完成

- [x] **Task 1.2**: 重写 SessionIcons.tsx
  - [x] 改用 Lucide 动态渲染（`icons[pascalName]`）
  - [x] 保留 kind → 默认图标名的 KIND_FALLBACK
  - [x] 支持 iconName 参数（优先级最高）
  - [x] 导出 `renderLucideIcon` 供 RpcMenu 使用
  - [x] 导出 `getAllIconNames` 供 IconPicker 使用
  - 状态：✅ 已完成

- [x] **Task 1.3**: 更新前端 Session 类型与消费方
  - [x] App.tsx 中 Session interface 增加 `icon: string`
  - [x] Tab 渲染、Session 列表传递 icon 参数
  - 状态：✅ 已完成

### 阶段二：后端元数据声明 [✅ 已完成]
- [x] **Task 2.1**: Session 基类增加 display_name / display_icon（无类型注解，不参与 mutobj 字段处理）
  - [x] 修改 mutbot/session.py
  - [x] 各子类（AgentSession、TerminalSession、DocumentSession、GuideSession、ResearcherSession）声明值
  - **注意**：mutobj 不支持 ClassVar 检测，改用无类型注解的纯类属性
  - 状态：✅ 已完成

- [x] **Task 2.2**: 消除硬编码映射 & API 增加 icon 字段
  - [x] menus.py：删除 `_SESSION_DISPLAY`，改读类属性
  - [x] routes.py：删除 `_KIND_MAP` / `_TYPE_DISPLAY`，改读类属性
  - [x] `_session_dict()` 增加 `icon` 字段（优先级：config.icon > cls.display_icon）
  - 状态：✅ 已完成

### 阶段三：聊天消息虚拟滚动 [✅ 已完成]
- [x] **Task 3.1**: MessageList 改用 Virtuoso
  - [x] 用 `Virtuoso` 替换全量渲染
  - [x] `followOutput` 智能 auto-scroll
  - [x] 自定义 Scroller 组件，保留滚动条样式
  - [x] CSS 调整：message 间距、用户消息 margin-left:auto 替代 align-self:flex-end
  - 状态：✅ 已完成

### 阶段四：图标选择器 [✅ 已完成]
- [x] **Task 4.1**: 实现 IconPicker 组件
  - [x] 搜索框 + `VirtuosoGrid` 网格浏览
  - [x] 点击选择、hover tooltip（显示 kebab-case 名）、"重置为默认"按钮
  - [x] Portal 挂载 + 点击外部关闭 + Escape 关闭
  - 状态：✅ 已完成

- [x] **Task 4.2**: 右键菜单 "更换图标" 集成
  - [x] 后端：在 `Tab/Context` 和 `SessionList/Context` 注册 ChangeIconMenu（client_action）
  - [x] 前端 App.tsx：处理 `change_icon` action，打开 IconPicker
  - [x] 前端 SessionListPanel.tsx：通过 `onChangeIcon` 回调向 App.tsx 传递
  - [x] 选择后调用 `session.update` RPC 写入 `config.icon`
  - 状态：✅ 已完成

### 阶段五：菜单图标统一 [✅ 已完成]
- [x] **Task 5.1**: RpcMenu 非 session 图标改用 Lucide
  - [x] 后端 Menu display_icon 改为 Lucide 图标名（pencil, x, square, palette 等）
  - [x] 前端 RpcMenu 图标渲染改用 `renderLucideIcon`
  - 状态：✅ 已完成

## 5. 测试验证

### 图标系统
- [x] 各 Session 类型图标在 Tab 栏 / Session 列表 / RpcMenu 正确显示
- [x] 新增 Agent 角色时，只需声明 display_icon，无需改前端
- [x] 未声明 display_icon 的 Session 子类使用 kind 回退默认图标
- [x] 右键菜单 "更换图标" 弹出图标选择器
- [x] 搜索图标正常过滤
- [x] 选择图标后立即生效（Tab + Session 列表同步更新）
- [x] "重置为默认" 清除自定义图标
- [x] 刷新页面后自定义图标保持（持久化到 config）

### 聊天虚拟滚动
- [x] 长对话（100+ 消息）滚动流畅，无卡顿
- [x] 用户在底部时，新消息自动滚动到底部
- [x] 用户向上翻阅历史时，新消息到达不打断滚动位置
- [x] 工具卡片折叠/展开时布局正确，不跳动
- [x] Markdown 渲染（代码块、表格等）高度正确测量

### 视觉验证
- [x] Lucide 图标风格与 UI 一致（stroke-based）
- [x] 图标在 16px / 24px 下清晰
- [x] 图标选择器布局美观，虚拟滚动流畅
- [x] 图标选择器定位正确，不溢出视口
