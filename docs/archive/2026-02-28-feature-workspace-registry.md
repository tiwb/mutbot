# Workspace 注册表管理 设计规范

**状态**：✅ 已完成
**日期**：2026-02-28
**类型**：功能设计

## 1. 背景

当前 workspace 系统有以下问题：

1. **启动自动创建**：`WorkspaceManager.ensure_default()` 在服务器启动时自动创建 workspace，用户无法从空列表开始
2. **无法删除**：WorkspaceSelector 没有删除 workspace 的功能
3. **无注册表**：workspace 列表直接从 `~/.mutbot/workspaces/*.json` 文件 glob 获取，单元测试创建的 workspace 也会出现在用户列表中

## 2. 设计方案

### 2.1 引入 workspace 注册表文件

新增 `~/.mutbot/workspaces/registry.json` 配置文件，管理用户可见的 workspace 列表：

```json
{
  "workspaces": ["a1b2c3d4e5f6", "f6e5d4c3b2a1"]
}
```

- `workspaces`: workspace ID 的有序数组，顺序即列表显示顺序（最近访问在前）
- 只有在此列表中的 workspace 才对用户可见
- workspace JSON 数据文件（`{id}.json`）仍正常保留，不受注册表影响
- `registry.json` 不存在时视为空列表（不做迁移）

### 2.2 WorkspaceManager 改造

**`load_from_disk()` 变更**：
- 加载 `registry.json`，获取已注册 ID 列表
- 只加载注册表中的 workspace JSON 文件（而非 glob 全部）
- 注册表中引用的 ID 如果对应 JSON 文件不存在，自动从注册表移除

**`create()` 变更**：
- 创建 workspace 后，将 ID 追加到 `registry.json`

**新增 `remove(workspace_id)` 方法**：
- 从注册表移除 workspace ID
- 从内存 `_workspaces` 字典移除
- **不删除** `{id}.json` 文件（保留数据）

**移除 `ensure_default()`**：
- 删除此方法
- `server.py` 中不再调用

### 2.3 storage 层新增

```python
def load_workspace_registry() -> list[str]:
    """加载注册表，返回 workspace ID 列表。文件不存在返回空列表。"""

def save_workspace_registry(ids: list[str]) -> None:
    """原子写入注册表。"""
```

### 2.4 右键菜单删除 — 复用 RpcMenu 框架（服务端处理）

#### 问题：RpcMenu 依赖 WorkspaceRpc，但 workspace 选择页面只有 AppRpc

`RpcMenu` 当前类型签名为 `rpc: WorkspaceRpc | null`，而 `menu.query` / `menu.execute` 只注册在 `workspace_rpc` 上。workspace 选择页面没有 workspace 连接，只有 `AppRpc`。

#### 解决方案

两者的 `.call()` 方法签名完全一致，改造成本低：

**前端**：提取 `RpcClient` 接口，让 RpcMenu 接受两者

```typescript
// lib/types.ts
export interface RpcClient {
  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
}
```

`AppRpc` 和 `WorkspaceRpc` 已天然实现此接口，无需修改。RpcMenu 的 `rpc` 属性类型从 `WorkspaceRpc | null` 改为 `RpcClient | null`。

**后端**：在 `app_rpc` 上注册 `menu.query` + `menu.execute`

```python
# routes.py — app_rpc 也注册菜单处理
@app_rpc.method("menu.query")
async def handle_app_menu_query(params: dict, ctx: RpcContext) -> list[dict]:
    category = params.get("category", "")
    menu_context = params.get("context", {})
    ctx._menu_context = menu_context
    return menu_registry.query(category, ctx)

@app_rpc.method("menu.execute")
async def handle_app_menu_execute(params: dict, ctx: RpcContext) -> dict:
    menu_id = params.get("menu_id", "")
    menu_cls = menu_registry.find_menu_class(menu_id)
    menu_instance = menu_cls()
    result = menu_instance.execute(params.get("params", {}), ctx)
    result_dict = {"action": result.action, "data": result.data}

    # app 级后续处理：workspace_removed
    if result.action == "workspace_removed":
        wm = ctx.managers.get("workspace_manager")
        ws_id = result.data.get("workspace_id", "")
        if wm and ws_id:
            wm.remove(ws_id)

    return result_dict
```

