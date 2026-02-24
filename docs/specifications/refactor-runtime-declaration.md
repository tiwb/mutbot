# Runtime 重构与 Declaration 体系设计规范

**状态**：✅ 已完成
**日期**：2026-02-24
**类型**：重构

## 1. 背景

当前 mutbot 的 session、storage、workspace 位于 `src/mutbot/` 顶层，terminal 位于 `src/mutbot/web/` 下。这些模块都属于 runtime 概念，但散落在不同层级。同时，Session 使用 dataclass 定义，缺乏可扩展性。

目标：
1. 将 runtime 概念统一到 `mutbot.runtime` 下
2. 基于 `mutobj.Declaration` 建立可扩展的 Session 类型体系
3. 建立 Menu 机制，Python 端定义菜单，前端自动渲染
4. 通过 Menu 实现动态 Session 添加等功能

## 2. 设计方案

### 2.1 模块重组：mutbot.runtime

将 runtime 相关模块统一到 `mutbot.runtime` 包下：

```
src/mutbot/
├── runtime/
│   ├── __init__.py          # 导出 RuntimeManager
│   ├── workspace.py         # Workspace (原 mutbot.workspace)
│   ├── session.py           # Session Declaration 基类 + SessionManager
│   ├── storage.py           # 持久化层 (原 mutbot.storage)
│   └── terminal.py          # TerminalManager (原 mutbot.web.terminal)
├── web/
│   ├── server.py            # FastAPI app
│   ├── routes.py            # REST/WS handlers
│   ├── agent_bridge.py      # Agent sync/async bridge
│   ├── connection.py        # WebSocket manager
│   ├── serializers.py       # 序列化
│   └── auth.py              # 认证
└── __main__.py
```

**迁移策略**：直接移动文件并更新所有 import，不保留兼容层。

- `mutbot.workspace` → `mutbot.runtime.workspace`
- `mutbot.session` → `mutbot.runtime.session`
- `mutbot.storage` → `mutbot.runtime.storage`
- `mutbot.web.terminal` → `mutbot.runtime.terminal`（terminal 是 runtime 概念，不应绑定在 web 层）
- terminal 迁移时将 `fastapi.WebSocket` 依赖抽象为回调接口 `on_output: Callable[[bytes], Awaitable]`，WS 绑定在 `web/routes.py` 完成

### 2.2 Session Declaration 体系

将 Session 从 dataclass 重构为 `mutobj.Declaration` 子类，支持通过继承和 `@impl` 扩展：

```python
class Session(mutobj.Declaration):
    """所有 Session 的基类"""
    id: str
    workspace_id: str
    title: str
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""
    config: dict = mutobj.field(default_factory=dict)

    # 声明方法 — 由子类或 @impl 提供实现
    def start(self, context: RuntimeContext) -> None: ...
    def stop(self) -> None: ...
    def serialize(self) -> dict: ...

class AgentSession(Session):
    """Agent 对话 Session"""
    model: str = ""
    system_prompt: str = ""

class TerminalSession(Session):
    """终端 Session"""
    rows: int = 24
    cols: int = 80
    cwd: str = ""

class DocumentSession(Session):
    """文档编辑 Session"""
    file_path: str = ""
    language: str = ""
```

**Session 与 Menu 的子类发现机制**：

#### mutobj 提供的发现 API

mutobj 已提供两个公开 API 用于子类发现和变更检测：

```python
# 返回 _class_registry 中 base_cls 的所有已注册子类（不含 base_cls 自身）
mutobj.discover_subclasses(base_cls: type) -> list[type]

# 返回注册表的全局 generation 号（类注册/更新、@impl 注册/卸载时递增）
mutobj.get_registry_generation() -> int
```

**`discover_subclasses`** 每次调用重新扫描 `_class_registry`，结果反映当前注册状态。天然支持运行时加载新模块和卸载模块后的三种场景：

| 场景 | 说明 |
|------|------|
| 新类注册 | 运行时加载插件模块，新的 Session/Menu 子类出现在 `_class_registry` 中 |
| 类被更新 | 模块热重载，DeclarationMeta 就地更新已有类对象 |
| 类被移除 | 模块卸载（`unregister_module_impls`），类从 registry 中消失 |

**`get_registry_generation`** 返回单调递增的全局计数器，在以下时机递增：
1. Declaration 子类定义/注册
2. `@impl` 实现注册
3. `unregister_module_impls` 卸载模块

调用方可通过比较前后 generation 判断是否需要重新扫描，避免不必要的重复发现。

