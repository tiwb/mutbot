# Workspace 简化与终端 Settings UI 设计规范

**状态**：🔄 实施中
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

### 终端 Settings — RpcMenu + UIContext Modal

Settings 是 **Session 级别的配置**。每个 TerminalSession 有自己的 config（cwd、未来的 shell command 等），Settings UI 读写的是该 session 的 config。

#### 交互流程

```
终端 Tab 右键 → "Settings" 菜单项（context 含 session_id）
  ↓
Menu.execute() 读取该 session 的当前 config
  ↓
创建 UIContext，推送 Settings view（填充当前值）
  ↓
返回 MenuResult(action="ui_modal", data={context_id})
  ↓
前端打开 Modal，通过 context_id 连接 UIContext
  ↓
ViewRenderer 渲染表单（cwd、未来: shell command 等）
  ↓
用户修改 + 提交 → UIEvent(submit) → 后端 wait_event() 收到
  ↓
后端更新该 session 的 config，restart terminal
  ↓
UIContext 显示 "Restarting..." spinner → 完成后 close()
  ↓
前端关闭 Modal
```

#### UIContext Modal 渲染面

当前 UIContext 只在 ToolCallCard 中渲染（绑定 tool_call_id）。需要新增 **Modal 渲染面**：

- 前端新增 `UIModal` 组件，监听特定 context_id 的 UIContext 事件
- 复用现有 `ViewRenderer`，在 Modal 中渲染
- UIContext 的 `broadcast` 走 **workspace 级通道**（Tab 右键菜单在 App 层触发，Modal 也在 App 层渲染，ctx.broadcast 即 workspace 级广播；前端按 context_id 过滤事件）

#### UIContext 生命周期管理

Menu.execute() 是 RPC 调用，需要在 execute 返回后保持 UIContext 存活：

