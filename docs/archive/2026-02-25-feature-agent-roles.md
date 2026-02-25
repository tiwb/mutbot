# MutBot Agent 角色体系设计规范

**状态**：✅ 已完成
**日期**：2026-02-25
**类型**：功能设计
**来源**：known-issues P1（研究员 Agent）+ P2（默认 Agent 定位）

## 1. 背景

### 1.1 现状问题

mutbot 当前创建 Agent 时，使用的是 mutagent 的默认配置：
- 系统提示词面向 Python 代码自我修改场景（`session.py:270-276`）
- 工具集只有 `ModuleToolkit`（代码热替换）和 `LogToolkit`（日志查询）
- 没有区分不同角色的 Agent，所有 session 使用相同配置
- `create_agent()` 是一个万能工厂函数，通过 dict 参数控制行为

这不符合 mutbot 作为通用 Web 应用的定位。mutbot 需要定义自己的 Agent 角色体系，让不同场景使用合适的 Agent。

### 1.2 目标

定义两个初始 Agent 角色：

| 角色 | 定位 | 核心能力 |
|------|------|----------|
| **向导（Guide）** | 默认 Agent，用户首先接触的入口 | 引导使用、需求识别、创建专业 Agent Session |
| **研究员（Researcher）** | 信息检索与分析专家 | Web 搜索、网页读取、信息整理、建议（不直接执行） |

### 1.3 典型交互流程

```
用户在向导 Session 中："我想注册一个顶级的 .ai 域名"

向导（识别到研究类需求）
  → 调用 Session-create 工具，创建研究员 Session
  → 研究员 Session 出现在 UI 中，向导向其传达用户需求
  → 研究员：搜索 .ai 域名注册信息、价格对比、注册商推荐
  → 研究员：在自己的 Session 中展示调研结果

用户可以：
  a) 直接在研究员 Session 中继续追问细节
  b) 回到向导 Session，向导做总结或继续协调其他 Agent
  c) 如需执行操作，向导可创建新的专业 Agent Session
```

## 2. Agent 角色定义

### 2.1 向导（Guide Agent）

**职责**：
- 新 session 的默认 Agent，用户的第一个交互对象
- 介绍 mutbot 的功能和使用方式
- 识别用户的具体需求类型（研究、编码、文件操作等）
- 创建专业 Agent Session 并传达用户需求
- 在用户回到向导时可做跨 Session 的总结
- 能处理引导类对话（自我介绍、功能说明、简单问答），仅当自身无法帮助用户时才委托给专业 Agent

**委派机制**：与 mutagent 的 `AgentToolkit.delegate()` 不同，mutbot 的向导不在后台静默调用子 Agent，而是创建用户可见的新 AgentSession：
1. 向导调用 `Session-create` 工具 → SessionManager 创建对应类型的 Session
2. 新 Session 出现在 UI 面板中，用户可以看到
3. 向导向新 Session 发送初始消息（介绍用户需求）
4. 用户可直接在新 Session 中与专业 Agent 交流
5. 用户也可以回到向导 Session 继续对话

**UI 联动**：向导创建新 Session 后，在向导会话中通过链接方式展示新的 AgentSession，用户点击后可直接跳转到对应面板。

**系统提示词要点**：
- 你是 MutBot 的向导，帮助用户了解和使用 MutBot
- 当识别到具体需求时，为用户创建合适的专业 Agent Session
- 创建 Session 时要清晰地向专业 Agent 传达用户的需求
- 用户回来提问时，可以总结其他 Session 的进展

**工具集**：
- `Session-create` — 创建指定类型的 AgentSession

### 2.2 研究员（Researcher Agent）

**职责**：
- 通过 Web 搜索和网页内容读取来回答用户问题
- 整理分析检索到的信息
- 给出结论和下一步建议
- **不直接执行任何操作**（不修改文件、不运行命令）

**系统提示词要点**：
- 你是一个研究分析助手，擅长信息检索和分析
- 通过搜索和阅读网页内容来回答问题
- 给出有依据的分析结论和可行的下一步建议
- 你只负责研究和分析，不直接执行操作

**工具集**：
- `Web-search` — Web 搜索
- `Web-fetch` — 读取网页内容

## 3. 技术方案

### 3.1 Declaration 类提升为公开 API

**设计原则**：核心 Declaration 类是 mutbot 的公开 API，应放在 `mutbot` 顶层命名空间下，`runtime/` 只保留实现细节。这与 mutobj 声明-实现分离模式一致。

