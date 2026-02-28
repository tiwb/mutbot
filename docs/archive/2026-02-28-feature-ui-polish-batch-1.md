# UI 体验优化（批次一） 设计规范

**状态**：✅ 已完成
**日期**：2026-02-28
**类型**：功能设计

## 1. 背景

多项 UI 体验改进，涉及空标签页引导、Session 创建交互、右键菜单增强、工具执行状态指示器优化、用户消息显示延迟修复。

## 2. 设计方案

### 2.1 空标签页欢迎引导页

**场景**：所有标签页关闭时，主内容区域显示欢迎页。

**实现方式**：在 `App.tsx` 中检测 FlexLayout model 中是否有 tab 节点。若无 tab，在 `main-content` 区域覆盖渲染欢迎组件，替代空白的 FlexLayout。

**欢迎页内容**：
- 大号 "MutBot" 文字：深色半透明（`rgba(255,255,255,0.06)`），类似 VS Code 水印效果
  - 字号约 72px，字重 bold，居中显示
- 两行引导链接（居中，简洁排列）：
  1. 图标 `circle-question-mark` + "Guide Agent — 开始体验 AI 助手"
  2. 图标 `terminal` + "Terminal — 在命令行下工作"
- 整体文字颜色柔和低调（`var(--text-dim)` 级别），不跳脱
- 点击引导链接 → 调用 `menu.execute`（复用 `AddSessionMenu`）创建对应 Session → 触发 `handleMenuResult` → 打开 Tab

**检测逻辑**：新增 `hasOpenTabs` 状态，在 `handleModelChange` 时遍历 model 检测是否存在 tab 节点。

**新文件**：`frontend/src/components/WelcomePage.tsx`

### 2.2 全部标签关闭时新建 Session 自动打开

**场景**：左右分屏都没有标签页时，用户通过 Session 列表或其他方式创建 Session，应自动打开该 Session 的 Tab。

**实现方式**：在 `session_created` 事件监听中，检测当前是否 `!hasOpenTabs`。若无打开的 tab，自动调用 `addTabForSession(session)` 打开新建的 Session。

**注意**：仅限「无 tab」状态下自动打开。如果已有 tab 打开，保持现有行为（不自动打开），因为用户可能只是在 Session 列表创建备用 Session。

### 2.3 Session 列表空白区域右键菜单（含子菜单支持）

**场景**：在 Session 列表面板的空白区域右键，弹出菜单，包含"新建 Session"子菜单。

#### 后端 — 子菜单支持（通用机制）

在 `Menu` 类上新增 `display_submenu_category` 属性。指定了此属性的菜单项，前端自动将其渲染为带子菜单的父项，子菜单内容通过查询该 category 获取。

**`mutbot/menu.py`** — Menu 类新增属性：
```python
class Menu(mutobj.Declaration):
    ...
    display_submenu_category: str = ""   # 非空时，此菜单项作为子菜单父项，子菜单内容为该 category
```

**`mutbot/runtime/menu_impl.py`** — 序列化时传递 `submenu_category`：
- `_item_to_dict` 在 MenuItem 有 `submenu_category` 时，加入 `"submenu_category"` 字段
- `query()` 处理静态菜单时，读取 `display_submenu_category` 写入 MenuItem

**`MenuItem` dataclass** 新增字段：
```python
@dataclass
class MenuItem:
    ...
    submenu_category: str = ""   # 非空时表示这是一个子菜单触发项
```

**新增后端菜单** — `NewSessionBlankMenu`（category = `SessionList/Blank`）：
```python
class NewSessionBlankMenu(Menu):
    display_name = "New Session"
    display_icon = "plus"
    display_category = "SessionList/Blank"
    display_order = "0new:0"
    display_submenu_category = "SessionPanel/Add"   # 子菜单复用现有 Add 菜单
```
无需 `dynamic_items`，无需 `execute`。前端看到 `submenu_category` 后自动请求 `SessionPanel/Add` 的菜单项作为子菜单。

