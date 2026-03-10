# 移动平台 Workspace 适配 设计规范

**状态**：✅ 已完成
**日期**：2026-03-10
**类型**：功能设计

## 背景

当前 mutbot 的 workspace 布局基于 PC/大屏幕设计：

- **Sidebar（260px）+ Flexlayout 面板系统**：拖拽分割、多面板并排、可停靠标签页
- **最小宽度假设**：侧边栏最小 150px + 主内容区，至少需要 ~500px 才可用
- **交互模式**：鼠标悬停、右键菜单、拖拽调整大小、拖拽排序

手机屏幕（320-428px 宽）下，这套布局完全不可用。需要专门的移动端适配方案，可能整体交互逻辑都不同。

## 前置清理：移除 LogPanel

LogPanel（前端日志查看器）需要在本功能之前移除，原因是日志含敏感信息，不应通过前端暴露。

**删除范围**（仅前端 UI + 面向前端的查询/推送 API）：

| 文件 | 删除内容 |
|------|---------|
| `frontend/src/panels/LogPanel.tsx` | 整个文件 |
| `frontend/src/panels/PanelFactory.tsx` | LogPanel 的 import / lazy / case 分支 |
| `frontend/src/lib/layout.ts` | `PANEL_LOG` 常量 |
| `frontend/src/index.css:2080-2191` | `.log-panel` 相关全部样式 |
| `src/mutbot/web/rpc_workspace.py:217-240` | `log.query` RPC handler |
| `src/mutbot/web/routes.py:86-88, 105-134` | `_get_log_store()` 辅助函数 + `/ws/logs` WebSocket 端点 |
| `tests/test_rpc_handlers.py` | `TestLogHandlers` 测试类 |

**保留不动**（后端日志基础设施）：

| 保留内容 | 作用 |
|---------|------|
| `LogStore` + `LogStoreHandler`（server.py） | 内存日志缓冲区，挂在 root logger 上捕获全量 Python 日志到环形缓冲，供后端诊断（CLI `log_query`、未来其他消费者） |
| `app.state.log_store` | LogStore 实例挂在 app state，后端模块按需查询 |
| `remote-log.ts` | 前端日志**上报**到后端（方向：前端→后端），不是查看功能 |
| FileHandler + 磁盘日志 | CLI 工具 `log_query` 的数据源 |

## 设计方案

### 核心思路：响应式双模式

同一套代码根据屏幕宽度切换布局模式：

- **桌面模式**（≥768px）：保持现有 flexlayout 面板系统不变
- **移动模式**（<768px）：全屏单面板 + 顶栏快捷切换 + 抽屉式导航

### 移动模式布局

```
┌──────────────────────────┐
│ ☰  [S1] [S2] [S3]    ⋮  │  ← 顶栏：汉堡菜单 + 最近 session 图标 + 操作按钮
├──────────────────────────┤
│                          │
│    全屏单面板内容         │  ← 当前 session（AgentPanel / TerminalPanel）
│                          │
│                          │
├──────────────────────────┤
│    [聊天输入框]           │  ← Agent 面板时显示
└──────────────────────────┘

抽屉（汉堡菜单或右滑打开）：
┌──────────────┬───────────┐
│  Session     │           │
│  List        │   遮罩    │
│              │           │
│  S1 ●        │           │
│  S2 ○        │           │
│  S3 ○        │           │
│              │           │
│  + 新建      │           │
└──────────────┴───────────┘
```

### 关键设计决策

**1. 检测方式：CSS 媒体查询 + JS 断点检测**

- CSS `@media (max-width: 767px)` 控制样式
- JS 通过 `window.matchMedia` 暴露 `isMobile` 状态
- 不做 UA 嗅探（同一设备横竖屏可能切换模式）

**2. 移动模式下放弃 Flexlayout**

