# New Workspace 创建流程重设计

**状态**：✅ 已完成
**日期**：2026-03-10
**类型**：功能设计

## 背景

当前 New Workspace 流程存在以下 UX 问题：

1. **名称不受重视**：对话框中名称输入只是 optional placeholder，用户跳过或随便填。但 workspace 名称是 hash 路由的唯一标识（`mutbot.ai/#my-project@work`），直接影响用户识别和书签体验。
2. **路径优先的流程与心智不符**：当前流程要求用户先选路径，名称只是附属。实际上用户创建 workspace 时，先想好"这个空间叫什么"更自然。
3. **官网重复实现**：mutbot.ai 的 `launcher.ts` 用 vanilla JS 重新实现了整套 New Workspace 对话框（~160 行），与 React 前端的 DirectoryPicker 功能重复。

## 设计方案

### 核心思路：Hash 不存在 = 新建

统一 hash 语义：当 hash 指向的 workspace 名称在服务器上不存在时，视为新建请求。

```
#my-project@work → "my-project" 存在 → 进入 workspace
#my-project@work → "my-project" 不存在 → New Workspace 页面，name 预填 "my-project"
```

零特殊约定，hash 语义完全统一。

### 入口与 "+" 按钮统一规范

**"+" 按钮的显示规则**（mutbot 前端 + mutbot.ai 统一）：

| 模式 | "+" 按钮行为 |
|------|-------------|
| 桌面 | hover 时显示 "+"（现有行为） |
| 移动 | 始终显示 "+New" |

**空状态文案**（两边统一）：

```
No workspaces yet — create one
                    ^^^^^^^^^^ ← 可点击链接，触发与 "+" 相同逻辑
```

"create one" 链接点击后与 "+" 按钮行为一致：生成不存在的名称，设入 hash。

**"+" 按钮的逻辑**（两边共用）：

生成一个不存在的 workspace 名称，设入 hash，触发 New Workspace 页面。

```typescript
// 共用逻辑：生成不存在的名称
const taken = new Set(workspaces.map(w => w.name));
let name = "new-project";
let i = 1;
while (taken.has(name)) name = `new-project${i++}`;

// mutbot.ai 官网：带 @server 后缀
location.hash = buildHash(name, server.label);  // #new-project@work
// 已有的 hash 变更逻辑自动触发 React 加载（Level 1 动态加载）

// mutbot 前端（localhost）：无 @server 后缀
location.hash = name;  // #new-project
```

**官网侧**：删除 `openDirectoryPicker()` 函数（~160 行），"+" 按钮只需生成名称 + 设 hash。React 前端被动态加载后处理一切。不降级到 Level 3——复用现有 Level 1 动态加载机制。

### New Workspace 页面

独立页面（不是弹窗覆盖），页面中央居中面板。当 React 检测到 hash 指向不存在的 workspace 时渲染此页面。

页面有两个互斥状态，点击 📁 在两者之间切换：

**状态 A：New Workspace 面板**

```
┌──────────────────────────────────────────┐
│                                          │
│  Name:  [new-project              ]      │
│  Path:  [D:\projects\new-project  ] [📁] │
│                                          │
│              [Cancel]  [Create]          │
│                                          │
└──────────────────────────────────────────┘
```

**状态 B：目录浏览面板**（点击 📁 后替换状态 A）

```
┌──────────────────────────────────────────┐
│  [D:\ai                            ] [↑] │  ← 路径输入框 + 向上按钮
│  ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄ │
│  📁 mutbot                               │
│  📁 mutbot.ai                            │
│  📁 mutobj                               │
│                                          │
│                                          │
│              [Cancel]  [Select]          │
└──────────────────────────────────────────┘
```

目录浏览面板与 New Workspace 面板同宽（桌面 480px，移动全宽），高度固定（`min(480px, 70vh)`），目录列表区域可滚动。

**状态 A 交互规则**（JetBrains 联动模式）：