#### 前端 — RpcMenu 子菜单渲染

- `RpcMenuItem` 接口新增 `submenu_category?: string`
- 渲染逻辑：如果 item 有 `submenu_category`，显示右箭头 `▸`，鼠标悬停时：
  1. 调用 `rpc.call("menu.query", { category: submenu_category })` 获取子菜单项
  2. 在父项右侧弹出子菜单面板
  3. 子菜单项的点击走正常的 `handleExecute` 流程
- 缓存：同一菜单打开期间，已加载的子菜单不重复请求

#### SessionListPanel 集成

- `.session-list` 容器增加 `onContextMenu` 处理
- 检测右键目标不在 session item 上时，打开 `<RpcMenu category="SessionList/Blank">`
- 菜单结果回调走 `handleMenuResult`（通过新增 props 传给 SessionListPanel）

### 2.4 工具执行闪烁绿点

**场景**：工具运行时的旋转图标 (↻ spin) 与底部状态栏的 spinner 视觉冲突。改为闪烁绿点。

**修改**：
- `ToolCallCard.tsx`：running 状态图标从 `\u21bb`（↻）改为 `\u25cf`（●，实心圆点）
- CSS 修改：
  ```css
  .tool-card.running {
    border-color: var(--accent-green);
  }
  .tool-card.running .tool-card-status {
    color: var(--accent-green);
    animation: blink 1.2s ease-in-out infinite;
  }
  @keyframes blink {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }
  ```

### 2.5 用户消息显示延迟修复

**根因分析**：
- 前端 `handleSend` 只发送 WebSocket 消息并设置 `agentStatus`，**不在本地插入用户消息**
- 用户消息仅在后端广播 `user_message` 事件回来后才渲染
- 对已活跃 session（bridge 已存在），后端 `bridge.send_message()` 使用 `asyncio.ensure_future` 广播，延迟极小
- **对新建/stopped session，第一条消息触发 `sm.start()` 做 lazy 初始化**（加载历史 JSON、import 模块、创建 provider、tool discovery），全部完成后才广播 `user_message`，造成明显延迟

**修复方案**：后端在 `routes.py` 的 WebSocket handler 中，**先广播 `user_message` 事件，再执行 `sm.start()`**。

```python
# routes.py — handle "message" 分支
if msg_type == "message":
    text = raw.get("text", "")
    if text:
        # 立即广播 user_message，不等 bridge 初始化
        user_event = {
            "type": "user_message",
            "id": _generate_event_id(),
            "text": text,
            "timestamp": _now_iso(),
        }
        await connection_manager.broadcast(session_id, user_event)

        # 然后再处理 bridge 初始化和消息投递
        if bridge is None:
            bridge = sm.start(session_id, loop, connection_manager.broadcast)
        bridge.send_message(text, data, skip_broadcast=True)  # 不重复广播
```

需要同步修改 `AgentBridge.send_message()`，增加 `skip_broadcast` 参数，避免重复广播。同时需要确保持久化逻辑（将 user message 写入 `chat_messages`）仍然在 bridge 中正确执行。

## 3. 待定问题

（已全部确认，无待定问题）

## 4. 实施步骤清单

### 阶段一：基础设施 — 子菜单支持 [✅ 已完成]
- [x] **Task 1.1**: Menu/MenuItem 添加 submenu_category
  - [x] `mutbot/menu.py`：Menu 类新增 `display_submenu_category = ""`
  - [x] `mutbot/menu.py`：MenuItem 新增 `submenu_category: str = ""`
  - [x] `mutbot/runtime/menu_impl.py`：`_item_to_dict` 序列化 `submenu_category`
  - [x] `mutbot/runtime/menu_impl.py`：`query()` 读取 `display_submenu_category` 写入 MenuItem
  - 状态：✅ 已完成

