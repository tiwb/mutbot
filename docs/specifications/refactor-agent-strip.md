# mutbot Agent 功能剥离重构

**状态**：✅ 已完成
**日期**：2026-05-08
**类型**：重构

## 需求

1. mutagent 重构后，mutbot 中依赖 mutagent agent 层的功能全部不可用，导致 mutbot 无法启动
2. 需要从 mutbot 中剥离 Agent 相关 import 链，让 mutbot 只保留 terminal 功能 + pysandbox 能力，恢复启动能力
3. Agent 功能性代码暂时不删，保留代码结构供未来重新接入
4. 确定 mutbot 与未来 mutagent 的对接架构方向：MCP 只暴露 `pysandbox`，agent 侧未来通过 WS namespace mount 集成

## 关键参考

- `mutbot/src/mutbot/session.py` — Session/AgentSession/TerminalSession 声明（保留 Session + TerminalSession，删 AgentSession）
- `mutbot/src/mutbot/runtime/session_manager.py` — Session 生命周期管理（剥离 Agent 组装逻辑）
- `mutbot/src/mutbot/runtime/agent_bridge.py` — Agent 运行时桥接（纯 Agent，切 import）
- `mutbot/src/mutbot/web/server.py` — 启动入口（去掉 SandboxApp/agent builtins 初始化）
- `mutbot/src/mutbot/builtins/__init__.py` — import 链枢纽，切 agent 模块引入
- `mutbot/src/mutbot/builtins/debug_tools.py` — `mutbot.*` namespace 实现，pysandbox 内调用入口（保留不动）
- `mutbot/src/mutbot/__init__.py` — 顶层导出，去掉 AgentSession
- `mutbot/src/mutbot/ui/context.py` — UIContext 继承 `mutagent.Declaration`（= mutobj.Declaration），terminal Settings 依赖它
- `mutbot/src/mutbot/web/routes.py` — drain 流程中含 Agent session 停止逻辑
- `mutbot/src/mutbot/web/rpc_session.py` — session RPC 含 AgentSession 分支
- `mutbot/src/mutbot/runtime/config.py` — MutbotConfig 继承 mutagent.Config（保留不动）
- `D:/ai/CLAUDE.md` — "Sandbox 运行时内省" 一节，描述当前 `mutbot.*` namespace 14 个函数

## 设计方案

### 架构定位（核心决策）

mutbot 与 agent 客户端的对接区分两种透传方式：

| 通道 | 服务对象 | 协议 | 调用形态 |
|------|---------|------|---------|
| MCP | 非 pysandbox 客户端（Claude Code、其他 IDE） | MCP，只暴露 `pysandbox(code)` 一个 tool | `pysandbox(code="mutbot.status()")` |
| WS namespace mount | pysandbox 客户端（mutagent，未来其他 agent 框架） | 待定（独立规范） | `mutbot.status()` 直接像本地函数 |

**核心理由**：mutagent agent 本身在 pysandbox 里写 Python，如果走 MCP 调用 mutbot 的 `pysandbox`，会出现"pysandbox 套 pysandbox"——agent 代码退化为字符串拼接 `pysandbox(code="...")`，DX 和能力都受损。MCP 的 tool 抽象是给"不透明 RPC"设计的，pysandbox-to-pysandbox 必须在 sandbox 这一层直接打通。

**对本次重构的影响**：

- mutbot 的 MCP 实现保持现状（只暴露 `pysandbox`），不需要把 debug_tools 函数注册成多个 MCP tool
- `mutbot.*` namespace（debug_tools.py）作为唯一能力载体保留，未来 MCP 和 WS 共享同一套实现
- WS namespace mount 协议本次**不实现**，仅作为方向锁定，独立规范单独立项

### 切断范围

#### 直接切 import（代码保留不删）

| 模块 | 说明 |
|------|------|
| `mutbot/runtime/agent_bridge.py` | Agent 运行时桥接，不再被 builtins/__init__ import |
| `mutbot/copilot/` | Copilot provider，不再被引用 |
| `mutbot/proxy/` | LLM 代理路由，不再被引用 |
| `mutbot/claude_code/` | Claude Code 运行时，不再被引用 |
| `mutbot/toolkits/` | Agent Session 工具，不再被引用 |
| `mutbot/builtins/config_toolkit.py` | Agent LLM 配置向导 |
| `mutbot/builtins/pysandbox_toolkit.py` | Agent sandbox 工具 |
| `mutbot/builtins/web_jina_ext.py` | Jina 错误增强 |
| `mutbot/builtins/web_tools.py` | Web 搜索/抓取 namespace |
| `mutbot/builtins/http_client.py` | User-Agent 覆盖 |

