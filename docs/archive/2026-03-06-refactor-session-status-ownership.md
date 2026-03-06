# Session 状态归属重构 设计规范

**状态**：✅ 已完成
**日期**：2026-03-06
**类型**：重构

## 背景

Session 生命周期管理存在抽象缺失：所有类型的 session 创建、启动、停止、状态设置逻辑**散落在 SessionManager、routes.py、menus.py、App.tsx 中**，通过 `isinstance` 判断或 `issubclass` 分支处理各类型的差异行为。这违反了多态原则——各 Session 子类应自行定义自己的生命周期行为。

### 现状：isinstance/issubclass 分支散落位置

**创建后初始化**：
- `routes.py:328-342`：`issubclass(TerminalSession)` → 创建 PTY、设 running；`issubclass(DocumentSession)` → 设默认 file_path
- `menus.py:86-105`：几乎相同的 `issubclass` 分支，重复逻辑

**停止**：
- `session_impl.py:669-684`：`isinstance(AgentSession)` → 停 bridge、移 handler；`isinstance(TerminalSession)` → kill PTY；统一设 `"stopped"`

**序列化/反序列化**：
- `session_impl.py:64-80`（serialize_session）：`isinstance(AgentSession)` → 写 model/tokens 等；`isinstance(DocumentSession)` → 写 file_path/language
- `session_impl.py:185-201`（_session_from_dict）：`issubclass(AgentSession)` → 读 model/tokens 等；`issubclass(DocumentSession)` → 读 file_path/language

**WS 启动判断**：
- `routes.py:891`：`session.status not in ("", "stopped")` + 隐式依赖 `sm.start()` 内部 isinstance
- `routes.py:911-914`：同上，隐式依赖 TerminalSession 不发 message

**重启清理**：
- `server.py:170-174`：遍历所有 session，`"running"` → `""`，不区分类型

### 根本问题

1. **SessionManager.stop() 越权**：统一设 `"stopped"`，但 AgentSession 不应有此状态
2. **行为分支散落**：创建/停止/序列化的类型特定逻辑分散在多处，routes.py 和 menus.py 存在重复代码
3. **隐式类型依赖**：WS 路由通过状态值间接区分类型，而不是显式的类型判断
4. **状态语义混杂**：`"stopped"` 是 TerminalSession 专属语义，被 SessionManager 强加给所有类型
5. **序列化硬编码**：serialize_session 和 _session_from_dict 用 isinstance 分支处理子类字段，每加一个 Session 子类就要改两处

## 设计方案

### 核心设计

在 Session 基类上声明生命周期桩方法和序列化桩方法，各子类通过 `@impl` 提供自己的实现。SessionManager 调用这些多态方法，不再做 isinstance 分支。

#### 生命周期方法

**Session 基类新增声明**（`session.py`）：

```python
class Session(mutobj.Declaration):
    # ... 现有字段 ...

    def on_create(self, sm: SessionManager) -> None:
        """创建后的初始化（设状态、创建关联资源等）。

        sm 提供 terminal_manager、config 等运行时资源。
        各子类按需从 sm 取用，基类默认空操作。
        """
        ...

    def on_stop(self, sm: SessionManager) -> None:
        """停止时的关联资源清理和状态归位。

        runtime 资源（bridge、log handler）由 SessionManager 清理，
        此方法只负责 Session 自身的状态和关联资源（如 PTY）。
        """
        ...

    def on_restart_cleanup(self) -> None:
        """服务器重启时清理残留状态（无需外部资源）。"""
        ...
```

`on_create` 和 `on_stop` 接收 `sm: SessionManager` 参数，各子类按需取用：
- TerminalSession.on_create：从 `sm.terminal_manager` 创建 PTY，从 `self.config` 读取 rows/cols/cwd
- TerminalSession.on_stop：从 `sm.terminal_manager` kill PTY
- AgentSession / DocumentSession：不依赖 sm 的额外资源
- on_restart_cleanup 无参数：重启时只做状态清理，不需要外部资源（PTY 进程已死、bridge 已失效）

> **cwd 传递**：SessionManager 上没有 workspace_manager（当前 workspace_manager、session_manager、terminal_manager 是 server.py 中的三个平级全局对象，通过 RpcContext.managers dict 松耦合连接；只有 terminal_manager 注入到了 SessionManager 上）。当前方案：调用方把 cwd 预写入 config，on_create 从 self.config 读取，cwd 不与 workspace 强绑定。如将来 on_create/on_stop 需要访问 workspace_manager，可像 terminal_manager 一样在 server.py lifespan 中注入 `sm.workspace_manager = workspace_manager`。

**各子类实现**：