Flexlayout-react 的拖拽分割面板在小屏上无意义。移动模式下：
- 不渲染 flexlayout 组件
- 改用**全屏单面板**，通过顶栏 session 图标切换
- 面板切换保留组件状态（不销毁）

**3. 顶栏 Session 快捷切换**

类似桌面 sidebar 收紧状态的图标栏，移到顶栏：
- 显示最近若干个 session 图标，当前 session 高亮
- 点击图标直接切换 session
- 溢出不显示，完整列表在抽屉中操作

**4. Session 列表 → 抽屉式导航**

- 点击汉堡菜单或右滑手势打开
- 半屏宽度（~80vw），带半透明遮罩
- 点击遮罩或左滑关闭
- 支持新建、删除、重命名等完整 session 管理操作

**5. 面板适配**

| 面板 | 移动端处理 |
|------|-----------|
| AgentPanel | 全屏聊天，输入框固定底部，消息气泡宽度自适应 |
| TerminalPanel | 全屏终端，xterm.js 自适应容器宽度 |
| CodeEditorPanel | 暂不支持（功能尚不可用），移动模式下不注册 |
| LogPanel | 已移除（见前置清理章节） |

**6. 布局持久化：桌面/移动独立保存**

桌面切移动不丢失桌面布局，反之亦然：

```json
{
  "layout_desktop": { "...flexlayout model JSON..." },
  "layout_mobile": {
    "activeSessionId": "xxx",
    "recentSessions": ["id1", "id2", "id3"]
  }
}
```

后端 `workspace.update` 需要区分两种布局的读写。

### 实现层次

```
App.tsx
├── useMobileDetect()          ← 新 hook：检测是否移动模式
├── 桌面模式 → 现有 Sidebar + Flexlayout
└── 移动模式 → MobileLayout
    ├── MobileTopBar            ← 顶栏（汉堡菜单 + session 图标栏 + 操作）
    ├── MobileDrawer            ← 抽屉式 Session 列表（复用 SessionListPanel 逻辑）
    └── MobilePanelContainer    ← 全屏面板容器（复用现有 Panel 组件）
```

### 触摸交互

- **点遮罩**：关闭抽屉
- **顶栏图标点击**：切换 session（触摸 < 200ms 松开）
- **顶栏图标长按**：弹出上下文菜单（触摸 > 300ms 未移动）
- **顶栏图标长按后拖动**：排序（触摸 > 300ms 后移动）
- 抽屉内 session 列表沿用桌面端交互（已有右键菜单、拖拽排序）
- 滑动手势（左右切换 tab、边缘滑出抽屉）暂不做——与浏览器导航手势冲突

#### 长按菜单设计

顶栏 session 图标长按后弹出 RpcMenu，复用 `SessionList/Context` category（与桌面端右键菜单一致）。

**手势判定逻辑**（在 MobileLayout 中实现）：

```
touchstart → 记录起点坐标 + 启动 300ms 定时器
  ├── 定时器触发前松开（< 300ms）→ onClick 切换 tab
  ├── 定时器触发前移动 > 10px → 取消长按（普通滚动）
  └── 定时器触发（>= 300ms 且未移动）→ 进入长按状态
      ├── 松开 → 弹出 RpcMenu
      └── 开始移动 → 进入拖动排序模式（Phase 2.8b，后续实现）
```

**RpcMenu 触发方式**：长按完成后记录 `{ sessionId, position }` 状态，渲染 RpcMenu 组件（与桌面端 SessionListPanel 中的上下文菜单一致）。

#### 拖动排序设计（后续实现）

长按进入拖动模式后：
- 被拖动图标半透明跟手移动
- 其他图标根据拖动位置实时让位（CSS transition）
- 松手后调用 `onReorderSessions` + RPC `workspace.reorder_sessions`
- 复用桌面端 SessionListPanel 的 reorder 逻辑

### CSS 策略

不新建 CSS 文件，在 `index.css` 末尾增加移动端样式块：