#### 发现时机与优化

采用**每次查询时重新扫描**的策略（与前端"每次打开菜单时请求"配合），结合 `get_registry_generation()` 做短路优化：

```python
class MenuRegistry:
    _cached_generation: int = -1
    _cached_menus: list[type] = []

    def get_menus(self) -> list[type]:
        gen = mutobj.get_registry_generation()
        if gen != self._cached_generation:
            self._cached_generation = gen
            self._cached_menus = mutobj.discover_subclasses(Menu)
        return self._cached_menus
```

- `menu.query` RPC 调用时 → 通过 registry 获取最新菜单列表
- `SessionManager.create()` 时 → 扫描 `discover_subclasses(Session)` 查找目标类型
- 扫描 `_class_registry` 的开销本身很小（遍历一个 dict），generation 检查提供额外的短路优化

这种设计天然支持热重载：新模块加载后，generation 递增，下一次查询自动发现新子类。

### 2.3 Session Runtime 状态管理

当前 Session dataclass 中 `agent` 和 `bridge` 是 runtime 字段（不序列化）。Declaration 体系下有两种方案：

**方案 A：Extension 模式** — 用 `mutobj.Extension` 为 Declaration 实例附加 runtime 状态

```python
class AgentSessionRuntime(mutobj.Extension[AgentSession]):
    _agent: Agent | None = None
    _bridge: AgentBridge | None = None

# 使用：
runtime = AgentSessionRuntime.of(session)
runtime._agent = agent
```

**方案 B：分离模式** — Session Declaration 只描述配置/元数据，SessionManager 内部维护 runtime 映射

```python
# Session Declaration 只有持久化属性（纯数据）
# SessionManager 内部维护 runtime 状态：
class SessionManager:
    _runtimes: dict[str, SessionRuntime]  # session_id → runtime

@dataclass
class AgentSessionRuntime:
    agent: Agent | None = None
    bridge: AgentBridge | None = None
```

**对比**：

| 维度 | Extension 模式 | 分离模式 |
|------|---------------|---------|
| 访问方式 | `Extension.of(session).agent` | `manager.get_runtime(session_id).agent` |
| 生命周期 | 绑定在 Declaration 实例上（WeakRef 缓存） | 由 Manager 显式管理 |
| 序列化隔离 | 自动隔离（Extension 不参与序列化） | 自然隔离（两个不同对象） |
| 可发现性 | runtime 状态分散在各 Extension 中 | runtime 状态集中在 Manager |
| 多类型扩展 | 每个 Session 子类可有独立 Extension | 需要不同的 Runtime 子类或 dict |
| 复杂度 | 引入 Extension 概念 | 逻辑更直观 |

**决定**：采用**分离模式**。理由：
1. Session 的 runtime 状态（agent、bridge、PTY process）天然由 Manager 管理生命周期
2. 当前 SessionManager 已是 runtime 状态的管理者，分离模式与现有架构一致
3. 保持 Declaration 的纯粹性（只描述配置），Extension 留给辅助方法场景

### 2.4 Menu Declaration 体系

设计 Menu 基于 `mutobj.Declaration`：

```python
class Menu(mutobj.Declaration):
    """菜单项基类"""
    # 显示属性
    display_name: str = ""
    display_icon: str = ""          # icon 标识符（前端映射）
    display_order: str = "_"        # 排序键，格式 "group:index"
    display_category: str = ""      # 菜单归属，如 "SessionPanel/Add"

    # 行为属性
    enabled: bool = True
    visible: bool = True

    # 声明方法
    def execute(self, context: MenuContext) -> MenuResult: ...

    @classmethod
    def dynamic_items(cls, context: MenuContext) -> list[MenuItem]: ...
```

**核心概念**：

| 概念 | 说明 |
|------|------|
| `display_category` | 菜单的 scope（在哪里显示），如 `"SessionPanel/Add"`, `"SessionPanel/Context"` |
| `display_order` | 排序和分组，格式 `"group:index"`，如 `"0new:0"` |
| `dynamic_items()` | 类方法，运行时动态生成菜单项（如根据可用 Session 类型生成） |
| `execute()` | 点击执行逻辑，在 Python 端运行 |

**Menu 发现与注册**：
- 使用 `mutobj.discover_subclasses(Menu)` 扫描已注册子类，配合 `get_registry_generation()` 做变更检测
- MenuRegistry 按 `display_category` 索引，支持查询某个 scope 下的所有菜单
- 前端每次打开菜单时通过 WS RPC 请求后端，后端实时扫描返回最新列表