**现状**：`Session`、`Menu` 等核心 Declaration 类定义在 `mutbot/runtime/session.py` 和 `mutbot/runtime/menu.py` 中，与 `SessionManager`、`MenuRegistry` 等实现细节混在一起。

**目标结构**：

```
mutbot/src/mutbot/
├── __init__.py              # 重新导出公开 API
├── session.py               # Session 基类（公开 API）
│   └── Session, AgentSession, TerminalSession, DocumentSession, get_session_class()
├── menu.py                  # Menu 基类（公开 API）
│   └── Menu, MenuItem, MenuResult
├── builtins/
│   ├── __init__.py          # 导入所有内置模块，确保 Declaration 子类被注册
│   ├── guide.py             # GuideSession + 向导相关菜单（如有）
│   ├── researcher.py        # ResearcherSession + 研究员相关菜单（如有）
│   └── menus.py             # 通用内置 Menu 子类：AddSessionMenu, RenameSessionMenu, ...
├── runtime/
│   ├── session_impl.py      # SessionManager、SessionRuntime 等
│   ├── menu_impl.py         # MenuRegistry、辅助函数等
│   ├── storage.py
│   ├── terminal.py
│   └── workspace.py
├── toolkits/
│   └── session_toolkit.py   # SessionToolkit（向导的 Session-create 工具）
└── web/                     # Web 层
```

**设计说明**：

- `mutbot/session.py` 和 `mutbot/menu.py` 只放基类，是稳定的公开 API
- `mutbot/builtins/` 存放所有内置 Declaration 子类，按功能组织文件（不按类型区分 session/menu）
  - 当前按角色拆分为独立文件：`builtins/guide.py`、`builtins/researcher.py`
  - 每个文件可同时包含该角色的 Session 子类和相关 Menu 子类
  - 通用菜单（如 Tab 右键菜单）放在 `builtins/menus.py`
- `builtins/__init__.py` 负责导入所有内置模块，确保 mutobj 子类发现机制能找到它们

**拆分说明**：

| 文件 | 内容 | 性质 |
|------|------|------|
| `mutbot/session.py` | `Session`、`AgentSession`、`TerminalSession`、`DocumentSession` 基类，`get_session_class()` | 公开 API，稳定 |
| `mutbot/menu.py` | `Menu` 基类、`MenuItem`/`MenuResult` 数据类 | 公开 API，稳定 |
| `mutbot/builtins/guide.py` | `GuideSession` 及向导相关菜单 | 内置内容，可扩展 |
| `mutbot/builtins/researcher.py` | `ResearcherSession` 及研究员相关菜单 | 内置内容，可扩展 |
| `mutbot/builtins/menus.py` | `AddSessionMenu`、`RenameSessionMenu` 等通用内置菜单 | 内置内容，可扩展 |
| `mutbot/runtime/session_impl.py` | `SessionManager`、`SessionRuntime`、`AgentSessionRuntime` | 实现细节 |
| `mutbot/runtime/menu_impl.py` | `MenuRegistry`、`_get_attr_default`、`_menu_id`、`_item_to_dict`、`menu_registry` 全局实例 | 实现细节 |

`mutbot/__init__.py` 导出核心类型：
```python
from mutbot.session import Session, AgentSession
from mutbot.menu import Menu, MenuItem, MenuResult
```

**注意**：`AddSessionMenu.dynamic_items()` 当前依赖 `_get_type_default()` 读取 Session 子类的 type 字段。改用全限定名后，需要改为直接使用 `cls.__module__` + `cls.__qualname__` 生成菜单项 ID 和 session_type 参数。

### 3.2 Session.type 使用全限定名

**现状**：`Session.type` 是手动指定的短字符串（`"agent"`、`"terminal"`），需要每个子类手动声明，存在冲突风险。

**方案**：`type` 由类的 `__module__` + `__qualname__` 自动生成，不需要手动声明。

```python
# 之前：手动声明 type
class GuideSession(AgentSession):
    type: str = "guide"          # 手动声明，可能冲突

# 之后：type 自动生成
class GuideSession(AgentSession):
    pass                          # type 自动为 "mutbot.session.GuideSession"
```

**实现**：直接复用 `mutobj.discover_subclasses(Session)`，不再维护独立的类型注册表：

```python
def get_session_class(qualified_name: str) -> type[Session]:
    """通过全限定名查找 Session 子类，直接使用 mutobj 基础设施。"""
    for cls in mutobj.discover_subclasses(Session):
        if f"{cls.__module__}.{cls.__qualname__}" == qualified_name:
            return cls
    raise ValueError(f"Unknown session type: {qualified_name}")
```