- **Name** 输入框 autofocus，预填从 hash 获取的名称
- **Path** 输入框显示完整路径，末段跟随 Name 实时联动：`{cwd}/{name}`
- 用户手动修改 Path 后，联动断开，Path 保持用户设定
- 点击 📁 → 切换到状态 B（目录浏览面板）
- Cancel：localhost 模式回到 WorkspaceSelector（清除 hash）；remote 模式（mutbot.ai）`location.reload()` 回到 launcher 首页
- Create 创建 workspace 并直接进入（不回 WorkspaceSelector）

**状态 B 交互规则**（目录浏览面板）：

- **路径输入框**：显示当前浏览路径，可直接编辑后回车跳转；路径不存在时回退到最近存在的祖先目录，列表下方提示 "Directory not found"
- **↑ 按钮**：返回上级目录
- 点击目录条目 → 进入该子目录（刷新路径 + 列表）
- **Select** → 以当前浏览路径为选中目录，回到状态 A，Path 更新，联动断开；Name 未手动改过时回填目录名
- **Cancel** → 放弃选择，回到状态 A，Path 不变
- 路径不存在时的回退逻辑：前端调用 `filesystem.browse` 失败后，截掉末段重试，循环直到成功或到根目录

**两个 Path 输入框的职责分工**：
- 状态 A 的 Path：用户想要的目标路径（可以不存在，Create 时 `create_dir: true` 自动创建）
- 状态 B 的路径输入框：当前浏览位置（必须是已存在的目录）

**名称与路径的关系**：

| 输入 | workspace.name（hash 路由标识） | 默认目录路径 |
|------|-------------------------------|-------------|
| Name: "My Project" | `my-project`（slug 化） | `{cwd}/My Project`（原始输入） |
| 用户通过 📁 选择 `/home/user/existing-dir` | `existing-dir`（如 Name 未手动改） | `/home/user/existing-dir` |

### 移动端适配

New Workspace 页面和目录浏览面板通过 CSS 响应式适配移动端，不做独立移动组件。

**面板宽度**：

```css
/* 桌面：固定宽度居中 */
.new-workspace-panel { width: 480px; margin: auto; }

/* 移动：接近全宽 */
@media (max-width: 767px) {
  .new-workspace-panel { width: 100%; padding: 0 16px; }
}
```

**目录浏览面板**：桌面与 New Workspace 面板同宽同位置（状态切换）；移动端全屏。

### 默认路径逻辑

| 场景 | 默认基础路径（CWD） |
|------|-------------------|
| `uvx mutbot` / `python -m mutbot` | 进程启动时的 CWD |
| 未来系统安装 | `~/mutbot-workspaces` 或平台惯例 |

后端通过 `/ws/app` welcome 消息暴露 CWD，前端据此计算默认路径。

### 后端改动

**`/ws/app` welcome 消息新增 `cwd` 字段**：

当前 welcome 消息（`routes.py`）格式为 `{ version, setup_required }`。新增 `cwd`：

```json
{
  "type": "event",
  "event": "welcome",
  "data": { "version": "0.1.0", "setup_required": false, "cwd": "/home/user/projects" }
}
```

`cwd` 取自 mutbot 进程启动时的工作目录（`os.getcwd()` 或启动时记录的值）。

**`workspace.create` 支持自动创建目录**：

当前 `rpc_app.py:47` 硬性校验 `if not p.is_dir(): return {"error": ...}`。新流程中用户输入的路径可能不存在（name 自动生成的路径），需要支持自动创建：

- 新增参数 `create_dir: bool`（默认 `False`）
- `create_dir=True` 且目录不存在 → 自动 `mkdir -p`
- 目录已存在 → 直接使用（无论是否为空）
- `create_dir=False`（默认）→ 保持现有行为，目录不存在则报错

### 前端改动

**新组件 `NewWorkspacePage.tsx`**：
- 独立页面（非弹窗），背景 + 居中面板
- Name 输入框 + Path 输入框 + 📁 按钮 + Cancel/Create
- 📁 按钮展开时复用现有目录浏览逻辑（`filesystem.browse` RPC）
- JetBrains 联动模式控制 Name ↔ Path 关系