- [x] **Task 1.2**: RpcMenu 前端支持子菜单渲染
  - [x] `RpcMenuItem` 接口增加 `submenu_category?: string`
  - [x] 渲染逻辑：hover 展开子菜单（rpc.call 获取子菜单项）、定位在父项右侧
  - [x] 子菜单项的 click 走 `handleExecute`
  - [x] SubMenu 独立组件，延迟展开/关闭避免抖动
  - 状态：✅ 已完成

### 阶段二：功能实现 [✅ 已完成]
- [x] **Task 2.1**: 欢迎引导页组件
  - [x] 新建 `WelcomePage.tsx`：MutBot 大字水印 + 2 行引导链接（图标+说明）
  - [x] CSS 样式（水印文字、链接行、hover 效果，文字柔和不跳脱）
  - [x] 点击链接调用 `menu.execute` 创建对应 Session
  - 状态：✅ 已完成

- [x] **Task 2.2**: App.tsx 集成欢迎页
  - [x] 新增 `hasOpenTabs` 状态检测（handleModelChange + modelHasTabs helper）
  - [x] 在 `main-content` 中条件渲染 `WelcomePage`（叠加在 Layout 上）
  - 状态：✅ 已完成

- [x] **Task 2.3**: 全部标签关闭时自动打开新建 Session
  - [x] `session_created` 事件监听中检测 `!hasOpenTabsRef.current` 并自动 `addTabForSession`
  - 状态：✅ 已完成

- [x] **Task 2.4**: Session 列表空白区域右键菜单
  - [x] 后端新增 `NewSessionBlankMenu`（category `SessionList/Blank`，submenu = `SessionPanel/Add`）
  - [x] `SessionListPanel` 空白区域 onContextMenu → `<RpcMenu category="SessionList/Blank">`
  - [x] 新增 `onMenuResult` prop，菜单结果回调打开新 tab
  - 状态：✅ 已完成

- [x] **Task 2.5**: 工具执行绿点闪烁
  - [x] `ToolCallCard.tsx`：running 图标改为 `●`（\u25cf）
  - [x] CSS：动画改为 blink，颜色改为 accent-green，边框同步
  - 状态：✅ 已完成

- [x] **Task 2.6**: 用户消息延迟修复
  - [x] `routes.py`：lazy start 路径中先 `await broadcast` user_message 再执行 `sm.start()`
  - [x] `agent_bridge.py`：`send_message` 增加 `skip_user_broadcast` keyword 参数
  - [x] 持久化逻辑不受影响（bridge 仍执行 `_append_chat_message`）
  - 状态：✅ 已完成

---

### 实施进度总结
- ✅ **阶段一：基础设施** — 100% 完成 (2/2 任务)
- ✅ **阶段二：功能实现** — 100% 完成 (6/6 任务)

**TypeScript 编译：✅ 通过（零错误）**
**Python import 验证：✅ 通过**

## 5. 测试验证

### 手动测试
- [x] 关闭所有 tab → 显示欢迎页（MutBot 大字 + 两行引导链接）
- [x] 点击 Guide Agent 链接 → 创建 GuideSession 并打开 tab → 欢迎页消失
- [x] 点击 Terminal 链接 → 创建 TerminalSession 并打开 tab
- [x] 关闭所有 tab 后，从 Session 列表右键空白 → "New Session" → 子菜单选择类型 → 创建并自动打开
- [x] 关闭所有 tab 后，通过任意方式新建 Session → 自动打开该 tab
- [x] 有 tab 打开时，新建 Session 不自动打开 tab
- [x] 工具执行时显示绿色闪烁圆点，不再旋转
- [x] 已活跃 session 发送消息 → 用户消息即时显示
- [x] 新建 session 发送第一条消息 → 用户消息即时显示（不被 bridge 初始化阻塞）
- [x] 子菜单 hover 展开、点击创建、自动关闭