```css
/* ===== Mobile Layout ===== */
@media (max-width: 767px) {
  /* 隐藏桌面组件 */
  /* 移动端布局样式 */
  /* 触摸优化 */
}
```

### 已确认决策

- **断点阈值**：768px。iPad Mini 竖屏刚好桌面模式，手机横屏保持移动模式。
- **Tablet 中间态**：先只做两档（桌面/移动），后续根据反馈再考虑。
- **手势库**：手写 touch 事件，只需左右滑动，不引入额外依赖。
- **Welcome 页面**：复用现有 Welcome 页面，调整为单列竖向排列，按钮全宽。
- **移动端 Terminal**：体验待专门设计，本规范暂不涉及。

## 分阶段实施计划

### Phase 0：前置清理

移除 LogPanel（独立于移动端，先做掉）。

### Phase 1：最基础移动端体验

目标：手机上能正常使用 Agent 聊天。

- `useMobileDetect()` hook（CSS 媒体查询 + JS matchMedia）
- MobileLayout 骨架：顶栏（汉堡菜单 + 当前 session 标题）+ 全屏面板
- MobileDrawer：点击汉堡菜单开关，复用 SessionListPanel 的 session 列表逻辑
- AgentPanel 全屏适配（消息气泡宽度、输入框固定底部）
- 基础 CSS 媒体查询（隐藏桌面 sidebar/flexlayout）

### Phase 2：交互增强与体验修复

目标：修复 Phase 1 的体验问题，提升 session 切换效率。

- 抽屉中 SessionListPanel 强制展开模式（当前只显示图标，无文字）
- Terminal 面板移动端可用（直接渲染 TerminalPanel，不禁止）
- 顶栏 session 图标栏：所有 session 按列表顺序排列，当前 session 原位展开显示「图标+名称」，其他 session 收紧为图标，点击切换
- 滑动手势开关抽屉（左边缘右滑打开，左滑/点遮罩关闭）
- 长按上下文菜单（替代右键，session 重命名/删除等）

### Phase 3：持久化与精细适配

目标：完善状态管理和各页面适配。

- 布局持久化分离（`layout_desktop` / `layout_mobile` 独立保存）
- Welcome 页面移动端适配（单列竖排，按钮全宽）
- Terminal 移动端专项适配（单独设计）

## 关键参考

### 源码
- `frontend/src/App.tsx` — 主应用组件，workspace 路由 + flexlayout 管理（~1115 行）
- `frontend/src/panels/SessionListPanel.tsx` — 侧边栏 session 列表
- `frontend/src/panels/AgentPanel.tsx` — Agent 聊天面板
- `frontend/src/panels/TerminalPanel.tsx` — 终端面板
- `frontend/src/panels/PanelFactory.tsx` — 面板工厂
- `frontend/src/lib/layout.ts` — Flexlayout 模型配置
- `frontend/src/lib/types.ts` — Session/Workspace 类型定义
- `frontend/src/index.css` — 全局样式（3349 行）

### App.tsx 关键结构
- `sessions: Session[]`（line 90）、`activeSessionId`（line 91）— session 状态
- `handleSelectSession()`（line 462-514）— session 选择逻辑
- `addTabForSession()`（line 355-405）— session→面板映射（kind→PANEL 常量）
- `panelFactory()`（line 970-983）— flexlayout 面板工厂
- 桌面布局渲染（line 1044-1084）：`.app-layout` > `.sidebar` + `.sidebar-resize-handle` + `.main-content`
- WelcomePage 渲染（line 1080-1082）：`!hasOpenTabs` 时显示

### 现有布局参数
- Sidebar 展开宽度：260px，收起宽度：48px
- Flexlayout splitter：4px
- Tab bar 高度：35px
- 唯一的现有媒体查询：`@media (max-width: 720px)` 仅调整 ws-selector padding

### 相关规范
- `docs/specifications/bugfix-workspace-hash-routing.md` — Workspace hash 路由

