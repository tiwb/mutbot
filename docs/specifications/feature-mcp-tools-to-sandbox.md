# MCP 内省工具迁移至 Sandbox Namespace 设计规范

**状态**：✅ 已完成
**日期**：2026-04-23
**类型**：重构

## 需求

1. mutbot MCP endpoint 当前暴露 15 个 tool（server/workspace/session/connection/log/config/exec/browser 八个 ToolSet），占用 agent 上下文大，每次对话加载全部 schema
2. 希望 MCP endpoint 只保留 `pysandbox` 一个 tool,其余调试能力通过 sandbox namespace 调用(按需发现,不预加载 schema)
3. 迁移必须契合"sandbox 由文档驱动使用"的设计 —— 函数 docstring 必须完整保留,作为 agent 调用依据
4. 本次只迁移 MCP → sandbox,不处理 CLI 双轨问题(后续单独 SDD)

## 关键参考

- `mutbot/src/mutbot/web/mcp.py` — 当前 MCP ToolSet 全部实现(601 行,包含 8 个 MCPToolSet 子类)
- `mutbot/src/mutbot/builtins/web_tools.py` — `NamespaceTools` 现成用法示例(WebTools → sandbox `web.*`)
- `mutagent/src/mutagent/sandbox/namespace.py` — `NamespaceTools` 基类定义
- `mutagent/src/mutagent/sandbox/_app_impl.py:72-126` — `_build_declaration_namespaces` 发现机制 + async 自动包装
- `mutio/src/mutio/mcp/toolset.py` — `MCPToolSet` 基类,用于对比
- `mutbot/src/mutbot/web/server.py:184-189` — SandboxApp 启动、NamespaceTools 触发 import 位置
- `mutbot/src/mutbot/builtins/pysandbox_toolkit.py` — 保留的 agent 工具(不变)
- `D:/ai/CLAUDE.md` — 需要更新的"MCP 运行时内省"章节

## 设计方案

### 机制选择

`NamespaceTools` 与 `MCPToolSet` 机制几乎同构:

- 都是 `Declaration` 子类,靠 `discover_subclasses` 零注册发现
- 都以类方法为能力单元,自动生成函数/工具元数据
- `NamespaceTools` 支持 async 方法(`_app_impl.py:117` `_wrap_async`),迁移时函数体一字不改

迁移本质 = **换基类 + 改归属属性**。不涉及算法或数据流重构。

### 类合并策略

当前 8 个 `MCPToolSet` 子类按"功能面"切分(server/workspace/session/connection/log/config/exec/browser)。迁移到 sandbox 时合并为**单个** `MutbotTools(NamespaceTools)`,`_namespace = "mutbot"`,所有方法平铺。

合并到一个 namespace 的理由:
- 目标都是"mutbot 运行时调试",逻辑上是同一组能力
- 四个 `exec_*` 函数(worker/ptyhost/supervisor/frontend)按维度属于同类,拆出去反而打断对称性
- `help(mutbot)` 一次列全所有 debug 能力,agent 查找成本低于遍历多个子 namespace

### 函数命名

sandbox `Namespace` 目前只支持单层扁平容器(见 `mutagent/src/mutagent/sandbox/_namespace.py`),本次不做多级 namespace 基础设施改动。所有 `MutbotTools` 方法平铺挂在 `mutbot.*` 下,通过**前缀分组**获得可读性 —— `help(mutbot)` 按字母序输出时,同前缀函数自然聚集。

四个 `exec_*` 函数按**目标端**命名(而非按语言),形成对称结构:worker/ptyhost/supervisor 是 Python 进程(默认语言 Python),frontend 是浏览器(默认语言 JavaScript)。语言由目标端唯一决定,不标在函数名里。

原 `list_*` / `get_*` / `inspect_*` 等动词前缀按名词主语重组,减少冗余动词:

| 迁移前(MCP tool) | 迁移后(sandbox 函数) |
|---|---|
| `server_status` | `mutbot.status()` |
| `list_workspaces` | `mutbot.workspaces()` |
| `list_sessions(workspace_id)` | `mutbot.sessions(workspace_id)` |
| `inspect_session(session_id)` | `mutbot.session_inspect(session_id)` |
| `get_session_messages(...)` | `mutbot.session_messages(...)` |
| `list_connections` | `mutbot.connections()` |
| `query_logs(level, logger, pattern, last_n)` | `mutbot.logs(level, logger, pattern, last_n)` |
| `get_errors(last_n)` | `mutbot.errors(last_n)` |
| `get_config(key)` | `mutbot.config_get(key)` |
| `set_config(key, value)` | `mutbot.config_set(key, value)` |
| `restart_server` | `mutbot.restart()` |
| `exec_python(code, target="worker")` | `mutbot.exec_worker(code)` |
| `exec_python(code, target="ptyhost")` | `mutbot.exec_ptyhost(code)` |
| `exec_python(code, target="supervisor")` | `mutbot.exec_supervisor(code)` |
| `exec_js(code, client_id)` | `mutbot.exec_frontend(code, client_id)` |

命名原则:
- 去掉 `list_` / `get_` 动词前缀:`mutbot.sessions()` 比 `mutbot.list_sessions()` 更贴近 Pythonic 属性式查询
- `config_get` / `config_set` 保留前缀:区分读写,且让 `help(mutbot)` 里 config 相关聚集
- `session_*` / `exec_*` 前缀让同主语操作聚集
- 单名词不加前缀:`status` / `workspaces` / `sessions` / `connections` / `logs` / `errors` / `restart`

### 文件组织

- 新建 `mutbot/src/mutbot/builtins/debug_tools.py` — 放 `MutbotTools`,复用 `mcp.py` 现有所有 helper 函数(`_get_managers` / `_mask_secrets` / `_format_log_entries` / `_int` / `_bool` / `_safe_eval` / `_AsyncResult` / `_eval_js_pending` / `_CONFIG_WHITELIST` / `_start_time`)
- 精简 `mutbot/src/mutbot/web/mcp.py` — 只保留 `MutBotMCP(MCPView)` 类;删除 8 个迁走的 ToolSet 类
- 在 `mutbot/src/mutbot/web/server.py` 的 NamespaceTools 触发 import 位置(当前 `import mutbot.builtins.web_tools`)旁,新增 `import mutbot.builtins.debug_tools`

### docstring 规范

sandbox 由文档驱动使用。每个迁移过去的方法必须保留原 MCPToolSet 的 docstring,且满足:

1. **首行**:一句话描述能力(与原 MCP tool description 等价)
2. **参数段**:若方法含参数,列出每个参数的类型、默认值、作用
3. **返回**:返回值格式(文本格式/JSON结构/错误形式)
4. **示例**(可选):典型调用片段

现有 `mcp.py` 的 docstring 已接近此格式,迁移时做一次梳理补全而非重写。

### `MutBotMCP.instructions` 更新

原 instructions:`"MutBot 运行时内省工具。查看服务器状态、session、workspace、连接、日志、配置。"`
新 instructions:`"MutBot Python 沙箱 — 通过 pysandbox(code) 调用。调试能力见 help(mutbot)。"`
引导 agent 进入 sandbox 后自行发现能力,而非在 MCP 层列举。

### `get_session_messages` 返回格式

原 `mcp.py:264` 返回拼接字符串(`--- [role] ---` 分隔)。sandbox 调用者可能希望得到结构化数据便于后处理。本次**保持原字符串返回**,不改格式 —— 迁移范围控制,格式优化留到后续再议。

### 破坏性变更

- MCP endpoint 上 `server_status` / `list_workspaces` / `list_sessions` / `inspect_session` / `get_session_messages` / `list_connections` / `query_logs` / `get_errors` / `get_config` / `set_config` / `restart_server` / `exec_python` / `exec_js` 共 13 个 tool **删除**
- 原调用方(`curl ...tools/call` + tool name) → 新调用方(`pysandbox("mutbot.xxx(...)")` 或 `pysandbox("browser.exec_js(...)")`)
- Claude Code 里内建的 `mcp__mutbot__*` 系列 tool 名会消失,只剩 `mcp__mutbot__pysandbox`

因 mutbot 的 MCP 内省工具主要给本工作区 agent 调试用,无外部生产消费者,破坏性变更可接受,不保留兼容层。

