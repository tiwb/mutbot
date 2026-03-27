# Workspace 简化与终端 Settings UI 设计规范

**状态**：✅ 已完成
**日期**：2026-03-19
**类型**：重构 + 功能设计

## 背景

当前 Workspace 绑定了 `project_path`，创建时必须选择目录。实际使用中：
- 工作区不需要"主路径"概念，每个终端/session 可以有不同路径
- 创建工作区流程繁琐（必须浏览选择目录）
- Workspace 文件名为纯 ID（`abc123def456.json`），不便于查找

需求：
1. 去掉 `project_path`，创建工作区只需名称
2. Workspace 文件名改为 `日期-name-hash` 格式
3. 终端默认 cwd 用 mutbot 启动时的 cwd（daemon 模式用 home）
4. 终端 tab 右键可修改 Settings（cwd，未来扩展 shell command 等），使用 RpcMenu 触发 + UIContext Modal 渲染
5. file.read / CodeEditorPanel 最小适配（改为绝对路径，保留占位）

## 设计方案

### Workspace 数据模型变更

移除 `project_path` 字段：

```python
@dataclass
class Workspace:
    id: str                        # 12位 UUID hex
    name: str                      # slug 格式
    sessions: list[str] = ...
    layout: dict | None = None
    created_at: str = ""
    updated_at: str = ""
    last_accessed_at: str = ""
    # project_path: 移除
```

`WorkspaceManager.create(name)` 不再需要 `project_path` 参数。

### Workspace 文件名格式

从 `{id}.json` 改为 `{date}-{name}-{id}.json`：

```
20260319-my-project-abc123def456.json
```

- `date`：创建日期 `YYYYMMDD`（从 `created_at` 提取，转本地时间）
- `name`：workspace slug name
- `id`：12位 UUID hex

**查找和加载**（参照 session 文件的 `_find_session_file` 模式）：

- 新增 `_find_workspace_file(workspace_id)` 函数，glob `*{workspace_id}.json` 兼容新旧格式
- `load_workspace(workspace_id)` 改用 `_find_workspace_file` 查找（当前硬编码 `{id}.json`）
- `save_workspace()` 使用新格式写入，新增 `_workspace_file_prefix(ws_data)` 构建 `{date}-{name}-` 前缀
- `save_workspace()` 写入新格式后，检测并删除旧格式文件（避免同一 workspace 两个文件共存）
- `load_all_workspaces()` 无需改动（已经 glob `*.json`）

### 终端默认 cwd

优先级：
1. 创建终端时显式指定的 cwd（来自 session config）
2. **mutbot 启动时的 cwd**（`os.getcwd()`，启动时记录一次）
3. daemon 模式下 fallback 到用户 home 目录

记录方式：`storage.py` 中新增 `STARTUP_CWD` 模块级变量，`server.py` 启动时设置（与 `MUTBOT_DIR` 风格一致）。

### 终端 Settings — Session 固有 UI，Menu 仅触发

#### 架构设计

**核心洞察**：Menu 是触发器，不是 UI 的 owner。Settings UI 属于 Session 本身。

正交分解：

| 轴 | 维度 | 当前实现 |
|----|------|----------|
| UI 归属 | Session 级 / Workspace 级 | 本次只做 Session 级 |
| UI 触发方式 | Menu / 快捷键 / 程序触发 | 本次通过 Menu 触发 |
| UI 传输通道 | Session channel / Workspace event | 复用 session channel（与 AgentPanel UIContext 一致） |

关键决策：
- **Settings UI 归属 Session**：任何 Session 类型都可以有 Settings UI，TerminalSession 是第一个
- **Menu 只发信号**：`Menu.execute()` 返回 `MenuResult(action="open_settings", data={session_id})`，不创建 UIContext、不管理生命周期
- **走 session channel**：与 AgentPanel 的 UIContext 完全一致，无新基础设施
- **未来 Workspace 级 UI**：到时走 workspace 通道，独立设计，不影响当前架构

#### 交互流程

```
终端 Tab 右键 → "Settings" 菜单项
  ↓
Menu.execute() → 返回 MenuResult(action="open_settings", data={session_id})
  ↓
前端收到 → 通过 session channel 调用 session.open_settings RPC
  ↓
后端 Session 创建 UIContext，通过 session channel 推送 Settings view
  ↓
前端 Session Panel 渲染 Settings（Modal 或内嵌面板，复用 ViewRenderer）
  ↓
用户修改 + 提交 → UIEvent(submit) → 通过 session channel 回传
  ↓
后端更新 session config，restart terminal
  ↓
UIContext 显示 spinner → 完成后 close()
```

#### 后端：session.open_settings RPC

新增 Session 级 RPC `session.open_settings`，由 Session 自身管理 UIContext：

- Session 创建 UIContext（broadcast 用 session 的 broadcast_json）
- 后台 asyncio Task 持有 UIContext，构建 view → 等待 submit → 应用配置
- 通过 session channel 推送 `ui_view` / `ui_close`（与 Agent tool UI 一致）

#### 前端：Session Panel 处理 Settings

- AgentPanel 已经处理 `ui_view` / `ui_close` 事件（ToolCallCard）
- TerminalPanel 需要新增同样的处理：收到 `ui_view` 时渲染 Settings Modal
- 复用 `ViewRenderer` 组件，提交/取消通过 channel 发送 `ui_event`

#### Terminal Settings View Schema

```python
view = {
    "title": "Terminal Settings",
    "components": [
        {
            "type": "text",
            "id": "cwd",
            "label": "Working Directory",
            "placeholder": "/path/to/directory",
            # value: 当前 session.config["cwd"]
        },
        # 未来扩展：
        # {"type": "text", "id": "shell", "label": "Shell Command"},
    ],
    "actions": [
        {"type": "submit", "label": "Apply & Restart", "primary": true},
        {"type": "cancel", "label": "Cancel"},
    ],
}
```

