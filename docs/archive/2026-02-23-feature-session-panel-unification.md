# Session 面板统一与管理改进 设计规范

**状态**：✅ 已完成
**日期**：2026-02-23
**类型**：功能设计

## 1. 背景

### 1.1 现状

当前 Web UI 中存在几类不同的"面板内容"，它们的管理方式各自独立：

| 面板类型 | 当前管理方式 | 创建入口 |
|----------|-------------|---------|
| Agent 对话 | SessionListPanel 侧栏 + `createSession` API | 侧栏 "+ New Session" 按钮 |
| 终端 | Toolbar 按钮 → `addNode` | 顶部工具栏 "Terminal" 按钮 |
| 代码编辑 | Agent 工具调用触发 | 无直接用户入口 |
| 日志 | Toolbar 按钮 → `addNode` | 顶部工具栏 "Logs" 按钮 |

问题：
- Session 概念仅覆盖 Agent 对话，终端和文档编辑各自独立，无统一视图
- 新建入口分散在侧栏按钮和工具栏中，操作模式不一致
- 没有统一的活动/已结束状态管理，终端没有结束状态概念
- 面板关闭行为不统一，没有区分"关闭面板"和"结束会话"

### 1.2 目标

将 Agent 对话、终端、文档编辑统一为"Session"概念，提供一致的生命周期管理和操作体验。所有面板最终都应纳入 Session 体系。

## 2. 设计方案

### 2.1 统一 Session 模型

将所有面板内容统一抽象为 **Session**：

```
Session
├── Agent Session   — Agent 对话（现有）
├── Terminal Session — 终端会话（从独立面板提升为 Session）
└── Document Session — 文档编辑（从独立面板提升为 Session）
```

**Session 通用字段**：

```typescript
interface Session {
  id: string;
  workspace_id: string;
  type: "agent" | "terminal" | "document";  // Session 类型
  title: string;
  status: "active" | "ended";
  created_at: string;
  updated_at: string;
  config?: Record<string, unknown>;  // 类型特定的扩展配置
}
```

**各类型扩展配置**（存储在 Session 的 `config` 字段中）：

```typescript
// Agent Session — 无额外配置（Agent 实例由后端管理）
interface AgentSessionConfig {}

// Terminal Session — 关联 PTY + 启动进程命令
interface TerminalSessionConfig {
  terminal_id?: string;      // 关联的 PTY ID
  shell_command?: string;    // 启动进程命令（默认 "cmd.exe"，以后可配置）
}

// Document Session — 关联文件（支持虚拟/临时文件）
interface DocumentSessionConfig {
  file_path: string;     // 文件路径（可以是虚拟路径，不要求物理存在）
  language?: string;     // 语言标识
}
```

**设计原则**：
- 一个 Document Session 对应一个文件。文件不要求物理存在——可以是临时文件或虚拟文件。多文件场景通过创建多个 Document Session 实现。
- Terminal Session 的 `shell_command` 是启动 PTY 时的进程命令，当前固定为 `cmd.exe`，后续可配置。结束后重新打开时，使用相同的 `shell_command` 启动新 PTY。

### 2.2 Session 列表

Session 列表是**固定的 UI 功能组件**，不是 Session。它固定在工作区最左侧（flexlayout 的 left border），不可关闭。

**现状**：SessionListPanel 仅列出 Agent Session，左侧栏顶部有 "+ New Session" 按钮。

**改造后**：

1. **列出所有类型的 Session**：Agent、Terminal、Document 全部在同一列表中展示
2. **去掉顶部 "+ New Session" 按钮**：不再在 Session 列表中创建
3. **状态分组排列**：
   - **活动 Session**（`status === "active"`）：排在列表前部，正常显示
   - **已结束 Session**（`status === "ended"`）：排在列表后部，灰色显示
4. **列表项显示**：
   - 类型图标（区分 Agent / Terminal / Document）
   - Session 标题
   - 状态指示（活动 / 已结束）

### 2.3 新建 Session 入口

**去掉**：Session 列表中的 "+ New Session" 按钮、顶部工具栏中的 Terminal / Logs 按钮。

**替代方案**：每个 **tabset（停靠区域）** 的右上角提供一个 **"+" 按钮**，点击后弹出下拉菜单：

```
  ┌─────────────┐
  │ + Agent     │
  │ + Document  │
  │ + Terminal  │
  └─────────────┘
```

菜单仅包含三种 Session 类型。Logs 功能本阶段隐藏（后续作为 Session 类型纳入体系）。