## 实施步骤清单

### Phase 0：前置清理 — 移除 LogPanel [✅ 已完成]

- [x] **Task 0.1**: 删除前端 LogPanel 组件及注册
  - [x] 删除 `frontend/src/panels/LogPanel.tsx` 文件
  - [x] `PanelFactory.tsx`：移除 LogPanel 的 import、lazy 声明、case 分支
  - [x] `lib/layout.ts`：移除 `PANEL_LOG` 常量导出
  - 状态：✅ 已完成

- [x] **Task 0.2**: 删除 LogPanel 相关 CSS
  - [x] `index.css`：删除 `.log-panel` 到 `.log-msg` 的全部样式
  - 状态：✅ 已完成

- [x] **Task 0.3**: 删除面向前端的日志查询/推送 API
  - [x] `rpc_workspace.py`：删除 `log.query` RPC handler
  - [x] `routes.py`：删除 `_get_log_store()` 辅助函数和 `/ws/logs` WebSocket 端点
  - 状态：✅ 已完成

- [x] **Task 0.4**: 删除 LogPanel 相关测试
  - [x] `tests/test_rpc_handlers.py`：删除 `TestLogHandlers` 类 + 从注册验证中移除 `"log.query"`
  - 状态：✅ 已完成

- [x] **Task 0.5**: 构建验证
  - [x] 前端 `npm run build` 通过
  - [x] 后端测试已有失败（RpcContext 签名变更），非本次引入
  - 状态：✅ 已完成

### Phase 1：最基础移动端体验 [✅ 已完成]

- [x] **Task 1.1**: 实现 `useMobileDetect()` hook
  - [x] 新建 `frontend/src/lib/useMobileDetect.ts`
  - [x] 基于 `useSyncExternalStore` + `window.matchMedia("(max-width: 767px)")`
  - [x] 返回 `isMobile: boolean`，响应屏幕旋转/窗口缩放
  - 状态：✅ 已完成

- [x] **Task 1.2**: 实现 MobileLayout 骨架组件
  - [x] 新建 `frontend/src/mobile/MobileLayout.tsx`
  - [x] 包含：MobileTopBar（汉堡菜单 + 当前 session 标题 + 连接状态）+ 全屏面板容器
  - [x] 复用 AgentPanel，通过 `activeSessionId` 决定渲染
  - 状态：✅ 已完成

- [x] **Task 1.3**: 实现 MobileDrawer 抽屉组件
  - [x] 新建 `frontend/src/mobile/MobileDrawer.tsx`
  - [x] 点击汉堡菜单按钮开关，带半透明遮罩
  - [x] 直接嵌入 SessionListPanel 组件
  - [x] 选择 session 后自动关闭抽屉
  - 状态：✅ 已完成

- [x] **Task 1.4**: App.tsx 集成移动模式分支
  - [x] 引入 `useMobileDetect()`，`isMobile` 时渲染 MobileLayout
  - [x] 移动模式下不渲染 flexlayout、sidebar、resize handle
  - [x] session 状态管理（RPC 事件、CRUD）在两种模式下共享
  - 状态：✅ 已完成

- [x] **Task 1.5**: 移动端基础 CSS
  - [x] `index.css` 末尾添加移动端样式块
  - [x] MobileTopBar 样式（44px 高、汉堡图标、标题溢出省略）
  - [x] MobileDrawer 样式（80vw 宽、遮罩、translateX 过渡动画）
  - [x] 空状态和不支持面板类型的提示样式
  - 状态：✅ 已完成

- [x] **Task 1.6**: 构建验证
  - [x] 前端 `npm run build` 通过
  - 状态：✅ 已完成

### Phase 2：交互增强与体验修复 [✅ 已完成]