**`App.tsx` 路由改动**：

当前行为（`App.tsx:175-178, 231-234`）：hash 指向不存在的 workspace → `exitWorkspace()` → `history.back()` → 回到 WorkspaceSelector。

新行为：hash 指向不存在的 workspace → 渲染 NewWorkspacePage，name 预填自 hash。需要区分"workspace 不存在"和"连接错误"两种情况——连接错误仍走现有的 exitWorkspace 逻辑。

**AppRpc welcome 事件处理**：

当前 `App.tsx:193-199` 的 welcome 回调只提取 `version`。需同时提取 `cwd`，存入 state 传递给 NewWorkspacePage。

**`__MUTBOT_CONTEXT__` 适配（remote 模式）**：

launcher.ts 加载 React 时设置 `__MUTBOT_CONTEXT__ = { remote, wsBase, workspace }`。当 workspace name 不存在时，React 当前视为错误。新逻辑下，React 需对 remote 模式的"workspace 不存在"同样渲染 NewWorkspacePage。

**WorkspaceSelector 改动**：
- "+" 按钮不再弹出 DirectoryPicker，改为生成不存在的名称并导航 hash
- 桌面 hover 显示 "+"，移动端始终显示 "+New"
- 空状态文案改为 "No workspaces yet — create one"，"create one" 为可点击链接
- DirectoryPicker 组件重构为 NewWorkspacePage 的内部子组件（📁 目录浏览面板）

### 官网侧改动

**launcher.ts**：
- 删除 `openDirectoryPicker()` 函数（`launcher.ts:940-1093`，~160 行）
- 删除对应的 CSS 样式（`global.css` 中 `dp-*` 类）
- "+" 按钮（`launcher.ts:732-736`）改为生成名称 + 设置 hash（~5 行）
- 空状态文案（`launcher.ts:611`）改为 "No workspaces yet — create one"，"create one" 为可点击链接
- 移动端 "+" 按钮始终显示为 "+New"
- 已有的 hash 变更 → `loadReactForVersion()` → React 动态加载逻辑不变
- React 接管后通过 `__MUTBOT_CONTEXT__.workspace` 获取预填名称

## 已确认决策

- **目录名用原始输入**：slug 化是 workspace 内部标识，文件系统目录名保留用户原始输入
- **create_dir 行为**：目录已存在直接用，不存在则创建
- **创建后直接进入 workspace**：不回 WorkspaceSelector
- **无过渡动画**：页面切换不做动画
- **Cancel 行为区分模式**：localhost 清除 hash 回 WorkspaceSelector；remote（mutbot.ai）reload 回 launcher
- **目录浏览器独立面板**：与 New Workspace 面板互斥显示，不内联嵌套（减少信息密度）
- **目录浏览器路径回退**：浏览不存在的路径时，前端自动回退到最近存在的祖先目录
- **浏览器仅浏览已有目录**：新路径直接在 New Workspace 的 Path 输入框手打，Create 时自动创建

### 实施概要

涉及两个仓库（mutbot + mutbot.ai），后端一个小改动（welcome + create_dir），前端一个新组件 + 路由变更，官网删代码。后端改动最小可先做，前端新组件是主要工作量，官网改动最后做。

## 实施步骤清单

### 阶段一：后端改动（mutbot 仓库）[✅ 已完成]

- [x] **Task 1.1**: welcome 消息新增 `cwd` 字段
  - [x] `routes.py` welcome 事件 data 中添加 `cwd`（取 `os.getcwd()`）
  - 状态：✅ 已完成

- [x] **Task 1.2**: `workspace.create` 支持 `create_dir` 参数
  - [x] `rpc_app.py` 读取 `create_dir` 参数
  - [x] `create_dir=True` 时跳过 `is_dir()` 校验，改为 `mkdir -p`
  - [x] `create_dir=False` 保持现有行为
  - 状态：✅ 已完成

### 阶段二：前端 New Workspace 页面（mutbot 仓库）[✅ 已完成]