flexlayout-react 原生支持 tabset 按钮：通过 `onRenderTabSet` 回调在 tabset 头部注入自定义按钮。

**实现方式**：

```typescript
// Layout 组件的 onRenderTabSet 回调
function onRenderTabSet(
  tabSetNode: TabSetNode | BorderNode,
  renderValues: {
    headerContent?: ReactNode;
    stickyButtons: ReactNode[];
    buttons: ReactNode[];
    headerButtons: ReactNode[];
  }
) {
  renderValues.stickyButtons.push(
    <AddSessionButton
      key="add-session"
      onAdd={(type) => handleAddSession(type, tabSetNode)}
    />
  );
}
```

**各类型创建流程**：
- **"+" → Agent**：调用创建 Session API → 打开 AgentPanel tab
- **"+" → Terminal**：调用创建 Session API（使用默认 `shell_command`）→ 打开 TerminalPanel tab
- **"+" → Document**：直接创建新空白文档 Session → 打开 CodeEditorPanel tab（后续增加文件浏览入口）

### 2.4 Session 生命周期与关闭行为

#### 2.4.1 通用规则

- **关闭面板** ≠ **结束 Session**：关闭 tab 只是从布局中移除面板，Session 仍然存在
- **结束 Session**：显式操作，将 Session 状态标记为 `ended`
- **已结束的 Session 可以重新打开**：从 Session 列表点击即可重新打开面板

#### 2.4.2 Terminal Session 关闭行为

用户关闭终端 tab 时，弹出确认对话框：

```
┌─────────────────────────────────┐
│  结束终端会话？                    │
│                                 │
│  [结束会话]    [仅关闭面板]        │
└─────────────────────────────────┘
```

- **结束会话**：销毁 PTY → Session 状态变为 `ended`
- **仅关闭面板**：关闭 tab，PTY 保持运行，Session 仍为 `active`

**Terminal Session 结束后重新打开**：
- PTY 立即销毁，不可恢复
- 用户从列表点击已结束的 Terminal Session 时，使用该 Session 记录的 `shell_command` 创建新的 PTY，直接启动新终端（无需确认对话框）

#### 2.4.3 Agent Session 关闭行为

关闭 Agent tab 时，默认**结束会话**，无需确认对话框（Agent Session 恢复成本低）：
- 调用 stop API → Session 状态变为 `ended`
- Agent 线程停止

已结束的 Agent Session 可以**恢复**：
- 从 Session 列表点击已结束的 Agent Session
- 打开面板后，用户发送新消息即重新启动 Agent（现有机制已支持）

#### 2.4.4 Document Session 关闭行为

关闭文档 tab 时，直接关闭面板，Session 保持 `active` 状态（文档无运行时资源）。

### 2.5 工具栏简化

改造后，**移除整个顶部工具栏**（Terminal / Logs 按钮）。所有创建入口统一到 tabset "+" 按钮。Logs 功能本阶段暂时隐藏，后续将作为 Session 类型纳入统一体系。

### 2.6 Session 类型扩展（预留）

所有面板最终都将纳入 Session 体系。当前仅做**接口预留**，不做具体扩展机制的设计：

```typescript
// 类型注册表概念（预留，当前硬编码三种类型）
type SessionType = "agent" | "terminal" | "document";

// 未来：Logs 等面板也将作为 Session 类型纳入
// 未来：用户可通过配置定义新的 Session 类型
// 当前：仅支持上述三种内置类型
```

后端 Session 模型增加 `type` 和 `config` 字段即可支持扩展。当前阶段不实现动态注册机制。

## 3. 待定问题

（无）

## 4. 实施步骤清单

### 阶段一：后端 Session 模型扩展 [✅ 已完成]

- [x] **Task 1.1**: 扩展 Session 数据模型
  - [x] Session dataclass 增加 `type` 字段（`"agent" | "terminal" | "document"`）
  - [x] Session dataclass 增加 `config` 字段（`dict | None`）
  - [x] 兼容现有 Session 数据（无 `type` 字段的默认为 `"agent"`）
  - 状态：✅ 已完成

- [x] **Task 1.2**: Terminal 纳入 Session 管理
  - [x] `SessionManager` 支持创建 Terminal 类型 Session
  - [x] Terminal Session 创建时自动分配 PTY（使用 `config.shell_command`，默认 `cmd.exe`）
  - [x] Terminal Session 结束时销毁关联 PTY
  - [x] API 端点适配：创建 Session 时支持 `type` 和 `config` 参数
  - 状态：✅ 已完成