| 方法 | AgentSession | TerminalSession | DocumentSession |
|------|-------------|-----------------|-----------------|
| `on_create` | 无操作（默认） | 创建 PTY，设 `status = "running"` | 设默认 file_path（若未设） |
| `on_stop` | 设 `status = ""` | kill PTY，设 `status = "stopped"` | 无操作（默认，status 已为空） |
| `on_restart_cleanup` | `"running"` → `""` | `"running"` → `"stopped"` | 无操作（默认） |

**各 Session 类型的完整状态模型**：

| Session 类型 | 有效状态 | 状态控制者 |
|-------------|---------|-----------|
| AgentSession | `""` / `"running"` | AgentBridge._broadcast_status() 设 running/空；on_stop 归位为空 |
| TerminalSession | `"running"` / `"stopped"` | on_create 设 running；前端 PTY exit 或 on_stop 设 stopped |
| DocumentSession | `""` | 无状态变化 |

#### 序列化/反序列化

当前 serialize_session 和 _session_from_dict 用 isinstance 分支硬编码各子类的字段。改为在 Session 基类上提供默认实现 + 子类可覆盖。

**命名**：保持 `serialize` / `deserialize`，不加 `on_` 前缀。`on_` 用于生命周期钩子（被动回调），serialize/deserialize 是主动操作。且 `serialize` 已是现有 API（`session.py:52`），改名会破坏接口。

**Session 基类声明**（`serialize` 已有，新增 `deserialize`）：

```python
class Session(mutobj.Declaration):
    # ... 现有字段 ...

    def serialize(self) -> dict:
        """序列化为 dict。默认实现基于 __annotations__ 自动收集所有字段。"""
        ...

    @classmethod
    def deserialize(cls, data: dict) -> Session:
        """从 dict 重建 Session 实例。默认实现基于 __annotations__ 自动提取字段。"""
        ...
```

**默认实现**（`@impl`，替换当前的 serialize_session 和 _session_from_dict）：

- `serialize`：遍历 `type(self)` 的完整 `__annotations__` 链（含继承），收集所有声明字段。非空/非默认值的字段写入 dict
- `deserialize`：根据目标类的 `__annotations__` 从 data dict 中提取对应 key，构造实例

**子类控制能力**：子类可通过 `@impl` 覆盖 serialize/deserialize 来定制行为（如跳过某些字段、添加计算字段等），但大多数情况下默认实现足够。

新增 Session 子类时：只需声明字段（`field_name: type = default`），序列化/反序列化自动生效，无需改任何现有代码。

### SessionManager 改动

**stop() 方法**简化为：
```python
async def stop(self, session_id: str) -> None:
    session = self._sessions.get(session_id)
    if session is None:
        return
    # runtime 资源清理（bridge/handler 由 SessionManager 管理）
    rt = self._runtimes.get(session_id)
    if rt:
        if rt.bridge is not None:
            await rt.bridge.stop()
        _remove_session_log_handler(rt.log_handler)
    # 子类自行处理状态归位和关联资源清理
    session.on_stop(self)
    session.updated_at = datetime.now(timezone.utc).isoformat()
    self._persist(session)
    self._runtimes.pop(session_id, None)
```

**create() 方法**末尾增加：
```python
session.on_create(self)
self._persist(session)  # on_create 可能改了 status/config
```

**重启清理**改为：
```python
for session in session_manager._sessions.values():
    session.on_restart_cleanup()
    session_manager._persist(session)
```

### routes.py / menus.py 清理

- **routes.py session.create RPC**：移除 `issubclass(TerminalSession)` 和 `issubclass(DocumentSession)` 分支，`sm.create()` 内部调用 `on_create`。调用方将 rows/cols/cwd 预写入 config
- **menus.py new_session**：同上，移除 issubclass 分支，将 cwd 预写入 config
- **routes.py:107**：移除 `s.status != "stopped"` 条件（AgentSession 不再有 stopped 状态）
- **routes.py:522**：返回实际 session 状态而非硬编码 `"stopped"`
- **routes.py:891 WS 启动判断**：改为 `isinstance(session, AgentSession) and session.status != ""`
- **routes.py:911-914 延迟启动**：加 `isinstance(session, AgentSession)` 守卫，简化 `was_inactive`

> 注：routes.py:891 和 911 处的 isinstance 保留——这不是"行为分支散落"问题，而是 WS 路由对"是否需要启动 agent bridge"的合理类型判断。agent bridge 启动逻辑（sm.start）本身就是 AgentSession 专属的。

### 前端无需改动

- `App.tsx:659`：terminal exit 设 `"stopped"`——正确，TerminalSession 专属行为
- `SessionListPanel.tsx:20`：`stopped` 显示为 "Stopped"——保留，给 TerminalSession 用
- `index.css:500`：`.status-stopped` 样式——保留

## 设计决策

### Q1: on_stop 职责边界（已确认）