**后端**：新增 `RemoveWorkspaceMenu` 菜单声明

```python
class RemoveWorkspaceMenu(Menu):
    display_name = "Remove"
    display_icon = "trash-2"
    display_category = "WorkspaceSelector/Context"
    display_order = "0manage:0"

    def execute(self, params: dict, context: RpcContext) -> MenuResult:
        workspace_id = params.get("workspace_id", "")
        if not workspace_id:
            return MenuResult(action="error", data={"message": "missing workspace_id"})
        return MenuResult(
            action="workspace_removed",
            data={"workspace_id": workspace_id},
        )
```

**前端**：WorkspaceSelector 中使用 RpcMenu 上下文菜单模式

```typescript
// 右键 workspace 项
onContextMenu={(e) => {
  e.preventDefault();
  setContextMenu({ position: { x: e.clientX, y: e.clientY }, ws });
}}

// 渲染
{contextMenu && (
  <RpcMenu
    rpc={appRpc}  // AppRpc 实现了 RpcClient
    category="WorkspaceSelector/Context"
    context={{ workspace_id: contextMenu.ws.id }}
    position={contextMenu.position}
    onClose={() => setContextMenu(null)}
    onResult={(result) => {
      if (result.action === "workspace_removed") {
        setWorkspaces(prev => prev.filter(w => w.id !== result.data.workspace_id));
      }
    }}
  />
)}
```

### 2.5 New Workspace 对话框改造

将 DirectoryPicker 改造为 **New Workspace** 对话框，合并目录选择和命名功能：

- **标题**：改为 "New Workspace"
- **新增 Name 输入框**：
  - 位于 path bar 上方（Name 在上，Path 在下）
  - placeholder 初始为 `"Workspace name (optional)"`
  - 用户选择目录后，placeholder 变为 `"Default: {目录名}"`（如 `"Default: my-project"`）
  - 用户不填写时，自动使用目录名
- **确认按钮**：文案从 "Select" 改为 "Create"
- **点击外部不关闭**：移除 overlay 的 `onClick={onCancel}`，只通过 Cancel 按钮关闭
- **固定大小**：对话框使用固定尺寸（width + max-height），目录列表区域 overflow-y auto，不随内容变化
- **创建时传递 name**：`workspace.create({ project_path, name })` RPC 调用时传入用户输入的名称

### 2.6 前端：hash 路由到不存在的 workspace

- hash 指向不存在的 workspace 时，**清空 hash** 回到 `#`
- 用户看到 workspace 选择页面，不会困惑 URL 还挂着失效的 hash

### 2.7 server.py 启动流程改造

```python
# 之前
ws = workspace_manager.ensure_default()
if not _mutbot_config.get("providers"):
    _ensure_setup_session(ws, session_manager, workspace_manager)

# 之后
workspace_manager.load_from_disk()
# 不再调用 ensure_default()
# setup 模式推迟到用户创建第一个 workspace 时处理
```

Setup 模式（GuideSession）的处理：
- 不再在启动时自动创建
- 改为在 `workspace.create` RPC 中检查：如果是用户创建的第一个 workspace 且无 LLM provider 配置，则创建 GuideSession

## 3. 待定问题

（已全部解决）

## 4. 实施步骤清单

### 阶段一：后端 — storage 层 + 注册表 [✅ 已完成]
- [x] **Task 1.1**: storage.py 新增注册表读写函数
  - [x] `load_workspace_registry()` — 加载 `registry.json`
  - [x] `save_workspace_registry(ids)` — 原子写入注册表
  - [x] `load_workspace(workspace_id)` — 加载单个 workspace
  - [x] `load_all_workspaces()` 排除 registry.json
  - 状态：✅ 已完成

### 阶段二：后端 — WorkspaceManager 改造 [✅ 已完成]
- [x] **Task 2.1**: `load_from_disk()` 使用注册表驱动加载
  - [x] 加载注册表 ID 列表
  - [x] 只加载注册表中的 workspace
  - [x] 清理无效 ID（JSON 文件不存在的）
  - 状态：✅ 已完成