现有的 `get_session_type_map()`、`_type_map_cache`、`_type_map_generation`、`_get_type_default()` 均可删除。`discover_subclasses` 内部已有 generation 机制，无需在外层重复缓存。

**注意**：Menu 系统已在使用全限定名（`_menu_id()` 返回 `f"{cls.__module__}.{cls.__qualname__}"`），Session 只是对齐同一模式。

**影响**：
- 持久化 JSON 中的 `type` 值从 `"agent"` 变为 `"mutbot.session.AgentSession"`
- 前端创建 Session 时传入全限定名
- 已有持久化数据需要迁移（可在加载时做兼容映射）

### 3.3 通过 AgentSession 子类定义角色

**方案**：每种 Agent 角色对应一个 `AgentSession` 子类，子类定义自己的 agent 组装逻辑。

```python
# mutbot/src/mutbot/builtins/guide.py

class GuideSession(AgentSession):
    """向导 Agent Session"""
    system_prompt: str = "你是 MutBot 的向导..."

    def create_agent(self, config: Config, **kwargs) -> Agent:
        """组装向导 Agent，配备 SessionToolkit"""
        ...
```

```python
# mutbot/src/mutbot/builtins/researcher.py

class ResearcherSession(AgentSession):
    """研究员 Agent Session"""
    system_prompt: str = "你是一个研究分析助手..."

    def create_agent(self, config: Config, **kwargs) -> Agent:
        """组装研究员 Agent，配备 WebToolkit"""
        ...
```

**关键变化**：
- 将当前 `create_agent()` 顶层函数重构为 `AgentSession.create_agent()` 方法
- 每个子类覆盖此方法，定义自己的系统提示词和工具集
- `SessionManager.start()` 调用 `session.create_agent()` 而非全局 `create_agent()`
- 现有 `AgentSession` 保留为兼容基类，其 `create_agent()` 提供当前默认行为

**Session 类型自动发现**：基于 `mutobj.discover_subclasses(Session)` 实现，新增的子类会被自动注册，无需手动配置。前端创建 Session 时可通过 API 获取可用的 session 类型列表。

### 3.4 工具命名规范

**现状**：~~当前工具名直接使用方法名。~~ 已实现统一前缀命名。

**规则**：工具名格式为 `{Prefix}-{method_name}`，前缀从类名自动推导：
- 类名以 `Toolkit` 结尾 → 去掉后缀（`WebToolkit` → `Web`）
- 类名不以 `Toolkit` 结尾 → 使用完整类名（`Greeter` → `Greeter`）

**命名结果**：

| Python 类名 | 前缀 | 示例工具名 |
|-------------|------|-----------|
| `WebToolkit` | `Web` | `Web-search`, `Web-fetch` |
| `SessionToolkit` | `Session` | `Session-create` |
| `ModuleToolkit` | `Module` | `Module-inspect`, `Module-view_source`, `Module-define`, `Module-save` |
| `LogToolkit` | `Log` | `Log-query` |
| `AgentToolkit` | `Agent` | `Agent-delegate` |

**API 兼容性**：Claude API 工具名允许 `[a-zA-Z0-9_-]`，连字符合法，无需额外映射层。

**实现**（`tool_set_impl.py`）：

```python
def _get_tool_prefix(cls: type) -> str:
    """从 Toolkit 类名生成工具前缀。去掉 Toolkit 后缀。"""
    name = cls.__name__
    if name.endswith("Toolkit") and name != "Toolkit":
        return name[:-7]
    return name

def _get_tool_name(cls: type, method_name: str) -> str:
    """生成工具名称，格式为 '{prefix}-{method_name}'。"""
    prefix = _get_tool_prefix(cls)
    return f"{prefix}-{method_name}"
```

**注意**：`add(source, methods=[...])` 的 `methods` 参数仍使用 Python 方法名过滤，注册后的工具名自动加前缀。

### 3.5 Web 工具（新增 Toolkit）

研究员 Agent 的核心工具。放在 mutagent 层（通用能力，不限于 mutbot）。

```python
# mutagent/src/mutagent/toolkits/web_toolkit.py

class WebToolkit(Toolkit):
    """Web 信息检索工具集。"""

    def search(self, query: str, max_results: int = 5) -> str:
        """搜索 Web 并返回结果摘要。

        Args:
            query: 搜索关键词。
            max_results: 最大返回结果数。

        Returns:
            搜索结果列表，包含标题、URL、摘要。
        """
        ...

    def fetch(self, url: str) -> str:
        """读取网页内容并返回文本。

        Args:
            url: 要读取的网页 URL。

        Returns:
            网页的主要文本内容（Markdown 格式）。
        """
        ...
```