#### 保留并继续工作的 pysandbox 能力

| 模块 | 说明 |
|------|------|
| `mutbot/builtins/debug_tools.py` | `mutbot.*` namespace（14 个函数），通过 MCP `pysandbox(code)` 入口对外服务，保持现状 |
| MCP endpoint | 继续暴露 `pysandbox` 单一 tool，外部通过 `mutbot.xxx()` 形态调用 |

#### 需要修改的文件（核心改动）

| 文件 | 改动 |
|------|------|
| `mutbot/builtins/__init__.py` | 去掉 `agent_bridge`、`config_toolkit`、`web_jina_ext`、`http_client` 的 import，保留 `menus` + `terminal` + `debug_tools` |
| `mutbot/__init__.py` | 去掉 `AgentSession` 导出（和 DocumentSession），只保留 `Session` + `TerminalSession` |
| `mutbot/session.py` | 删除 `AgentSession` 和 `DocumentSession` 类定义，保留 `Session` + `TerminalSession` + `SessionChannels` |
| `mutbot/runtime/session_manager.py` | 删除 `build_default_agent()`、`AgentSessionRuntime`、agent 相关 import；`get_runtime()` 只返回 Terminal 相关 |
| `mutbot/web/server.py` | 去掉 `SandboxApp`/`PySandboxTools` 中 agent 相关初始化段、agent builtins import 注册段；保留 pysandbox endpoint 和 `mutbot.*` namespace 注入 |
| `mutbot/web/rpc_session.py` | 去掉 `AgentSession` 分支和 `get_agent_runtime()` 调用 |
| `mutbot/web/routes.py` | drain 流程中去掉 "stopping agent sessions" 逻辑 |
| `mutbot/web/rpc_workspace.py` | 去掉 `LLMProvider` import |
| `mutbot/web/serializers.py` | 去掉 agent message 序列化函数 |
| `mutbot/builtins/menus.py` | "添加 Session" 菜单中去掉 Agent Session 选项 |
| `mutbot/ui/toolkit.py` | `UIToolkit` 继承 `mutagent.tools.Toolkit`，如果 terminal 不用则切 import |

### 不需要改的

- `mutbot/runtime/config.py` — MutbotConfig 继续继承 `mutagent.config.Config`
- `mutbot/ui/context.py` + `context_impl.py` — UIContext 继承 `mutagent.Declaration`（= mutobj.Declaration），terminal Settings 面板依赖
- `mutbot/runtime/terminal.py` — 完全不动
- `mutbot/web/supervisor.py` — 不涉及 agent
- `mutbot/builtins/debug_tools.py` — 完全不动（pysandbox 能力的核心载体）

### Agent 功能文件清单（保留，未来归位 mutagent）

| 文件 | 功能 | 未来去向 |
|------|------|---------|
| `debug_tools.py` | `mutbot.*` namespace 运行时调试（14 个函数） | **留在 mutbot**，作为 mutbot 自身的 pysandbox 能力，通过 MCP 或未来 WS namespace mount 对外 |
| `config_toolkit.py` | LLM 配置向导（~1000 行） | 回到 mutagent |
| `pysandbox_toolkit.py` | Agent sandbox 工具入口 | mutagent 已有，不再需要 |
| `web_tools.py` | web.search/fetch namespace | 回到 mutagent（agent 通用能力，非 mutbot 特有） |
| `web_jina_ext.py` | Jina 错误增强 | 回到 mutagent |
| `http_client.py` | User-Agent 覆盖 | 回到 mutagent 或留 mutbot |
| `copilot/` | GitHub Copilot Provider | 回到 mutagent |
| `proxy/` | LLM 代理路由 + 协议翻译 | 回到 mutagent |
| `claude_code/` | Claude Code 运行时适配 | 回到 mutagent |
| `toolkits/` | Session 工具 | 随 Agent 回 mutagent |
| `agent_bridge.py` | Agent 运行时桥接 | 随 Agent 回 mutagent |

### 决策记录

#### Agent Session 创建菜单去除
"添加 Session" 菜单去掉 AgentSession 选项，只保留 Terminal（mutbot 重构后没有 agent 能力，菜单项无意义）。未来 mutagent 通过 WS namespace mount 接入后，可调用 `mutbot.create_session(...)` 之类的 namespace 函数创建 session，不需要重新加菜单项。

#### exec_frontend 暂不跨进程
`mutbot.exec_frontend(code)` 当前实现走 worker → WebSocket → 浏览器 → 结果回传，**完全保留**。未来 mutagent 通过 WS namespace mount 调用时，由 namespace mount 协议天然支持双向 request/response，对 agent 调用方仍是 `mutbot.exec_frontend(code)`，无感知。