- [x] **Task 2.2**: `create()` 写入注册表
  - [x] 创建后将 ID 插入到注册表最前面
  - 状态：✅ 已完成

- [x] **Task 2.3**: 新增 `remove()` 方法
  - [x] 从注册表和内存移除
  - [x] 不删除数据文件
  - 状态：✅ 已完成

- [x] **Task 2.4**: 移除 `ensure_default()` 方法
  - 状态：✅ 已完成

### 阶段三：后端 — server.py + routes.py + menus [✅ 已完成]
- [x] **Task 3.1**: server.py 移除 `ensure_default()` 调用
  - [x] setup 模式逻辑移入 `workspace.create` RPC
  - [x] app WS context 包含 session_manager
  - 状态：✅ 已完成

- [x] **Task 3.2**: routes.py — `app_rpc` 注册 `menu.query` + `menu.execute` + `workspace.remove`
  - [x] app 级 `menu.query` handler
  - [x] app 级 `menu.execute` handler（处理 `workspace_removed` action）
  - [x] `workspace.remove` RPC handler
  - 状态：✅ 已完成

- [x] **Task 3.3**: menus.py 新增 `RemoveWorkspaceMenu`
  - [x] `display_category = "WorkspaceSelector/Context"`
  - [x] `execute()` 返回 `workspace_removed` action
  - 状态：✅ 已完成

### 阶段四：前端 [✅ 已完成]
- [x] **Task 4.1**: 提取 `RpcClient` 接口
  - [x] `lib/types.ts` 新增 `RpcClient` 接口
  - [x] RpcMenu 的 `rpc` 属性改为 `RpcClient | null`
  - 状态：✅ 已完成

- [x] **Task 4.2**: WorkspaceSelector 右键菜单删除
  - [x] workspace 项支持右键打开 RpcMenu
  - [x] 搜索弹窗项同样支持右键
  - [x] 处理 `workspace_removed` result → 更新 workspaces 状态
  - [x] 新增 `onRemoved` callback prop
  - 状态：✅ 已完成

- [x] **Task 4.3**: DirectoryPicker → New Workspace 对话框改造
  - [x] 标题改为 "New Workspace"
  - [x] 新增 Name 输入框（在 path bar 上方），placeholder 随目录变化
  - [x] 确认按钮文案改为 "Create"
  - [x] 点击 overlay 不关闭（移除 overlay onClick）
  - [x] 固定对话框尺寸（height: min(560px, 80vh)），目录列表 overflow-y auto
  - [x] 创建时传递 name 参数
  - 状态：✅ 已完成

- [x] **Task 4.4**: hash 路由改进
  - [x] 初始加载：不存在的 hash → 清空
  - [x] hashchange 监听：不存在的 hash → 清空 + setWorkspace(null)
  - 状态：✅ 已完成

### 阶段五：测试 [✅ 已完成]
- [x] **Task 5.1**: 后端单元测试
  - [x] 注册表读写测试（4 个）
  - [x] WorkspaceManager 注册表集成测试（7 个）
  - [x] workspace.remove RPC 测试（3 个）
  - [x] TypeScript 类型检查通过
  - 状态：✅ 已完成

## 5. 测试验证

### 单元测试
- [x] storage: `load_workspace_registry` / `save_workspace_registry` 读写正确
- [x] WorkspaceManager: `create` 后 ID 出现在注册表
- [x] WorkspaceManager: `remove` 后 ID 从注册表消失，JSON 文件保留
- [x] WorkspaceManager: `load_from_disk` 只加载注册表中的 workspace
- [x] WorkspaceManager: 无 `registry.json` 时返回空列表
- [x] WorkspaceManager: 注册表中无效 ID 自动清理
- 执行结果：55/55 通过

### 集成测试
- [ ] 启动服务器，workspace 列表为空时不自动创建
- [ ] 创建 workspace → 列表中出现
- [ ] 右键删除 workspace → 列表中消失
- [ ] hash 导航到不存在 workspace → 回到选择页面
- [ ] New Workspace 对话框：选择目录后 name placeholder 更新