注册后，工具名为 `Web-search` 和 `Web-fetch`。

**搜索 API**：使用 Jina AI Search API（`s.jina.ai`），返回结构化搜索结果。

**网页读取**：使用 Jina AI Reader（`r.jina.ai/URL`），返回 Markdown 格式。内容超过一定长度时截取主要内容。

**API Key 配置**：Jina AI 无 key 时有免费额度，提供 API key 后额度更高。配置通过 mutagent 的 Config 系统统一管理，以 Toolkit 类名作为配置 section：

```json
// .mutagent/config.json
{
  "WebToolkit": {
    "jina_api_key": "jina_xxxx"
  }
}
```

**配置命名约定**：Toolkit 的配置统一以类名为 section key，如 `WebToolkit.jina_api_key`、`ModuleToolkit.xxx`。这样看到配置路径就能直接定位到对应的 Python 类。

WebToolkit 初始化时从 Config 读取：
```python
class WebToolkit(Toolkit):
    config: Config

    # self.config.get("WebToolkit.jina_api_key")
```

未来可给 Config 系统增加环境变量引用能力（如 `"jina_api_key": "$JINA_API_KEY"`），当前阶段直接在 config.json 中写值即可。

### 3.6 Session 工具（新增 Toolkit）

向导 Agent 用于创建专业 Agent Session 的工具。放在 mutbot 层（这是 mutbot 特有的能力）。

```python
# mutbot/src/mutbot/toolkits/session_toolkit.py

class SessionToolkit(Toolkit):
    """Session 管理工具集。"""

    session_manager: SessionManager

    def create(self, session_type: str, initial_message: str) -> str:
        """创建一个新的专业 Agent Session 并发送初始消息。

        Args:
            session_type: Session 类型的全限定名（如 "mutbot.session.ResearcherSession"）。
            initial_message: 向新 Session 的 Agent 传达的初始需求描述。

        Returns:
            新 Session 的 ID 和 Agent 的初始响应摘要。
        """
        ...
```

注册后，工具名为 `Session-create`。

**实现要点**：
- 调用 `SessionManager.create()` 创建对应类型的 Session
- 调用 `SessionManager.start()` 启动 Agent
- 向 Agent 发送 `initial_message` 作为第一条用户消息
- 通过 WebSocket 广播通知前端新 Session 已创建
- 返回 Session ID 和 Agent 初始响应的摘要
- 向导会话中以链接形式展示新 Session，用户点击跳转

### 3.7 create_agent 重构

将全局 `create_agent()` 重构为 `AgentSession` 的方法：

```python
class AgentSession(Session):
    """Agent 对话 Session"""
    model: str = ""
    system_prompt: str = ""

    def create_agent(
        self,
        config: Config,
        log_dir: Path | None = None,
        session_ts: str = "",
        messages: list[Message] | None = None,
    ) -> Agent:
        """组装并返回 Agent 实例。子类覆盖此方法以定制工具集和提示词。"""
        # 默认实现：保持当前行为（ModuleToolkit + LogToolkit）
        ...
```

`SessionManager.start()` 的修改：
```python
# 之前
agent = create_agent(log_dir=..., session_ts=..., messages=...)

# 之后
agent = session.create_agent(config=self._config, log_dir=..., session_ts=..., messages=...)
```

## 4. 实施步骤清单

### 阶段一：Declaration 类重组 + Session.type 改造 [✅ 已完成]

- [x] **Task 1.1**: 将 Session 基类从 `runtime/session.py` 提升到 `mutbot/session.py`
  - [x] 新建 `mutbot/src/mutbot/session.py`，迁移 Session、AgentSession、TerminalSession、DocumentSession 基类
  - [x] 新建 `get_session_class(qualified_name)` 直接基于 `mutobj.discover_subclasses()`
  - [x] `runtime/session.py` 保留为向后兼容 shim，新建 `runtime/session_impl.py` 保留 SessionManager、SessionRuntime 等实现细节
  - [x] 修复所有导入路径
  - 状态：✅ 已完成