## 后续设计（独立立项，不在本次重构范围）

| 规范 | 内容 |
|------|------|
| `mutagent-pysandbox-namespace-mount.md`（待立项） | mutagent 侧的 remote pysandbox namespace mounter 客户端 + WS 协议设计（namespace 发现、`__getattr__` 代理、值序列化、双向调用、流式输出） |
| mutbot 的 WS namespace endpoint 实现（待立项） | mutbot 侧实现 namespace mount 协议的 WS endpoint，复用 `mutbot.*` namespace 现有实现 |

## 遗留问题

1. `debug_tools.py` 中的 `sessions()` / `session_inspect()` / `session_messages()` 含 AgentSession 相关分支。本次重构 AgentSession 类从 mutbot 移除后，这些分支会出现类型缺失。处理方式：本次只保证 import 不报错，函数运行时若遇到 agent session 类型分支，返回空或友好提示；彻底清理留到未来 namespace mount 规范激活时再处理。

2. `MutbotConfig` 的 `default_model` 字段——去掉 agent 后是否还需要？先保留在配置中，不影响启动。

## 实施步骤清单

### 切断 import 链
- [x] `mutbot/builtins/__init__.py` — 去掉 agent 模块 import，保留 menus/terminal/debug_tools
- [x] `mutbot/__init__.py` — 顶层导出去掉 AgentSession/DocumentSession
- [x] `mutbot/session.py` — 删除 AgentSession + DocumentSession 类定义，保留 Session/TerminalSession/SessionChannels

### 剥离 Session Manager 中的 Agent 组装
- [x] `mutbot/runtime/session_manager.py` — 删除 build_default_agent / AgentSessionRuntime / agent imports，get_runtime() 只返回 Terminal 相关

### Web 层清理
- [x] `mutbot/web/server.py` — 去掉 agent builtins 注册段，保留 pysandbox endpoint + mutbot.* namespace 注入；同时适配 mutagent SandboxApp 新 API（无参构造 + close() 替代 setup/shutdown，新增 mcp_sources/cli_sources 桥接段从 cli/pysandbox.py 模式移植）
- [x] `mutbot/web/rpc_session.py` — 去掉 AgentSession 分支和 get_agent_runtime() 调用（run_tool/run_setup 返回不可用错误，messages 改为只读磁盘）
- [x] `mutbot/web/routes.py` — drain 流程去掉 agent session 停止逻辑，保留持久化 + WS 关闭
- [x] `mutbot/web/rpc_workspace.py` — 去掉 LLMProvider import，ConfigOps.models() 返回空
- [x] `mutbot/web/serializers.py` — 全量重写：删除 mutagent.messages 依赖和所有 message/block/stream 序列化，只保留 workspace_dict/session_dict/terminal_dict/LANG_MAP
- [x] `mutbot/web/rpc_app.py` — workspace.create() 去掉默认 AgentSession 创建逻辑（原计划外，扫描时发现并补上）

### UI 与菜单
- [x] `mutbot/builtins/menus.py` — AddSessionMenu 是动态发现，AgentSession 类删除后菜单自动只剩 Terminal，无需代码修改
- [x] `mutbot/ui/toolkit.py` — mutagent.tools.Toolkit 仍可用，且 terminal/Settings 路径不走 toolkit，保留不动

### debug_tools 兼容性处理
- [x] `mutbot/builtins/debug_tools.py` — sessions/session_inspect 中去掉 AgentSession import，改为 getattr 探测；session_messages 返回不可用错误

### 启动验证
- [x] `python -c "import mutbot"` 通过，无 ImportError
- [x] `python -c "from mutbot import Session, TerminalSession"` 通过
- [x] `python -c "from mutbot.builtins import debug_tools"` 通过，MutbotTools 暴露 15 个 `mutbot.*` 函数
- [x] 模拟 worker 启动序列验证：WorkspaceManager/SessionManager/SandboxApp 初始化顺利，`help()` 返回 "mutbot — (15 functions)"，`mutbot.status()` 可调用（唯一错误是查询 supervisor /health 超时，及预期）
- [x] 历史 AgentSession 持久化数据兼容验证：14 个 session 成功加载，12 个 TerminalSession + 2 个旧 AgentSession 回退到 Session 基类，不报错
- [x] 持久化数据过滤：load_from_disk 跳过类型已不存在的 session（如 AgentSession），不加载到内存也不展示给前端
- [x] 由用户手动 `python -m mutbot` 启动，验证服务起得来 + terminal session 可创建 + MCP `pysandbox(code="mutbot.status()")` 可调

