# session.py 声明-实现分离重构 设计规范

**状态**：✅ 已完成
**日期**：2026-02-25
**类型**：重构

## 1. 背景

`session.py` 残留了旧的兼容代码和不符合声明-实现分离模式的自由函数：

- `_LEGACY_TYPE_MAP` — 旧短名称→全限定名映射，不再需要向后兼容
- `DEFAULT_SESSION_TYPE` — 全局默认类型常量，现在新建 Session 需要指定类型
- `get_session_class()` — 模块级自由函数，应为 `Session` 的静态方法
- 声明文件中包含实现逻辑，不符合 mutobj 声明-实现分离模式

**目标**：使 `session.py` 成为纯声明文件，实现逻辑通过 `@mutobj.impl` 放在 `session_impl.py`。

## 2. 设计方案

### 2.1 session.py — 纯声明文件

**移除：**
- `_LEGACY_TYPE_MAP` (L100-106)
- `DEFAULT_SESSION_TYPE` (L109)
- 自由函数 `get_session_class()` (L112-121)

**新增 `Session.get_session_class()` 静态方法桩：**
```python
@staticmethod
def get_session_class(qualified_name: str) -> type[Session]:
    """通过全限定名查找 Session 子类，直接使用 mutobj 基础设施。"""
    ...
```

### 2.2 session_impl.py — 承接实现

**移除 imports：**
- `get_session_class`、`_LEGACY_TYPE_MAP`、`DEFAULT_SESSION_TYPE`

**新增 `@mutobj.impl`：**
```python
@mutobj.impl(Session.get_session_class)
def get_session_class(qualified_name: str) -> type[Session]:
    for cls in mutobj.discover_subclasses(Session):
        if f"{cls.__module__}.{cls.__qualname__}" == qualified_name:
            return cls
    raise ValueError(f"Unknown session type: {qualified_name!r}")
```

**修改 `_session_from_dict()`：**
- 移除 `_LEGACY_TYPE_MAP.get(raw_type, raw_type)` 映射
- 直接使用 `Session.get_session_class(raw_type)`

**修改 `SessionManager.create()`：**
- `session_type` 参数去掉默认值，改为必选
- 调用改为 `Session.get_session_class(session_type)`

### 2.3 调用方更新

**`routes.py` — `handle_session_create`：**
- 移除 `DEFAULT_SESSION_TYPE` 的 import
- 未指定 type 时：检查工作区是否为空，为空则默认 `"mutbot.builtins.guide.GuideSession"`，否则返回错误
- `get_session_class(...)` → `Session.get_session_class(...)`

**`routes.py` — `_session_kind()`：**
- 移除旧短名称回退逻辑（`if session_type in ("agent", "terminal", "document")`）

**`menus.py` — `AddSessionMenu.execute`：**
- 移除 `DEFAULT_SESSION_TYPE` 的 import
- `session_type` 从 params 获取，无值时返回错误（菜单总会传递 explicit type）
- `get_session_class(...)` → `Session.get_session_class(...)`

**`session_toolkit.py` — `create()`：**
- `from mutbot.session import get_session_class, AgentSession` → `from mutbot.session import Session, AgentSession`
- `get_session_class(...)` → `Session.get_session_class(...)`

### 2.4 测试更新

**`test_runtime_session.py`：**
- import 改为 `from mutbot.session import Session`
- `get_session_class(...)` → `Session.get_session_class(...)`
- 移除：`test_get_session_class_legacy_names`、`test_legacy_type_map_covers_builtins`
- 移除：`TestBackwardCompatibility` 整个测试类
- 移除：`test_create_agent_session_legacy_type`
- `sm.create("ws1")` 无类型调用 → `sm.create("ws1", session_type="mutbot.session.AgentSession")`

**`test_runtime_imports.py`：**
- `from mutbot.session import get_session_class` → `from mutbot.session import Session`
- 验证 `Session.get_session_class` 存在

## 3. 待定问题

（无）

## 4. 实施步骤清单

### 阶段一：核心重构 [待开始]

- [ ] **Task 1.1**: 重构 `session.py`
  - [ ] 移除 `_LEGACY_TYPE_MAP`、`DEFAULT_SESSION_TYPE`、自由函数 `get_session_class()`
  - [ ] 新增 `Session.get_session_class()` 静态方法桩
  - 状态：⏸️ 待开始

- [ ] **Task 1.2**: 更新 `session_impl.py`
  - [ ] 移除三个旧 import
  - [ ] 新增 `@mutobj.impl(Session.get_session_class)` 实现
  - [ ] 修改 `_session_from_dict()` 移除 legacy 映射
  - [ ] 修改 `SessionManager.create()` 的 `session_type` 为必选参数
  - 状态：⏸️ 待开始

### 阶段二：调用方更新 [待开始]

- [ ] **Task 2.1**: 更新 `routes.py`
  - [ ] `handle_session_create` 移除 DEFAULT_SESSION_TYPE，空工作区默认 GuideSession
  - [ ] `_session_kind()` 移除旧短名称回退
  - [ ] 所有 `get_session_class` → `Session.get_session_class`
  - 状态：⏸️ 待开始

- [ ] **Task 2.2**: 更新 `menus.py`
  - [ ] 移除 DEFAULT_SESSION_TYPE import 和使用
  - [ ] `get_session_class` → `Session.get_session_class`
  - 状态：⏸️ 待开始

- [ ] **Task 2.3**: 更新 `session_toolkit.py`
  - [ ] 更新 import 和调用
  - 状态：⏸️ 待开始

### 阶段三：测试更新与验证 [待开始]

- [ ] **Task 3.1**: 更新 `test_runtime_session.py`
  - [ ] 更新 imports 和调用
  - [ ] 移除旧兼容测试（legacy names、backward compat）
  - [ ] 修复无默认类型的 create 调用
  - 状态：⏸️ 待开始

- [ ] **Task 3.2**: 更新 `test_runtime_imports.py`
  - [ ] 更新 import 验证
  - 状态：⏸️ 待开始

- [ ] **Task 3.3**: 运行全量测试
  - [ ] `pytest tests/test_runtime_session.py tests/test_runtime_imports.py -v`
  - [ ] `pytest` 全量测试
  - 状态：⏸️ 待开始

## 5. 测试验证

### 单元测试
- [ ] `Session.get_session_class` 通过全限定名查找子类
- [ ] `Session.get_session_class` 未知类型抛出 ValueError
- [ ] `SessionManager.create` 必须指定 session_type
- [ ] `_session_from_dict` 正确反序列化

### 集成测试
- [ ] `pytest` 全量通过

## 6. 涉及文件

| 文件 | 操作 |
|------|------|
| `src/mutbot/session.py` | 移除遗留代码，新增静态方法桩 |
| `src/mutbot/runtime/session_impl.py` | 承接 @impl 实现，修改调用 |
| `src/mutbot/web/routes.py` | 更新调用方 |
| `src/mutbot/builtins/menus.py` | 更新调用方 |
| `src/mutbot/toolkits/session_toolkit.py` | 更新调用方 |
| `tests/test_runtime_session.py` | 移除旧兼容测试，更新调用 |
| `tests/test_runtime_imports.py` | 更新 import 验证 |