- [x] **Task 1.2**: 将 Menu 基类从 `runtime/menu.py` 拆分，建立 `builtins/` 包
  - [x] 新建 `mutbot/src/mutbot/menu.py`，迁移 `Menu`、`MenuItem`、`MenuResult`
  - [x] `runtime/menu.py` 保留为向后兼容 shim，新建 `runtime/menu_impl.py` 保留 `MenuRegistry`、辅助函数、`menu_registry` 全局实例
  - [x] 新建 `mutbot/src/mutbot/builtins/__init__.py`，负责导入所有内置模块
  - [x] 新建 `mutbot/src/mutbot/builtins/menus.py`，迁移 `AddSessionMenu`、`RenameSessionMenu` 等内置菜单子类
  - [x] 更新 `mutbot/__init__.py` 导出核心基类
  - [x] 修复所有导入路径
  - 状态：✅ 已完成

- [x] **Task 1.3**: Session.type 改用全限定名
  - [x] 删除 `get_session_type_map()`、`_type_map_cache`、`_type_map_generation`（旧 API 保留为弃用 shim）
  - [x] 移除各子类手动声明的 `type: str = "xxx"` 字段，通过 `Session.__init__` 自动生成
  - [x] 更新 `AddSessionMenu.dynamic_items()` 使用全限定名生成菜单项
  - [x] 持久化兼容：`_LEGACY_TYPE_MAP` 支持旧短名称到新全限定名的映射
  - [x] 后端 API 适配：`_session_dict()` 新增 `kind` 字段供前端 switch/display 使用
  - [x] `routes.py` 中 `session.create` handler 改用 `issubclass()` 判断 Session 类型
  - 状态：✅ 已完成

### 阶段二：工具命名规范 [✅ 已完成]

- [x] **Task 2.1**: 修改 `tool_set_impl.py` 中工具注册逻辑，统一 `{前缀}-{方法名}` 命名
  - [x] 新增 `_get_tool_prefix()` 函数（从类名自动推导前缀，去掉 Toolkit 后缀）
  - [x] 新增 `_get_tool_name()` 函数（生成 `{prefix}-{method}` 格式）
  - [x] 修改 `_make_entries_for_toolkit()` 的 name 生成规则
  - [x] 修改 `add()` 方法中对象实例注册的 name 生成规则
  - [x] 更新 `_refresh_discovered()` 冲突检测使用工具名而非方法名
  - [x] 确保 dispatch 时能正确匹配新命名的工具
  - 状态：✅ 已完成

- [x] **Task 2.2**: 所有工具统一使用前缀命名，更新全部测试
  - [x] 所有 Toolkit 统一使用前缀命名（无 opt-in/opt-out）
  - [x] 更新 9 个测试文件中的工具名引用（test_tool_set, test_agent, test_claude_impl, test_e2e, test_messages, test_schema, test_ansi, test_rich_extras, test_userio, test_log_query）
  - [x] 新增 12 个命名规范专项测试（TestToolNamingConvention + TestToolNamingAutoDiscover）
  - [x] 简化冗余方法名：`inspect_module`→`inspect`、`define_module`→`define`、`save_module`→`save`、`query_logs`→`query`（`view_source` 保留）
  - [x] 更新 4 个声明文件 + 4 个实现文件 + 系统提示词 + 12 个测试文件
  - [x] 运行 mutagent 全量测试确认通过（623 passed, 2 skipped）
  - 状态：✅ 已完成

### 阶段三：WebToolkit [✅ 已完成]

- [x] **Task 3.1**: 创建 WebToolkit 声明和实现
  - [x] `mutagent/src/mutagent/toolkits/web_toolkit.py`（声明）：`WebToolkit(Toolkit)` 含 `search()` 和 `fetch()` 方法
  - [x] `mutagent/src/mutagent/builtins/web_impl_jina.py`（实现）：使用 Jina AI Search/Reader API
  - [x] 工具名 `Web-search`、`Web-fetch` 符合命名规范
  - [x] API key 通过 `config.get("WebToolkit.jina_api_key")` 读取
  - 状态：✅ 已完成

- [x] **Task 3.2**: 测试 Jina AI API 可用性
  - [x] 测试 Jina AI Search API 的可用性和返回质量（JSON 格式，含 title/url/description/content）
  - [x] 测试 Jina AI Reader 的网页转换质量（JSON 格式，含 title/content）
  - [x] 确定内容截断策略：50000 字符截断，附加 `[内容已截断]` 标记
  - [x] 搜索 API 需要 API key，Reader API 无 key 时有免费额度
  - 状态：✅ 已完成