### file.read RPC 处理

`FileOps.read()` 当前依赖 `ws.project_path` 做路径遍历检查。使用方：`CodeEditorPanel.tsx`。

`CodeEditorPanel` 未来会重新设计。当前处理：
- 移除 `FileOps` 中对 `ws.project_path` 的依赖
- `file.read` 改为接受绝对路径，保留 RPC 接口作为占位
- `CodeEditorPanel` 保留组件文件，内部暂时 noop 或显示占位提示

### 创建工作区 UI 变更

去掉目录浏览选择步骤，创建工作区只需输入名称（或自动生成）。

前端 `WorkspaceSelector` 的创建流程简化：
- 当前：选择目录 → 输入名称（可选）→ 创建
- 改为：输入名称 → 创建

## 设计决策

- **startup_cwd 存储**：`storage.STARTUP_CWD` 模块级变量，`server.py` 启动时设置，与 `MUTBOT_DIR` 风格一致
- **向后兼容**：参照 session 文件命名迁移模式（`_find_session_file` glob `*{id}{suffix}`）：新增 `_find_workspace_file()` 用 glob 兼容新旧格式；加载时忽略 `project_path` 字段；save 时以新格式写入并删除旧格式文件（避免两个文件共存导致 glob 歧义）
- **Settings UI 归属 Session，Menu 仅触发**：Menu.execute() 返回 `action="open_settings"`，不创建 UIContext。Session 自身管理 UIContext 生命周期，通过 session channel 通信（与 AgentPanel UIContext 一致，无新基础设施）
- **未来扩展**：任何 Session 类型都可以有 Settings UI；Workspace 级 UI 独立设计，走 workspace 通道

## 实施步骤清单

### Phase 1-2: Workspace 模型与存储 + 清理 project_path [✅ 已完成]

已提交：移除 Workspace.project_path、新文件名格式、STARTUP_CWD、清理所有引用、更新测试。

### Phase 3-4: Terminal Settings UI [✅ 已完成]

- [x] **Task 3.1**: `TerminalSettingsMenu` 菜单项
  - [x] `builtins/menus.py` — 新增 Menu 子类，category=`Tab/Context`，`client_action="open_settings"`，`check_visible` 限制 terminal session
  - 状态：✅ 已完成

- [x] **Task 3.2**: 后端 `open_settings` 消息处理
  - [x] `runtime/terminal.py` — `on_message` 新增 `open_settings` 和 `ui_event` 消息类型
  - [x] `_handle_open_settings()` — 创建 UIContext，推送 Settings 表单（cwd），等待 submit → 保存 config
  - [x] 不自动 restart，用户手动 restart 时使用新 cwd
  - 状态：✅ 已完成

- [x] **Task 3.3**: 修复 `menu_impl.py` — `check_visible` 收不到前端 context
  - [x] 移除错误的 `hasattr(context, "managers")` 守卫
  - 状态：✅ 已完成

- [x] **Task 4.1**: `App.tsx` 处理 `open_settings` client_action
  - [x] `handleTabClientAction` 新增 `open_settings`，dispatch CustomEvent 通知 TerminalPanel
  - 状态：✅ 已完成

- [x] **Task 4.2**: `TerminalPanel` 处理 UIContext 事件
  - [x] 监听 `open-session-settings` CustomEvent → 通过 channel 发 `open_settings`
  - [x] `handleJsonMessage` 新增 `ui_view` / `ui_close` → 渲染 Settings Modal（复用 ViewRenderer）
  - [x] submit/cancel 通过 channel 发 `ui_event`
  - 状态：✅ 已完成

- [x] **Task 4.3**: `ToolCallCard.tsx` — export `ViewRenderer` 供 TerminalPanel 复用
  - 状态：✅ 已完成

### Phase 5: 测试与验证 [✅ 已完成]

- [x] **Task 5.1**: 构建前端并验证
  - [x] `npm --prefix mutbot/frontend run build` — 构建成功
  - [x] 后端测试 481 passed
  - [ ] 手动验证：创建工作区、终端 Settings、文件名格式
  - 状态：⏸️ 待开始

## 关键参考

### 源码
- `src/mutbot/runtime/workspace.py` — Workspace 数据模型、WorkspaceManager
- `src/mutbot/runtime/storage.py` — 文件持久化、MUTBOT_DIR、STARTUP_CWD
- `src/mutbot/menu.py` — Menu Declaration、MenuItem、MenuResult
- `src/mutbot/ui/context.py` — UIContext Declaration（set_view / wait_event / show / close）
- `src/mutbot/ui/context_impl.py` — UIContext @impl（broadcast 推送、Future 管理、全局 registry）
- `src/mutbot/builtins/menus.py` — 内置菜单实现
- `src/mutbot/web/rpc_app.py` — AppMenuOps（menu.query / menu.execute）
- `src/mutbot/web/rpc_workspace.py` — TerminalOps.create
- `src/mutbot/web/rpc_session.py` — SessionOps.create
- `frontend/src/components/ToolCallCard.tsx` — ViewRenderer、UIComponent（现有 UIContext 渲染器）
- `frontend/src/panels/AgentPanel.tsx` — ui_view / ui_close 事件处理（参考实现）
- `frontend/src/panels/TerminalPanel.tsx` — 终端面板（需新增 Settings 处理）
- `frontend/src/components/RpcMenu.tsx` — RpcMenu 组件
- `frontend/src/lib/workspace-rpc.ts` — WorkspaceRpc（channel 多路复用、event 系统）