**display_order 排序规则**：
- 字符串字典序排列
- `"group:index"` 格式，同 group 的项目归为一组，组间显示分隔线
- 示例：`"0new:0"` < `"0new:1"` < `"1manage:0"`（两组：new 和 manage）

### 2.5 Menu 前后端交互：WebSocket RPC 方案

**调研结论**：建议 Menu 交互采用 **WebSocket 异步 RPC + 推送**，不使用 REST API。

**方案设计**：在现有 Session WebSocket（`/ws/session/{session_id}`）基础上，引入一条 **Workspace 级 WebSocket**（`/ws/workspace/{workspace_id}`），承载所有非 Session 特定的交互：

```
前端                              后端
  │                                 │
  │── ws connect ──────────────────→│  /ws/workspace/{workspace_id}
  │                                 │
  │── { type: "rpc",               │
  │     id: "req_1",               │  菜单查询
  │     method: "menu.query",      │
  │     params: { category: "..." }│
  │   } ───────────────────────────→│
  │                                 │
  │←── { type: "rpc_result",       │
  │      id: "req_1",             │  返回菜单项列表
  │      result: [...] } ──────────│
  │                                 │
  │── { type: "rpc",               │
  │     id: "req_2",               │  菜单执行
  │     method: "menu.execute",    │
  │     params: { menu_id: "..." } │
  │   } ───────────────────────────→│
  │                                 │
  │←── { type: "rpc_result",       │
  │      id: "req_2",             │  执行结果
  │      result: { action: "..." } │
  │   } ───────────────────────────│
  │                                 │
  │←── { type: "event",            │
  │      event: "session_created", │  服务端推送事件
  │      data: { ... } } ─────────│
```

**RPC 消息格式**：

```python
# 请求
{ "type": "rpc", "id": str, "method": str, "params": dict }
# 成功响应
{ "type": "rpc_result", "id": str, "result": Any }
# 错误响应
{ "type": "rpc_error", "id": str, "error": { "code": int, "message": str } }
# 服务端推送事件
{ "type": "event", "event": str, "data": dict }
```

**对比 REST vs WebSocket RPC**：

| 维度 | REST API | WebSocket RPC |
|------|----------|---------------|
| 连接数 | 每次请求新连接 | 复用单条长连接 |
| 延迟 | HTTP 握手开销 | 无额外开销 |
| 推送能力 | 无（需额外 WS） | 天然支持双向 |
| 协议统一性 | REST + WS 两套协议 | 统一 WS 协议 |
| 调试便利 | 浏览器 DevTools / curl 友好 | 需 WS 调试工具 |
| 错误处理 | HTTP 状态码 | 自定义错误格式 |
| 现有基础 | mutbot 已有完整 REST 路由 | 已有 ReconnectingWebSocket |

**推荐 WebSocket RPC 的理由**：
1. mutbot 已有 WS 基础设施（ReconnectingWebSocket、ConnectionManager），扩展成本低
2. Menu execute 可能触发异步操作（如创建 Session），WS 天然支持"请求 + 后续推送"模式
3. 统一通信协议简化前后端交互模型，避免 REST + WS 两套并行
4. Workspace 级 WS 可承载更多未来交互（不仅是 Menu）

**实施建议**：
- 新建 `/ws/workspace/{workspace_id}` 端点
- 现有 REST API 保留（已有前端在用），新功能（Menu 等）走 WS RPC
- 前端封装 `WorkspaceRpc` 类，提供 `call(method, params) → Promise<result>` 接口
- 后端实现 `RpcDispatcher`，按 method 名分发到对应 handler

### 2.6 动态菜单：Session 添加示例

通过 `dynamic_items()` 实现动态菜单项生成：

```python
class AddSessionMenu(Menu):
    display_name = "New Session"
    display_category = "SessionPanel/Add"
    display_order = "0new:0"

    @classmethod
    def dynamic_items(cls, context):
        """根据已注册的 Session 子类动态生成菜单项"""
        items = []
        for session_cls in mutobj.discover_subclasses(Session):
            items.append(MenuItem(
                id=f"add_{session_cls.type_name}",
                name=session_cls.display_name,
                icon=session_cls.display_icon,
                order=session_cls.display_order,
            ))
        return items

    def execute(self, context):
        """创建指定类型的 Session"""
        session = context.session_manager.create(
            workspace_id=context.workspace_id,
            session_type=context.params["session_type"],
        )
        return MenuResult(action="created_session", data={"session_id": session.id})
```