- [x] **Task 3.3**: 编写 WebToolkit 测试
  - [x] `mutagent/tests/test_web_toolkit.py`：29 个测试用例
  - [x] 覆盖：声明验证、工具注册、Schema 生成、配置读取、搜索/读取实现（mock）、错误处理
  - [x] 全量测试通过（652 passed, 2 skipped）
  - 状态：✅ 已完成

### 阶段四：AgentSession 子类体系 [✅ 已完成]

- [x] **Task 4.1**: 重构 `create_agent()` 为 `AgentSession.create_agent()` 方法
  - [x] 将 `create_agent()` 逻辑拆分为 `setup_environment()`、`create_llm_client()`、`build_default_agent()` 三个辅助函数
  - [x] `AgentSession.create_agent()` 方法委托给 `build_default_agent()`，子类可覆盖
  - [x] `SessionManager` 新增 `_get_config()` 懒加载 Config
  - [x] `SessionManager.start()` 改为调用 `session.create_agent(config=..., session_manager=self)`
  - [x] 原 `create_agent()` 全局函数保留为弃用兼容 shim
  - 状态：✅ 已完成

- [x] **Task 4.2**: 实现 `ResearcherSession`（`builtins/researcher.py`）
  - [x] 定义研究员系统提示词（信息检索与分析，不直接执行操作）
  - [x] 覆盖 `create_agent()` 方法，配备 WebToolkit（`Web-search`、`Web-fetch`）
  - [x] 通过 mutobj 子类发现机制自动注册
  - 状态：✅ 已完成

- [x] **Task 4.3**: 实现 `GuideSession`（`builtins/guide.py`）
  - [x] 定义向导系统提示词（引导使用、需求识别、创建专业 Agent Session）
  - [x] 实现 `SessionToolkit`（`mutbot/toolkits/session_toolkit.py`），工具名 `Session-create`
  - [x] 覆盖 `create_agent()` 方法，配备 SessionToolkit
  - [x] 设置 `DEFAULT_SESSION_TYPE = "mutbot.builtins.guide.GuideSession"`
  - [x] 更新 `SessionManager.create()`、`routes.py`、`menus.py` 的默认类型
  - [x] 更新 `_KIND_MAP`、`_SESSION_DISPLAY`、`_LEGACY_TYPE_MAP`
  - [x] 全量测试通过：mutagent 652 passed + mutbot 187 passed
  - 状态：✅ 已完成

### 阶段五：前端适配 [✅ 已完成]

- [x] **Task 5.1**: 前端使用 kind 字段进行面板路由
  - [x] `App.tsx`：`addTabForSession` 和 `handleSelectSession` 改用 `session.kind` 代替 `session.type`
  - [x] `SessionListPanel.tsx`：图标和显示逻辑改用 `session.kind`，新增 Guide 和 Researcher 图标
  - [x] `PanelFactory.tsx`：sessions 类型定义添加 `kind` 字段
  - 状态：✅ 已完成

- [x] **Task 5.2**: 添加 `session.types` RPC 端点
  - [x] `routes.py` 新增 `session.types` handler，返回可用 session 类型列表（含 kind、label、description）
  - [x] `_session_type_display()` 辅助函数生成类型展示信息
  - 状态：✅ 已完成

- [x] **Task 5.3**: SessionManager 广播支持
  - [x] `SessionManager` 新增 `set_broadcast(loop, broadcast_fn)` 和 `_maybe_broadcast_created()`
  - [x] Agent 线程中创建 Session 时，通过 `loop.call_soon_threadsafe()` 安全广播 `session_created` 事件
  - [x] Workspace WebSocket handler 中调用 `set_broadcast()` 注册广播回调
  - 状态：✅ 已完成

- [x] **Task 5.4**: Session 链接支持
  - [x] `Markdown.tsx`：拦截 `mutbot://session/{id}` 链接，调用 `onSessionLink` 回调
  - [x] `MessageList.tsx`：新增 `onSessionLink` prop 并传递给 Markdown 组件
  - [x] `AgentPanel.tsx`：新增 `onSessionLink` prop，传递给 MessageList
  - [x] `PanelFactory.tsx`：将 `ctx.onSelectSession` 作为 `onSessionLink` 传递给 AgentPanel
  - [x] `SessionToolkit.create()`：返回值包含 `mutbot://session/{id}` 格式的 markdown 链接
  - [x] `index.css`：新增 `.session-link` 样式（虚线下划线，hover 变实线）
  - [x] 全量测试通过：mutagent 652 passed + mutbot 187 passed + TypeScript 编译通过
  - 状态：✅ 已完成