- Menu.execute() 创建 UIContext，注册到全局 registry
- 返回 `MenuResult(action="ui_modal", data={"context_id": ctx_id})`
- 后台 asyncio Task 持有 UIContext，等待用户交互
- 用户提交/取消 → Task 完成 → UIContext.close() → 从 registry 移除

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
        # {"type": "text", "id": "shell", "label": "Shell Command", "placeholder": "/bin/bash"},
        # {"type": "text", "id": "env", "label": "Environment", "multiline": true},
    ],
    "actions": [
        {"type": "submit", "label": "Apply & Restart", "primary": true},
        {"type": "cancel", "label": "Cancel"},
    ],
}
```

Apply 后需要 restart 终端（进程 cwd 不可运行时修改）。按钮文案 "Apply & Restart" 已足够明确，不再二次确认。后端可用 UIContext 多步交互：提交后显示 "Restarting..." spinner，完成后自动关闭。

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
- **UIContext broadcast 通道**：走 workspace 级通道。Tab 右键菜单在 App 层触发，RpcContext.broadcast 即 workspace 级广播，Modal 在 App 层渲染，前端按 context_id 过滤事件
- **向后兼容**：参照 session 文件命名迁移模式（`_find_session_file` glob `*{id}{suffix}`）：新增 `_find_workspace_file()` 用 glob 兼容新旧格式；加载时忽略 `project_path` 字段；save 时以新格式写入并删除旧格式文件（避免两个文件共存导致 glob 歧义）
- **UIContext Modal 通用性**：做成通用基础设施，任何 Menu.execute() 均可返回 `action="ui_modal"`，Terminal Settings 是第一个使用场景

## 实施步骤清单

### Phase 1: 后端 — Workspace 模型与存储 [待开始]

- [ ] **Task 1.1**: 移除 `Workspace.project_path`
  - [ ] `workspace.py` — 移除 dataclass 字段、`_workspace_to_dict`、`_workspace_from_dict`
  - [ ] `WorkspaceManager.create()` — 移除 `project_path` 参数
  - 状态：⏸️ 待开始

- [ ] **Task 1.2**: Workspace 文件名新格式
  - [ ] `storage.py` — 新增 `_find_workspace_file()`、`_workspace_file_prefix()`
  - [ ] `storage.py` — 更新 `save_workspace()` 使用新格式，删除旧格式文件
  - [ ] `storage.py` — 更新 `load_workspace()` 使用 `_find_workspace_file()`
  - 状态：⏸️ 待开始

- [ ] **Task 1.3**: `STARTUP_CWD` 全局变量
  - [ ] `storage.py` — 新增 `STARTUP_CWD` 变量
  - [ ] `server.py` — 启动时设置 `storage.STARTUP_CWD`（daemon 模式 fallback home）
  - 状态：⏸️ 待开始

### Phase 2: 后端 — 清理 `project_path` 引用 [待开始]

- [ ] **Task 2.1**: 替换所有 `ws.project_path` 为 `STARTUP_CWD`
  - [ ] `rpc_workspace.py` — `TerminalOps.create` cwd 参数
  - [ ] `rpc_session.py` — `SessionOps.create` config.setdefault
  - [ ] `builtins/menus.py` — `AddSessionMenu.execute` config cwd
  - [ ] `serializers.py` — 移除 `project_path` 序列化
  - [ ] `web/mcp.py` — 移除 `project_path` 展示
  - 状态：⏸️ 待开始

- [ ] **Task 2.2**: `workspace.create` RPC 移除 `project_path` 参数
  - [ ] `rpc_app.py` — `WorkspaceOps.create` 不再要求 project_path
  - 状态：⏸️ 待开始

- [ ] **Task 2.3**: `file.read` 最小适配
  - [ ] `rpc_workspace.py` — `FileOps.read` 改为接受绝对路径，移除 project_path 遍历检查
  - 状态：⏸️ 待开始

### Phase 3: 后端 — Terminal Settings Menu + UIContext Modal [待开始]

- [ ] **Task 3.1**: `TerminalSettingsMenu` 菜单项
  - [ ] `builtins/menus.py` — 新增 Menu 子类，category=`Tab/Context`，`check_visible` 限制 terminal session
  - 状态：⏸️ 待开始

- [ ] **Task 3.2**: Menu → UIContext 桥接
  - [ ] 在 Menu.execute() 中创建 UIContext（使用 ctx.broadcast），注册到全局 registry
  - [ ] 启动后台 asyncio Task 持有 UIContext，等待用户交互
  - [ ] 返回 `MenuResult(action="ui_modal", data={"context_id": ...})`
  - 状态：⏸️ 待开始

- [ ] **Task 3.3**: Terminal Settings 业务逻辑
  - [ ] 读取 session config，构建 view schema，推送到前端
  - [ ] 接收 submit 事件，更新 session config，restart terminal
  - [ ] 显示 spinner → 完成后 close
  - 状态：⏸️ 待开始

### Phase 4: 前端 [待开始]

- [ ] **Task 4.1**: `WorkspaceSelector` 简化创建流程
  - [ ] 去掉目录浏览，只保留名称输入
  - [ ] RPC 调用移除 project_path 参数
  - 状态：⏸️ 待开始

- [ ] **Task 4.2**: `UIModal` 通用组件
  - [ ] 新增 `UIModal.tsx`，监听 workspace WebSocket 消息中 `ui_view` / `ui_close` 事件
  - [ ] 按 `context_id` 过滤，复用 `ViewRenderer` 渲染
  - [ ] 提交/取消通过 workspace RPC 发送 UIEvent
  - 状态：⏸️ 待开始

- [ ] **Task 4.3**: `App.tsx` 集成 UIModal
  - [ ] 处理 MenuResult `action="ui_modal"`，打开 UIModal
  - [ ] UIModal 关闭时清理状态
  - 状态：⏸️ 待开始

- [ ] **Task 4.4**: `CodeEditorPanel` 占位处理
  - [ ] 保留组件，改为显示占位提示
  - 状态：⏸️ 待开始

### Phase 5: 测试与验证 [待开始]

- [ ] **Task 5.1**: 更新单元测试
  - [ ] `test_workspace_selector.py` — 移除 project_path 参数
  - [ ] 新增 workspace 文件名格式测试
  - [ ] 新增旧格式兼容性测试
  - 状态：⏸️ 待开始

- [ ] **Task 5.2**: 构建前端并验证
  - [ ] `npm --prefix mutbot/frontend run build`
  - [ ] 手动验证：创建工作区、终端 Settings、文件名格式
  - 状态：⏸️ 待开始

## 关键参考### 源码
- `src/mutbot/runtime/workspace.py` — Workspace 数据模型、WorkspaceManager
- `src/mutbot/runtime/storage.py` — 文件持久化、MUTBOT_DIR
- `src/mutbot/menu.py` — Menu Declaration、MenuItem、MenuResult
- `src/mutbot/ui/context.py` — UIContext Declaration（set_view / wait_event / show / close）
- `src/mutbot/ui/toolkit.py` — UIToolkitBase（UIContext 创建和绑定）
- `src/mutbot/ui/context_impl.py` — UIContext @impl（broadcast 推送、Future 管理）
- `src/mutbot/builtins/menus.py` — 内置菜单实现
- `src/mutbot/web/rpc_app.py` — AppMenuOps（menu.query / menu.execute）
- `src/mutbot/web/rpc_workspace.py` — TerminalOps.create（cwd=ws.project_path）
- `src/mutbot/web/rpc_session.py` — SessionOps.create（config.setdefault cwd）
- `frontend/src/components/ToolCallCard.tsx` — ViewRenderer、UIComponent（现有渲染器）
- `frontend/src/components/RpcMenu.tsx` — RpcMenu 组件
- `frontend/src/panels/CodeEditorPanel.tsx` — file.read 使用方
- `frontend/src/components/WorkspaceSelector.tsx` — 创建工作区 UI