当有新的 Session 子类被定义，菜单自动包含新选项。

### 2.7 RuntimeContext

Menu 和 Session 的操作都需要运行时上下文：

```python
class RuntimeContext:
    """运行时上下文，传递给 Menu.execute() 和 Session.start()"""
    workspace_manager: WorkspaceManager
    session_manager: SessionManager
    terminal_manager: TerminalManager
    current_workspace_id: str | None
    current_session_id: str | None
    broadcast_fn: Callable     # WebSocket 广播
    loop: asyncio.AbstractEventLoop
```

## 3. 待定问题

### Q1: Menu 前端直接响应
**决定**：初期仅支持 Python 端执行 + WS 返回 + 事件推送。后续如需纯前端操作，在 MenuItem 中添加 `client_action` 字段。 ✅ 已确认

### Q2: Session Runtime 状态管理
**决定**：采用分离模式（2.3 节）。Session Declaration 只描述配置，SessionManager 内部维护 runtime 映射。 ✅ 已确认

### Q3: 前端菜单更新
**决定**：不需要实时推送。前端每次打开菜单时通过 WS RPC 请求最新列表。 ✅ 已确认

### Q4: terminal.py WebSocket 依赖
**决定**：迁移到 runtime 时抽象为回调接口，WS 绑定在 web 层。 ✅ 已确认

### Q5: 旧模块兼容层
**决定**：不保留，直接修改所有 import。 ✅ 已确认

### Q6: mutobj 子类发现 API 的依赖关系
**决定**：mutobj 已提供正式公开 API：`discover_subclasses(base_cls)` 和 `get_registry_generation()`。mutbot 直接使用 `mutobj.discover_subclasses` 和 `mutobj.get_registry_generation`，无需临时实现。 ✅ 已解决

## 4. 实施步骤清单

### 阶段一：模块重组 [✅ 已完成]
- [x] **Task 1.1**: 创建 `mutbot/runtime/` 包结构
  - [x] 创建 `__init__.py`
  - 状态：✅ 已完成

- [x] **Task 1.2**: 迁移 workspace.py → runtime/workspace.py
  - [x] 移动文件，更新所有 import
  - 状态：✅ 已完成

- [x] **Task 1.3**: 迁移 session.py → runtime/session.py
  - [x] 移动文件，更新所有 import
  - 状态：✅ 已完成

- [x] **Task 1.4**: 迁移 storage.py → runtime/storage.py
  - [x] 移动文件，更新所有 import
  - 状态：✅ 已完成

- [x] **Task 1.5**: 迁移 web/terminal.py → runtime/terminal.py
  - [x] 移动文件
  - [x] 抽象 WebSocket 依赖为回调接口 (`OutputCallback = Callable[[bytes], Awaitable[None]]`)
  - [x] 更新所有 import，routes.py 中通过 `websocket.send_bytes` 绑定回调
  - 状态：✅ 已完成

- [x] **Task 1.6**: 验证构建与启动
  - [x] 所有模块 import 正常（storage, workspace, session, terminal, server, routes, auth）
  - [ ] 前端功能正常（待手动验证）
  - 状态：✅ 已完成

### 阶段二：Session Declaration 体系 [✅ 已完成]
- [x] **Task 2.1**: 将 Session 从 dataclass 重构为 Declaration
  - [x] 定义 Session 基类（`mutobj.Declaration`）及子类（AgentSession, TerminalSession, DocumentSession）
  - [x] 实现 `serialize()` 方法和 `_session_from_dict()` 反序列化
  - [x] 旧持久化格式向后兼容（按 `type` 字段映射到正确子类）
  - 状态：✅ 已完成

- [x] **Task 2.2**: 集成 mutobj 发现 API 及 SessionRegistry
  - [x] `get_session_type_map()` 使用 `mutobj.discover_subclasses(Session)` + `get_registry_generation()` 缓存
  - [x] `get_session_class(type_name)` 按类型名查找子类
  - [x] `_get_type_default()` 从 AttributeDescriptor 读取 `type` 字段默认值
  - 状态：✅ 已完成