- [x] **Task 2.1**: 新建 `NewWorkspacePage.tsx` 组件
  - [x] 独立页面，居中面板（桌面 480px，移动全宽）
  - [x] Name 输入框（autofocus，预填自 hash）
  - [x] Path 输入框 + 📁 按钮
  - [x] JetBrains 联动模式：Path 末段跟随 Name，手动改 Path 后断开
  - [x] Cancel / Create 按钮
  - 状态：✅ 已完成

- [x] **Task 2.2**: 📁 目录浏览器
  - [x] 复用 `filesystem.browse` RPC 的目录浏览逻辑（DirectoryBrowser 内部组件）
  - [x] 桌面：面板内展开
  - [x] 移动：全屏 overlay
  - [x] 选中目录后更新 Path，联动断开；Name 未手动改时回填目录名
  - 状态：✅ 已完成

- [x] **Task 2.3**: Create 逻辑
  - [x] 调用 `workspace.create`（`project_path` + `name` + `create_dir: true`）
  - [x] 创建成功后直接进入 workspace（更新 hash 为实际 slug 名称）
  - 状态：✅ 已完成

### 阶段三：App.tsx 路由改动（mutbot 仓库）[✅ 已完成]

- [x] **Task 3.1**: "hash 不存在 = 新建" 路由
  - [x] 当前 `exitWorkspace()` 逻辑改为：workspace 不存在时 `setNewWorkspaceName`
  - [x] 连接错误仍走现有降级 UI 逻辑
  - [x] remote 模式（`__MUTBOT_CONTEXT__`）同样支持
  - 状态：✅ 已完成

- [x] **Task 3.2**: AppRpc welcome 事件提取 `cwd`
  - [x] welcome 回调同时提取 `cwd`，存入 state
  - [x] 传递给 NewWorkspacePage 用于默认路径计算
  - 状态：✅ 已完成

### 阶段四：WorkspaceSelector 改动（mutbot 仓库）[✅ 已完成]

- [x] **Task 4.1**: "+" 按钮改为 hash 导航
  - [x] `onNewWorkspace` prop 生成不存在的名称，设入 hash
  - [x] 移除 DirectoryPicker 弹窗调用
  - 状态：✅ 已完成

- [x] **Task 4.2**: "+" 按钮移动端适配
  - [x] 桌面：hover 时显示 "+"（CSS `.ws-selector-heading-row:hover`）
  - [x] 移动：始终显示 "+New"（`.ws-selector-new-btn-mobile`）
  - 状态：✅ 已完成

- [x] **Task 4.3**: 空状态文案
  - [x] 改为 "No workspaces yet — create one"
  - [x] "create one" 为可点击链接（`.ws-selector-create-link`）
  - 状态：✅ 已完成

- [x] **Task 4.4**: 清理 DirectoryPicker
  - [x] WorkspaceSelector 不再 import DirectoryPicker
  - [x] DirectoryPicker.tsx 保留（暂未删除，后续可清理）
  - 状态：✅ 已完成

### 阶段五：官网改动（mutbot.ai 仓库）[✅ 已完成]

- [x] **Task 5.1**: launcher.ts "+" 按钮改为 hash 导航
  - [x] 删除 `openDirectoryPicker()` 函数（~160 行）
  - [x] "+" 按钮改为生成名称 + 设置 hash（~5 行）
  - [x] 移动端始终显示 "+New"
  - 状态：✅ 已完成

- [x] **Task 5.2**: 空状态文案 + CSS 清理
  - [x] 空状态改为 "No workspaces yet — create one"（"create one" 可点击）
  - [x] 删除 `global.css` 中 `dp-*` 相关样式
  - 状态：✅ 已完成

### 阶段六：构建验证 [✅ 已完成]

- [x] **Task 6.1**: mutbot 前端构建
  - [x] `npm --prefix mutbot/frontend run build` 通过
  - 状态：✅ 已完成

- [x] **Task 6.2**: mutbot.ai 构建
  - [x] `npm --prefix mutbot.ai run build` 通过
  - 状态：✅ 已完成