- [x] **Task 1.3**: Document 纳入 Session 管理
  - [x] `SessionManager` 支持创建 Document 类型 Session
  - [x] Document Session 存储文件路径配置（支持虚拟/临时文件路径）
  - [x] 新建文档时生成默认虚拟路径和空内容
  - [x] API 端点适配
  - 状态：✅ 已完成

### 阶段二：前端 Session 列表改造 [✅ 已完成]

- [x] **Task 2.1**: 统一 Session 列表显示
  - [x] 修改 `SessionListPanel` 显示所有类型 Session
  - [x] 添加类型图标区分 Agent / Terminal / Document
  - [x] 实现状态分组排列（活动在前，已结束灰色在后）
  - [x] 去掉 "+ New Session" 按钮
  - [x] 确保 Session 列表固定在工作区最左侧（left border，不可关闭）
  - 状态：✅ 已完成

- [x] **Task 2.2**: Session 列表点击行为
  - [x] 点击 Agent Session → 打开 AgentPanel tab
  - [x] 点击活动 Terminal Session → 打开 TerminalPanel tab（传递 terminal_id）
  - [x] 点击已结束 Terminal Session → 用相同 shell_command 创建新 PTY 并打开
  - [x] 点击 Document Session → 打开 CodeEditorPanel tab（传递 file_path）
  - [x] 已打开的 Session 切换到对应 tab 而非重复创建
  - 状态：✅ 已完成

### 阶段三：Tabset "+" 按钮与创建流程 [✅ 已完成]

- [x] **Task 3.1**: 实现 tabset "+" 按钮
  - [x] 使用 `onRenderTabSet` 注入 "+" 按钮
  - [x] 实现下拉菜单（Agent / Document / Terminal）
  - [x] 移除顶部工具栏（Terminal / Logs 按钮）
  - 状态：✅ 已完成

- [x] **Task 3.2**: 创建流程对接
  - [x] "+" → Agent：调用创建 Session API → 打开 AgentPanel tab
  - [x] "+" → Terminal：调用创建 Session API（默认 shell_command）→ 打开 TerminalPanel tab
  - [x] "+" → Document：创建新空白文档 Session → 打开 CodeEditorPanel tab
  - 状态：✅ 已完成

### 阶段四：关闭行为与生命周期 [✅ 已完成]

- [x] **Task 4.1**: Terminal 关闭确认
  - [x] 使用 flexlayout 的 `onAction` 拦截 `DELETE_TAB` 动作
  - [x] 弹出确认对话框：结束会话 / 仅关闭面板
  - [x] "结束会话"：调用 API 结束 Session + 销毁 PTY
  - [x] "仅关闭面板"：仅移除 tab，Session 保持 active
  - 状态：✅ 已完成

- [x] **Task 4.2**: Agent 关闭行为
  - [x] 关闭 Agent tab 时自动调用 stop API 结束会话（无确认）
  - [x] 已结束的 Agent Session 从列表重新打开后，发送消息自动恢复
  - 状态：✅ 已完成

- [x] **Task 4.3**: Document 关闭行为
  - [x] 关闭 Document tab 时仅移除面板，Session 保持 active
  - 状态：✅ 已完成

---

### 实施进度总结
- ✅ **阶段一：后端 Session 模型扩展** — 100% 完成 (3/3 任务)
- ✅ **阶段二：前端 Session 列表改造** — 100% 完成 (2/2 任务)
- ✅ **阶段三：Tabset "+" 按钮与创建流程** — 100% 完成 (2/2 任务)
- ✅ **阶段四：关闭行为与生命周期** — 100% 完成 (3/3 任务)

**核心功能完成度：100%** (10/10 任务)
**构建验证：Python 后端模块导入通过，TypeScript 类型检查通过，Vite 生产构建通过**

## 5. 测试验证

### 单元测试
- [ ] Session 模型 `type` / `config` 字段序列化与反序列化
- [ ] SessionManager 创建不同类型 Session
- [ ] Terminal Session 结束时 PTY 清理
- [ ] 旧数据兼容（无 `type` 字段默认为 `"agent"`）
- [ ] Document Session 支持虚拟文件路径

### 集成测试
- [ ] 创建各类型 Session → 列表正确显示
- [ ] Session 列表状态分组排列（活动在前、已结束灰色在后）
- [ ] tabset "+" 按钮创建各类型 Session
- [ ] Terminal 关闭确认对话框流程
- [ ] Agent Session 关闭 → 结束 → 重新打开 → 恢复
- [ ] 已结束 Terminal Session 重新打开 → 使用相同 shell_command 启动新终端
- [ ] 布局持久化包含新 Session 类型信息