### 配置依赖

`MutbotTools` 所有方法对全局状态的依赖方式:

- **绝大多数方法**:通过 `_get_managers()` 延迟 import `mutbot.web.server`,访问 `workspace_manager` / `session_manager` / `log_store` / `channel_manager` / `config` / `terminal_manager` 等模块级全局。迁移后路径不变,依然 `from mutbot.web import server as _srv`。
- **`exec_frontend`**:通过 `mutbot.web.routes._clients` 访问活跃连接,通过模块级 `_eval_js_pending` dict + RPC `eval_result` 回调 resolve。RPC 回调路径(routes.py 中处理 `eval_result` 消息的逻辑)需同步更新为 import `mutbot.builtins.debug_tools._eval_js_pending`。

### 测试验证

手工端到端验证清单(不写自动化测试,迁移属纯搬运):

1. `pysandbox("mutbot.status()")` 返回与原 `mcp__mutbot__server_status` 一致
2. `pysandbox("mutbot.logs(level='ERROR', last_n=5)")` 返回日志
3. `pysandbox("mutbot.exec_worker('srv.session_manager._sessions')")` 能看到活 session dict
4. `pysandbox("mutbot.exec_frontend('location.href')")` 前端 URL 正常返回
5. `pysandbox("help(mutbot)")` 能列出所有 14 个函数
6. Claude Code 重连 MCP 后,`mcp__mutbot__*` tool 列表只剩 `pysandbox`

## 消费者场景

| 消费者 | 场景 | 依赖的输出 | 验收标准 |
|---|---|---|---|
| Claude Code agent(本工作区日常调试) | 查 server 状态、日志、session | `mutbot.*` 函数返回文本 | 原 MCP tool 所有调用能 1:1 映射到 sandbox 调用,返回内容一致 |
| Claude Code agent(前端调试) | 在 mutbot 前端跑 JS | `mutbot.exec_frontend` | 与原 `mcp__mutbot__exec_js` 返回一致 |
| CLAUDE.md 文档读者 | 学会如何调试 mutbot | 更新后的"MCP 运行时内省"小节 | 章节改名为"Sandbox 运行时内省",列出所有 `mutbot.*` 函数及调用方式,不再出现 `curl tools/call` |

## 待定问题

（无，所有关键决策已落入「设计方案」章节）

## 实施步骤清单
- [x] 新建 `mutbot/src/mutbot/builtins/debug_tools.py`,定义 `MutbotTools(NamespaceTools)` 类,`_namespace = "mutbot"`,包含 15 个方法
- [x] 将 `mcp.py` 的 helper 函数(`_get_managers` / `_mask_secrets` / `_format_log_entries` / `_int` / `_bool` / `_safe_eval` / `_AsyncResult` / `_start_time` / `_CONFIG_WHITELIST`)随方法一并搬到 `debug_tools.py`
- [x] 将 `_eval_js_pending` dict 搬到 `debug_tools.py`,修改 `mutbot/src/mutbot/web/routes.py` 中 `eval_result` 回调路径的 import 源
- [x] 方法签名按新命名落地:`status` / `workspaces` / `sessions` / `session_inspect` / `session_messages` / `connections` / `logs` / `errors` / `config_get` / `config_set` / `restart` / `exec_worker` / `exec_ptyhost` / `exec_supervisor` / `exec_frontend`
- [x] 每个方法 docstring 整理:首行一句描述、参数说明、返回格式
- [x] 在 `mutbot/src/mutbot/web/server.py` NamespaceTools 触发 import 处新增 `import mutbot.builtins.debug_tools`
- [x] 精简 `mutbot/src/mutbot/web/mcp.py`:只保留 `MutBotMCP(MCPView)`,更新 `instructions` 为新文案;删除 ServerTools/WorkspaceTools/SessionTools/ConnectionTools/LogTools/ConfigTools/ExecTools/BrowserTools 八个类
- [x] 更新 `D:/ai/CLAUDE.md` 的"MCP 运行时内省"小节 —— 改为通过 `pysandbox("mutbot.xxx(...)")` 调用,更新工具清单与调用示例
- [x] 手工端到端验证:按「测试验证」6 项清单逐一过,确认行为一致