runtime 资源（bridge、log handler）的清理仍在 SessionManager 中（`_runtimes` 是 SessionManager 的内部状态），`on_stop` 只负责 Session 自身的状态归位和关联资源（如 TerminalSession 的 PTY kill）。

### Q2: 外部资源访问方式（已修正）

on_create / on_stop 接收 `sm: SessionManager` 参数，各子类在方法内按需取用（如 `sm.terminal_manager`），用完即走，**不需要在 session 上持久保存引用**。

terminal_manager 只在两个生命周期节点被使用：
- `on_create`：`sm.terminal_manager.create(...)` 创建 PTY
- `on_stop`：`sm.terminal_manager.kill(...)` 杀掉 PTY

两次调用之间 TerminalSession 不需要 terminal_manager（PTY 进程独立运行），因此传参方案自然解决了注入问题。

on_restart_cleanup 无参数：重启时 PTY 进程已死，只需清理状态，不需要外部资源。

### Q3: on_create 接管 PTY 创建（已确认）

TerminalSession.on_create 从 `sm.terminal_manager` 创建 PTY，从 `self.config` 读取 rows/cols/cwd。routes.py 和 menus.py 的 issubclass 分支移除。

调用方把 rows/cols/cwd 预写入 config（cwd 由调用方从 workspace 取得，SessionManager 上没有 workspace_manager）：
```python
# routes.py session.create RPC（简化后）
config = {"rows": rows, "cols": cols, "cwd": ws.project_path}
session = sm.create(workspace_id, session_type, config=config)
```

### Q4: session.stop RPC 返回值（已确认）

前端未使用，改为返回实际 session 状态。同步更新测试。

## 关键参考

### 源码
- `mutbot/src/mutbot/session.py` — Session 声明类（需新增 on_create/on_stop/on_restart_cleanup）
- `mutbot/src/mutbot/runtime/session_impl.py:50-81` — serialize_session（isinstance 分支，待改为自动收集）
- `mutbot/src/mutbot/runtime/session_impl.py:164-203` — _session_from_dict（issubclass 分支，待改为自动收集）
- `mutbot/src/mutbot/runtime/session_impl.py:473-503` — SessionManager.create()
- `mutbot/src/mutbot/runtime/session_impl.py:590-662` — SessionManager.start()（Agent 专属，保持不变）
- `mutbot/src/mutbot/runtime/session_impl.py:664-694` — SessionManager.stop()（核心改动点）
- `mutbot/src/mutbot/runtime/session_impl.py:578-586` — set_session_status()
- `mutbot/src/mutbot/web/agent_bridge.py:76-87` — AgentBridge._broadcast_status() 状态同步
- `mutbot/src/mutbot/web/routes.py:107` — agent session 查找（跳过 stopped，待移除条件）
- `mutbot/src/mutbot/web/routes.py:310-354` — session.create RPC（issubclass 分支，待清理）
- `mutbot/src/mutbot/web/routes.py:515-522` — session.stop RPC 返回值
- `mutbot/src/mutbot/web/routes.py:890-925` — WS 连接时的启动逻辑（891 + 912 两处）
- `mutbot/src/mutbot/web/server.py:168-176` — 重启时清理 running 状态
- `mutbot/src/mutbot/builtins/menus.py:86-105` — new_session 菜单（issubclass 分支，待清理）
- `mutobj/src/mutobj/core.py:467` — DeclarationMeta 通过 __annotations__ 收集字段

### 相关规范
- `docs/archive/2026-02-26-feature-agent-runtime-status.md` — agent 状态同步设计
- `docs/archive/2026-02-23-feature-session-panel-unification.md` — session 统一设计，status 开放字符串
- `docs/specifications/feature-session-list-management.md` — session 列表管理

## 实施步骤清单

### 阶段一：Session 声明层 [✅ 已完成]

- [x] **Task 1.1**: session.py — 新增生命周期桩方法
  - [x] Session 基类新增 `on_create(self, sm)` / `on_stop(self, sm)` / `on_restart_cleanup(self)` 桩方法
  - [x] Session 基类新增 `deserialize(cls, data)` 类方法桩
  - [x] TYPE_CHECKING 下 import SessionManager（避免循环引用）
  - 状态：✅ 已完成

### 阶段二：序列化/反序列化 [✅ 已完成]

- [x] **Task 2.1**: session_impl.py — serialize 改为基于 __annotations__ 自动收集
  - [x] 重写 `serialize_session`：遍历 MRO `__annotations__` 链自动收集所有声明字段
  - [x] 移除 `isinstance(AgentSession)` / `isinstance(DocumentSession)` 分支
  - [x] 确保与现有序列化结果兼容（字段名、空值跳过行为一致）
  - 状态：✅ 已完成