### 阶段七：UX 修复（mutbot 仓库）[✅ 已完成]

- [x] **Task 7.1**: Cancel 行为修复（remote 模式）
  - [x] NewWorkspacePage 的 `onCancel` 在 remote 模式下改为 `location.reload()`
  - [x] localhost 模式保持现有行为（清除 hash 回 WorkspaceSelector）
  - 状态：✅ 已完成

- [x] **Task 7.2**: 目录浏览器重构为独立面板
  - [x] 目录浏览面板与 New Workspace 面板互斥显示（状态切换，非内联嵌套）
  - [x] 面板顶部：路径输入框（可编辑 + 回车跳转）+ ↑ 向上按钮
  - [x] 目录列表：点击进入子目录
  - [x] Select / Cancel 按钮
  - [x] 面板高度固定（`min(480px, 70vh)`），列表区域可滚动
  - 状态：✅ 已完成

- [x] **Task 7.3**: 目录浏览器路径回退
  - [x] `filesystem.browse` 返回错误时，截掉末段重试，直到找到存在的祖先目录
  - [x] 列表下方提示 "Directory not found"
  - 状态：✅ 已完成

- [x] **Task 7.4**: 移动端输入框缩放修复
  - [x] 输入框 `font-size` 设为 16px（iOS Safari 在 font-size < 16px 时自动缩放页面）
  - 状态：✅ 已完成

### 阶段八：构建验证 [✅ 已完成]

- [x] **Task 8.1**: mutbot 前端构建
  - [x] `npm --prefix mutbot/frontend run build` 通过
  - 状态：✅ 已完成

## 测试验证

（实施阶段填写）

## 关键参考

### 源码
- `mutbot/frontend/src/components/DirectoryPicker.tsx` — 当前目录选择弹窗（待重构为子组件）
- `mutbot/frontend/src/components/WorkspaceSelector.tsx:181` — "+" 按钮当前弹出 DirectoryPicker
- `mutbot/frontend/src/App.tsx:175-178,231-234` — hash 不存在时 `exitWorkspace()` 逻辑（需改为渲染 NewWorkspacePage）
- `mutbot/frontend/src/App.tsx:193-199` — welcome 回调，当前只取 `version`（需同时取 `cwd`）
- `mutbot/frontend/src/lib/app-rpc.ts` — AppRpc 客户端，welcome 事件处理
- `mutbot/frontend/src/main.tsx` — `ensureLandingInHistory`，hash/history 管理（无需改动）
- `mutbot/frontend/src/lib/connection.ts` — `isRemote()`、`getMutbotHost()`（无需改动）
- `mutbot/src/mutbot/web/routes.py:109-116` — `/ws/app` welcome 消息发送点（需加 `cwd`）
- `mutbot/src/mutbot/web/rpc_app.py:33-74` — `workspace.create` handler（需加 `create_dir`）
- `mutbot/src/mutbot/web/rpc_app.py:47` — `if not p.is_dir()` 硬校验（需条件化）
- `mutbot/src/mutbot/runtime/workspace.py:54` — `sanitize_workspace_name()` slug 化逻辑
- `mutbot.ai/src/scripts/launcher.ts:114-130` — `parseHash()` / `buildHash()`：hash 格式 `#workspace@server`
- `mutbot.ai/src/scripts/launcher.ts:290-318` — `loadReactForVersion()` + `__MUTBOT_CONTEXT__` 设置
- `mutbot.ai/src/scripts/launcher.ts:732-736` — "+" 按钮当前调 `openDirectoryPicker()`（需改为设 hash）
- `mutbot.ai/src/scripts/launcher.ts:940-1093` — 官网 New Workspace 对话框（待删除）

### 相关规范
- `mutbot.ai/docs/specifications/feature-builtin-frontend.md` — 内置前端 + Level 1/3 降级策略、`__MUTBOT_CONTEXT__` 定义
- `mutbot.ai/docs/specifications/feature-website-github-pages.md` — 三级降级架构总体规划