- [x] **Task 2.1**: 抽屉中 SessionListPanel 强制展开
  - [x] `SessionListPanel` 增加 `forceExpanded?: boolean` prop
  - [x] 当 `forceExpanded` 为 true 时，忽略 `collapsed` 状态，始终渲染展开模式
  - [x] `MobileDrawer` 传入 `forceExpanded={true}`
  - 状态：✅ 已完成

- [x] **Task 2.2**: Terminal 面板移动端可用
  - [x] `MobileLayout` 中为 `kind === "terminal"` 的 session 渲染 TerminalPanel（lazy）
  - [x] 添加 `workspaceId` prop 传递链路（App → MobileLayout → TerminalPanel）
  - 状态：✅ 已完成

- [x] **Task 2.3**: 顶栏 session 图标栏
  - [x] 替换顶栏标题区域为 session 图标列表
  - [x] 所有 session 按列表顺序排列，当前 session 原位展开显示「图标+名称」，其他收紧为图标
  - [x] 点击图标切换 session
  - [x] 名称 `text-overflow: ellipsis` 截断，图标列表 `overflow: hidden` 防撑爆
  - 状态：✅ 已完成

- [x] **Task 2.4**: 移动端 CSS 补充
  - [x] `.mobile-session-tabs` / `.mobile-session-tab` / `.mobile-session-tab.active` 样式
  - [x] 活跃 tab 可收缩（`flex-shrink: 1`），非活跃固定宽度
  - 状态：✅ 已完成

- [x] **Task 2.5**: 构建验证
  - [x] 前端 `npm run build` 通过
  - 状态：✅ 已完成

### Phase 2.5：体验修复 [✅ 已完成]

- [x] **Task 2.5.1**: 抽屉内 Compact 按钮改为关闭抽屉
  - [x] `SessionListPanel` 增加 `onToggleOverride?: () => void` prop
  - [x] 存在时，toggle 按钮调用 `onToggleOverride` 而非内部 `toggleMode`
  - [x] `MobileDrawer` 传入 `onClose`
  - 状态：✅ 已完成

- [x] **Task 2.5.2**: 补全 MobileDrawer → SessionListPanel 回调传递
  - [x] 传递 `onHeaderAction`、`onChangeIcon`、`onMenuResult`
  - [x] 补全 MobileLayout → App.tsx 的对应 prop 传递链
  - 状态：✅ 已完成

- [x] **Task 2.5.3**: Agent 消息强制折行
  - [x] `.mobile-panel-container .message-bubble` 添加 `overflow-wrap: break-word`
  - [x] `.mobile-panel-container .content-col` 添加 `min-width: 0; max-width: 100%`
  - [x] 代码块 `pre` 添加 `overflow-x: auto; max-width` 约束
  - [x] `.mobile-panel-container .agent-panel` 添加 `overflow-x: hidden`
  - 状态：✅ 已完成

- [x] **Task 2.5.4**: 移动模式下失焦不清除 activeSessionId
  - [x] App.tsx `window blur` handler 中 `isMobile` 时 return
  - [x] useEffect 依赖数组添加 `isMobile`
  - 状态：✅ 已完成

- [x] **Task 2.5.5**: Session 列表新建按钮
  - [x] SessionListPanel header 增加 `+` 按钮（`sidebar-add-btn`）
  - [x] 点击触发 `onHeaderAction("create_session", {})`
  - [x] App.tsx `handleHeaderAction` 中处理 `create_session` → 创建 AgentSession
  - [x] 桌面端和移动端共用
  - 状态：✅ 已完成

- [x] **Task 2.5.6**: 构建验证
  - [x] 前端 `npm run build` 通过
  - 状态：✅ 已完成

### Phase 2.6：体验修复（续） [✅ 已完成]