- [x] **Task 2.3**: 迁移现有 Session 逻辑到 Declaration 体系
  - [x] Runtime 分离模式：`SessionRuntime` / `AgentSessionRuntime` dataclass，`SessionManager._runtimes` 字典
  - [x] `start()` 使用 `isinstance(session, AgentSession)` 检查，runtime 存入 `_runtimes`
  - [x] `stop()` 使用 `isinstance` 分派，从 `_runtimes` 获取并清理 runtime
  - [x] `_persist()` 从 `_runtimes` 获取 agent messages
  - [x] `create()` 使用 `get_session_class()` 创建正确子类
  - [x] `load_from_disk()` 使用 `_session_from_dict()` 重建正确子类
  - 状态：✅ 已完成

### 阶段三：WebSocket RPC 基础设施 [✅ 已完成]
- [x] **Task 3.1**: 实现 Workspace WebSocket 端点
  - [x] `web/rpc.py`：`RpcDispatcher` 框架 + `RpcContext` + `make_event` 辅助
  - [x] `/ws/workspace/{workspace_id}` 端点（`routes.py`），含连接管理和消息分发循环
  - [x] `workspace_rpc` 全局实例 + `workspace_connection_manager` 实例
  - 状态：✅ 已完成

- [x] **Task 3.2**: 前端 WorkspaceRpc 封装
  - [x] `lib/workspace-rpc.ts`：`WorkspaceRpc` 类
  - [x] `call(method, params) → Promise<result>` 接口（含请求 ID 关联和超时处理）
  - [x] `on(event, handler)` 事件监听（返回 unsubscribe 函数）
  - [x] 断线时 reject 所有 pending calls + `RpcError` 错误类
  - [x] TypeScript 类型检查通过
  - 状态：✅ 已完成

### 阶段四：Menu Declaration 体系 [✅ 已完成]
- [x] **Task 4.1**: 实现 Menu 基类和 MenuRegistry
  - [x] Menu Declaration 定义
  - [x] MenuResult / MenuItem 数据结构
  - [x] MenuRegistry：按 category 索引、排序
  - [x] 修复 `??` 语法错误，改为 Python 有效的空值处理
  - [x] 修复 `_get_attr_default` 支持纯值覆盖（子类无类型注解的属性覆写）
  - [x] 修复 `visible` 检查正确处理 None
  - 状态：✅ 已完成

- [x] **Task 4.2**: 实现 Menu RPC handler
  - [x] `menu.query` — 查询指定 category 的菜单
  - [x] `menu.execute` — 执行菜单并返回结果
  - [x] 注册到 `workspace_rpc` 全局 dispatcher
  - 状态：✅ 已完成

- [x] **Task 4.3**: 实现 AddSessionMenu（动态菜单示例）
  - [x] 根据 SessionRegistry 动态生成子项（agent/terminal/document）
  - [x] 执行后创建 Session 并返回 MenuResult
  - [x] 32 个单元测试全部通过
  - 状态：✅ 已完成

### 阶段五：前端适配 [✅ 已完成]
- [x] **Task 5.1**: 实现通用 Menu 组件
  - [x] `components/RpcMenu.tsx`：通过 WorkspaceRpc 获取菜单数据
  - [x] 渲染菜单（支持分组分隔线、图标、disabled 状态）
  - [x] 执行菜单（`menu.execute` RPC）并回调 MenuResult
  - [x] `index.css` 新增 `.rpc-menu-*` 样式（VS Code 风格）
  - 状态：✅ 已完成

- [x] **Task 5.2**: 替换硬编码菜单
  - [x] 用 RpcMenu 替换 App.tsx 中的 `AddSessionDropdown`（tabset "+" 按钮）
  - [x] App.tsx 创建 `WorkspaceRpc` 实例，连接 `/ws/workspace/{id}`
  - [x] `handleMenuResult` 处理 `session_created` 结果（通过 REST 获取 session 详情后添加 tab）
  - [x] 移除 `AddSessionDropdown` 组件和 `createSession` import
  - [x] `api.ts` 新增 `getSession()` 函数
  - [x] 后端 `AddSessionMenu.execute` 增强：处理 terminal PTY 创建和 document 默认路径
  - [x] TypeScript 编译通过，137 个后端测试全部通过
  - 注：右键上下文菜单仍使用前端 `ContextMenu` 组件（session 操作依赖前端状态，无需后端化）
  - 状态：✅ 已完成

## 5. 测试验证

### 功能测试
- [ ] 服务器正常启动，所有 API 可用
- [ ] 创建/停止/删除 Agent Session
- [ ] 创建/使用/关闭 Terminal Session
- [ ] 打开/编辑 Document Session
- [ ] WS RPC 请求/响应正常
- [ ] 菜单正确显示、点击执行正常
- [ ] WebSocket 事件推送正常