- [x] **Task 2.2**: session_impl.py — deserialize 替换 _session_from_dict
  - [x] 在 session_impl.py 中实现 `@impl(Session.deserialize)`，基于目标类 __annotations__ 自动提取字段
  - [x] 替换 `_session_from_dict` 函数调用（`load_from_disk` 中）
  - [x] 移除 `issubclass(AgentSession)` / `issubclass(DocumentSession)` 分支
  - 状态：✅ 已完成

### 阶段三：生命周期方法实现 [✅ 已完成]

> **实施偏差（已修正）**：最初 mutobj 的 `@impl` 无法对子类分别注册 impl，采用了单一 `@impl(Session.on_xxx)` 内部 isinstance 分派的临时方案。`mutobj` 修复继承桩方法冲突后（见 `mutobj/docs/specifications/bugfix-impl-inheritance-collision.md`），已改为标准的 per-subclass `@impl`：`@impl(TerminalSession.on_create)`、`@impl(AgentSession.on_stop)` 等，彻底消除 isinstance 分支。

- [x] **Task 3.1-3.3**: session_impl.py — 实现生命周期方法
  - [x] `@impl(Session.on_create)`：TerminalSession → PTY + running，DocumentSession → 默认 file_path
  - [x] `@impl(Session.on_stop)`：AgentSession → 空，TerminalSession → kill PTY + stopped
  - [x] `@impl(Session.on_restart_cleanup)`：AgentSession → running→空，TerminalSession → running→stopped
  - 状态：✅ 已完成

### 阶段四：SessionManager 接入多态方法 [✅ 已完成]

- [x] **Task 4.1**: session_impl.py — stop() 方法重构
  - [x] 移除 `isinstance(AgentSession)` / `isinstance(TerminalSession)` 分支
  - [x] runtime 清理保留在 stop() 中（通用 _runtimes 检查）
  - [x] 调用 `session.on_stop(self)` 替代统一的 `session.status = "stopped"`
  - [x] 移除末尾统一状态赋值
  - 状态：✅ 已完成

- [x] **Task 4.2**: session_impl.py — create() 方法接入 on_create
  - [x] 在 `self._persist(session)` 之前调用 `session.on_create(self)`
  - [x] on_create 后再 persist（on_create 可能修改 status/config）
  - 状态：✅ 已完成

- [x] **Task 4.3**: server.py — 重启清理改用 on_restart_cleanup
  - [x] 替换 `if session.status == "running"` 硬编码为 `session.on_restart_cleanup()`
  - [x] 只 persist 状态实际变化的 session（on_restart_cleanup 内部判断）
  - 状态：✅ 已完成

### 阶段五：routes.py / menus.py 清理 [✅ 已完成]

- [x] **Task 5.1**: routes.py — session.create RPC 移除 issubclass 分支
  - [x] 移除 `issubclass(TerminalSession)` 的 PTY 创建分支，改为将 rows/cols/cwd 写入 config
  - [x] 移除 `issubclass(DocumentSession)` 的 file_path 分支
  - [x] sm.create() 内部已调用 on_create
  - 状态：✅ 已完成

- [x] **Task 5.2**: menus.py — new_session 移除 issubclass 分支
  - [x] 移除 `issubclass(TerminalSession)` 分支，将 cwd 写入 config
  - [x] 移除 `issubclass(DocumentSession)` 分支
  - [x] 移除创建后设 `session.status = "running"` 的代码
  - 状态：✅ 已完成

- [x] **Task 5.3**: routes.py — 清理 stopped 相关判断
  - [x] 107 行：移除 `s.status != "stopped"` 条件
  - [x] 522 行：返回实际 session 状态 `session.status`
  - [x] 891 行：改为 `isinstance(session, AgentSession) and session.status != ""`
  - [x] 911-914 行：加 `isinstance(session, AgentSession)` 守卫，`was_inactive = session.status == ""`
  - 状态：✅ 已完成

### 阶段六：测试 [✅ 已完成]

- [x] **Task 6.1**: 更新现有测试
  - [x] `test_rpc_handlers.py`：更新 session.stop 返回值断言、terminal 创建断言
  - [x] `test_session_persistence.py`：更新 stopped → 空状态断言
  - [x] `test_runtime_session.py`：替换 `_session_from_dict` → `Session.deserialize`
  - [x] `test_runtime_imports.py`：移除 `_session_from_dict` 导入
  - [x] `test_runtime_menu.py`：更新 terminal 创建和缺少 terminal_manager 的测试
  - 状态：✅ 已完成

- [x] **Task 6.2**: 运行测试验证
  - [x] `cd mutbot && python -m pytest tests/ -x` — 381 passed
  - 状态：✅ 已完成

## 测试验证

- 381 tests passed，无失败
- 序列化/反序列化往返测试通过（AgentSession、TerminalSession、DocumentSession）
- SessionManager.stop() 各类型状态归位正确（Agent→空，Terminal→stopped）
- 重启清理逻辑正确（Agent running→空，Terminal running→stopped）