- [x] **Task 2.6.1**: Agent 状态栏移动端精简
  - [x] AgentPanel 拆分 "Session " 前缀和 session ID 为独立 span（`.session-id-label` / `.session-id-value`）
  - [x] TokenUsageDisplay 拆分 "Context:" 标签为独立 span（`.token-usage-label`），session total 加 `.token-usage-session` 类
  - [x] 移动端 CSS 隐藏 `.session-id-label`、`.token-usage-label`、`.token-usage-sep`、`.token-usage-session`
  - [x] session ID 空间不足时 `text-overflow: ellipsis` 省略
  - [x] 状态栏 padding/gap/font-size 精简
  - 状态：✅ 已完成

- [x] **Task 2.6.2**: 刷新后自动选中第一个 session
  - [x] MobileLayout 中 useEffect：当 `activeSessionId` 为 null 且 `sessions.length > 0` 时，自动调用 `onSelectSession(sessions[0].id)`
  - 状态：✅ 已完成

- [x] **Task 2.6.3**: 输入框不触发 iOS 自动缩放
  - [x] 移动端 CSS 给 `.mobile-panel-container .chat-input-container textarea` 设 `font-size: 16px`
  - 状态：✅ 已完成

- [x] **Task 2.6.4**: `+` 按钮弹出 RpcMenu
  - [x] SessionListPanel header 的 `+` 按钮改为 RpcMenu 的 trigger，复用 `"SessionList/Header"` category
  - [x] 移除 `handleHeaderAction` 中的 `create_session` 处理
  - 状态：✅ 已完成

- [x] **Task 2.6.5**: 构建验证
  - [x] 前端 `npm run build` 通过
  - 状态：✅ 已完成

### Phase 2.7：收尾修复 [✅ 已完成]

- [x] **Task 2.7.1**: 刷新后恢复 active session
  - [x] MobileLayout 使用 `localStorage("mutbot-mobile-active-session")` 持久化当前 session
  - [x] 刷新时优先从 localStorage 恢复（验证 session 仍存在），fallback 到 `sessions[0]`
  - 状态：✅ 已完成

- [x] **Task 2.7.2**: Welcome 页面复用桌面组件
  - [x] MobileLayout 无 active session 时渲染 `WelcomePage` 组件（替换自定义 `mobile-empty-state`）
  - [x] 移除 `mobile-empty-state` / `mobile-create-btn` 相关 JSX
  - 状态：✅ 已完成

- [x] **Task 2.7.3**: 构建验证
  - [x] 前端 `npm run build` 通过
  - 状态：✅ 已完成

### Phase 2.8：顶栏长按菜单 [✅ 已完成]

- [x] **Task 2.8.1**: 实现长按检测逻辑
  - [x] 在 MobileLayout 的 `.mobile-session-tabs` 容器上统一绑定 touch 事件
  - [x] 通过 `e.target.closest("[data-session-id]")` 识别目标 session
  - [x] 手势判定：短按（< 300ms）切换 tab，长按（>= 300ms 且移动 < 10px）切换 tab + 弹菜单
  - [x] 长按触发时调用 `navigator.vibrate?.(50)` 提供触觉反馈
  - [x] 右键（`onContextMenu`）同样切换 tab + 弹菜单
  - 状态：✅ 已完成

- [x] **Task 2.8.2**: 渲染 RpcMenu 上下文菜单
  - [x] 当菜单状态 `{ sessionId, position }` 有值时，渲染 `RpcMenu`（`category="SessionList/Context"`）
  - [x] 传递 session context（`session_id`、`session_ids`、`session_type`、`session_status`）
  - [x] 菜单关闭时清除状态
  - [x] client action 处理：`start_rename` → `prompt()` + `onRenameSession`，`change_icon` → `onChangeIcon`
  - 状态：✅ 已完成

- [x] **Task 2.8.3**: 长按视觉反馈 CSS
  - [x] `.mobile-session-tab:active` 已有背景加深样式，无需额外添加
  - 状态：✅ 已完成

- [x] **Task 2.8.4**: 构建验证
  - [x] 前端 `npm run build` 通过
  - 状态：✅ 已完成

## 测试验证

（实施阶段填写）
