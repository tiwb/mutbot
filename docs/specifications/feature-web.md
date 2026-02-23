# MutBot Web UI 设计规范

**状态**：🔄 进行中（阶段一、二已完成，阶段三待开始）
**日期**：2026-02-23
**类型**：功能设计

## 1. 背景

### 1.1 MutBot 定位

**mutbot** 是基于 mutagent 的 Web 应用，提供完整的用户交互界面和会话管理。两个仓库的职责划分：

| 仓库 | 职责 | 定位 |
|------|------|------|
| **mutagent** | Agent 核心框架：Agent 循环、LLM 通信、Toolkit 系统、UserIO 接口、运行时热替换 | 引擎 |
| **mutbot** | 用户界面与扩展：Web UI、Workspace/Session 管理、终端集成、文件编辑，未来还包括各种工作流和扩展 | 应用 |

依赖方向：`mutbot → mutagent`（mutbot 依赖 mutagent，反向无依赖）。

### 1.2 核心功能

- **Workspace 工作区**：以项目为单位组织面板布局、Session、终端
- **Agent Session 管理**：创建、持久化、恢复 Agent 会话
- **多 Agent 对话**：同时运行多个 Agent，独立面板
- **终端集成**：嵌入式终端面板
- **文件编辑**：Monaco Editor 代码查看/编辑/diff
- **多用户协作**：多客户端同步，所有用户均为操作者
- **可演化的内容块**：Agent 可在运行时定义新的块类型和渲染方式

### 1.3 启动方式

mutbot 是一个标准的 Web 应用，默认启动即为 Web 服务器：

```
python -m mutbot                → 启动 Web 服务器（默认模式）
python -m mutbot --port 8741    → 指定端口
python -m mutbot --host 0.0.0.0 → 远程访问模式
```

与 mutagent 终端模式的关系：

```
快速终端交互：python -m mutagent   → mutagent Rich 增强终端（已实现）
完整工作区：  python -m mutbot     → mutbot Web UI（本规范）
```

两者独立运行，互不依赖。mutagent 终端模式面向快速交互，mutbot Web 面向完整工作区体验。

## 2. 技术选型

### 2.1 后端：FastAPI + uvicorn

- WebSocket 一等支持
- Pydantic 模型可与 mutagent 数据模型共享
- 依赖轻量，无外部服务

#### Sync/Async 兼容方案

mutagent 的 Agent 循环是**同步阻塞**架构：`Agent.run()` 返回 `Iterator[StreamEvent]`，内部阻塞在 `requests` HTTP 调用和 `input_stream` 迭代上。FastAPI 是 asyncio 架构。

桥接策略：**Agent 运行在工作线程，通过队列与 async 事件循环通信**。

```
WebSocket handler (async, 主事件循环)
    │
    ├── 用户输入 → thread-safe Queue → Agent 线程的 input_stream 消费
    │
    └── Agent 线程产出 StreamEvent → asyncio Queue → WebSocket 广播

Agent.run() (sync, 工作线程 via asyncio.to_thread)
    │
    ├── input_stream: 从 thread-safe Queue 读取（阻塞等待）
    ├── client.send_message(): 同步 HTTP（在工作线程中不阻塞事件循环）
    └── yield StreamEvent → 桥接层转发到 asyncio Queue
```

关键实现点：
- `asyncio.to_thread(agent_runner)` 将 Agent 循环放入线程池
- `queue.Queue`（标准库，线程安全）：WebSocket → Agent 方向
- `asyncio.Queue` + `loop.call_soon_threadsafe()`：Agent → WebSocket 方向
- 每个 Session 一个工作线程，线程生命周期与 Session 绑定

这是成熟的 sync-in-async 模式，无需修改 mutagent 代码。

### 2.2 前端框架：React 19

经 React 19 与 Svelte 5 详细对比，选择 React 19。

| 因素 | React 19 | Svelte 5 | 影响 |
|------|----------|----------|------|
| **面板布局** | flexlayout-react（JSON 模型 + 程序化 API） | 无成熟库，需自建 ~1,000+ 行 | **决定性** |
| **语音助手操控面板** | `model.doAction()` 直接调用 | 需自建 API | **决定性** |
| Monaco 集成 | @monaco-editor/react（开箱即用） | 手动封装 | 中 |
| Markdown 渲染 | react-markdown（13K stars） | svelte-markdown（300 stars） | 中 |
| 生态规模 | 25M 周下载量 | 700K 周下载量 | 高 |
| Bundle 体积 | ~42 KB runtime | ~2-5 KB runtime | 低 |

### 2.3 前端核心库

| 技术 | 用途 |
|------|------|
| **flexlayout-react** | 面板布局（tabs + splits + drag + 程序化控制） |
| **xterm.js v5** | 终端嵌入（WebGL 渲染） |
| **Monaco Editor** | 代码查看/编辑/diff（懒加载） |
| **react-markdown + Shiki** | Markdown + 代码高亮 |
| **@tanstack/virtual** | 虚拟滚动 |

### 2.4 通信：WebSocket

双向通信（实时输入、交互响应、取消）+ 终端面板二进制帧。统一使用 WebSocket。

## 3. 架构设计

### 3.1 整体架构

```
Browser (React SPA)
│
├── WebSocket /ws/session/{session_id}    ← Agent 事件流
├── WebSocket /ws/terminal/{term_id}      ← 终端 I/O（二进制）
├── HTTP      /api/workspaces/*           ← 工作区 CRUD
├── HTTP      /api/sessions/*             ← 会话 CRUD + 历史恢复
├── HTTP      /api/auth/login             ← 认证
└── HTTP GET  /                            ← 静态前端资源

            ↕ WebSocket / HTTP

mutbot Server (FastAPI)
│
├── mutbot.web.server         — FastAPI 应用、路由
├── mutbot.web.connection     — WebSocket 连接池、广播
├── mutbot.web.agent_bridge   — WebUserIO（input_stream + present）、sync/async 桥接
├── mutbot.web.terminal       — PTY 管理 + WebSocket 桥接
├── mutbot.web.auth           — 认证
├── mutbot.web.serializers    — StreamEvent → JSON 序列化
├── mutbot.workspace          — Workspace 工作区管理
└── mutbot.session            — Session 生命周期、Agent 组装、持久化

            ↕ Python API

mutagent (Agent 核心框架)
├── Agent / LLMClient / UserIO / Toolkit / ToolSet
└── Runtime (ModuleManager, LogStore)
```

### 3.2 模块划分

mutbot 未来会为 mutagent 提供各种工作流和扩展，Web 相关实现放在独立的 `web` 子模块中：

```
src/mutbot/
├── __init__.py
├── __main__.py               — 入口：解析 --host/--port，启动 uvicorn
├── workspace.py              — Workspace 工作区管理
├── session.py                — Session 生命周期、Agent 组装、持久化
├── web/                      — Web UI 模块（独立子包）
│   ├── __init__.py
│   ├── server.py             — FastAPI app、路由、静态文件
│   ├── agent_bridge.py       — WebUserIO（input_stream + present）、sync/async 桥接
│   ├── connection.py         — WebSocket ConnectionManager
│   ├── terminal.py           — PTY 管理 + WebSocket 桥接
│   ├── auth.py               — 认证
│   ├── serializers.py        — StreamEvent/Content → JSON
│   └── frontend_dist/        — 预构建前端资源
└── (未来扩展模块)

frontend/                      — 前端源码
├── src/
│   ├── App.tsx
│   ├── panels/               — SessionListPanel, AgentPanel, TerminalPanel, SourcePanel, LogPanel
│   ├── components/           — MessageList, StreamingText, ToolCallCard
│   ├── blocks/               — 内置块渲染器 + 动态块引擎
│   └── lib/                  — websocket, markdown, protocol
├── vite.config.ts
└── package.json
```

`workspace.py` 和 `session.py` 放在顶层而非 `web/` 中，因为它们是通用的会话管理概念，未来其他扩展（如 CLI 工作流、自动化流水线等）也会用到。

#### Agent 组装（session.py）

每个 Session 独立组装 Agent 实例，不复用 mutagent 的 `App.setup_agent()`（后者与终端 UserIO 和单一全局 Agent 绑定）。组装流程：

```python
# session.py — 创建 Session 时组装 Agent
from mutagent.config import Config
from mutagent.agent import Agent
from mutagent.client import LLMClient
from mutagent.tools import ToolSet
from mutagent.toolkits.module_toolkit import ModuleToolkit
from mutagent.toolkits.log_toolkit import LogToolkit

def create_agent(agent_config: dict) -> Agent:
    config = Config.load()  # 复用 mutagent 配置系统
    model = config.get_model(agent_config.get("model"))
    client = LLMClient(
        model=model["model_id"],
        api_key=model["auth_token"],
        base_url=model.get("base_url", ""),
    )
    tool_set = ToolSet(auto_discover=True)
    # 按需添加 Toolkit（初期可跳过 ModuleManager、子 Agent 等）
    return Agent(client=client, tool_set=tool_set,
                 system_prompt=agent_config.get("system_prompt", ""),
                 messages=[])
```

#### WebUserIO（agent_bridge.py）

`WebUserIO` 是 `UserIO` 的精简子类，仅实现 Web 场景需要的两个方法：

- **`input_stream()`**：返回 `Iterator[InputEvent]`，内部从 `queue.Queue` 阻塞读取。WebSocket 收到用户消息时 put 到队列。
- **`present(content)`**：捕获非 LLM 输出（工具副作用、子 Agent 结果），转发到事件队列供 WebSocket 广播。

不实现 `render_event()`、`read_input()`、`confirm_exit()`——桥接层直接遍历 `Agent.run()` 产出的 StreamEvent 并转发 WebSocket。

桥接核心循环：

```python
# agent_bridge.py
async def run_agent_in_thread(agent, web_userio, event_queue, loop):
    def agent_runner():
        for event in agent.run(web_userio.input_stream()):
            loop.call_soon_threadsafe(event_queue.put_nowait, event)
    await asyncio.to_thread(agent_runner)
```

### 3.3 Workspace 与 Session

#### 层次模型

```
Workspace（工作区，用户入口）
├── 项目路径：/path/to/project
├── 面板布局：flexlayout-react JSON 模型
├── Sessions:
│   ├── Session A（活跃）— 主 Agent 对话
│   ├── Session B（活跃）— 子 Agent 对话
│   └── Session C（历史）— 已结束的对话
└── Terminals:
    ├── Terminal 1（活跃）
    └── Terminal 2（活跃）
```

#### Workspace 工作区

用户以工作区为单位打开和使用 mutbot。一个 Workspace 对应一个项目上下文。

| 字段 | 说明 |
|------|------|
| `id` | 唯一标识 |
| `name` | 工作区名称（通常是项目名） |
| `project_path` | 关联的项目源码路径 |
| `layout` | 面板布局状态（JSON） |
| `sessions` | 关联的 Session 列表 |
| `terminals` | 关联的终端会话列表 |
| `created_at` / `updated_at` | 时间戳 |

职责：
- **面板组织**：管理面板布局，哪些面板打开、如何排列
- **项目关联**：记录源码路径，Agent 工具（如文件编辑、代码查看）基于此路径工作
- **Session 追踪**：维护该项目下所有 Agent 对话的引用
- **持久化**：工作区状态保存到 `.mutbot/workspaces/` 或项目目录的 `.mutbot/`。项目目录有 `.mutbot/` 时用项目级存储，否则用全局 `~/.mutbot/` 存储


#### Session 会话

一个 Session 封装一次完整的 Agent 对话过程。

| 字段 | 说明 |
|------|------|
| `id` | 唯一标识 |
| `workspace_id` | 所属工作区 |
| `title` | 会话标题（用于 Session 列表显示，可自动生成或用户重命名） |
| `agent_config` | Agent 配置（模型、system_prompt、工具集） |
| `messages` | 对话历史（mutagent Message 列表） |
| `events` | StreamEvent 历史（用于前端重放） |
| `status` | `active` / `paused` / `ended` |
| `created_at` / `updated_at` | 时间戳 |

职责：
- **Agent 生命周期**：创建、运行、暂停、恢复、终止 Agent 实例
- **对话持久化**：保存完整对话历史，支持掉线恢复和历史查看
- **I/O 桥接**：通过 `WebUserIO` 连接 Agent 同步循环与 WebSocket 异步通道

Session 持久化内容：
- `Message` 列表：用于 Agent 恢复上下文继续对话
- `StreamEvent` 列表：用于前端重放完整的流式体验（含工具调用过程）
- 用户格式化的输入数据

#### 用户流程

```
首次使用：
1. 打开 mutbot → 检测到无 Workspace
2. 自动以当前工作目录创建默认 Workspace（名称取目录名）
3. 自动创建初始 Session → 直接进入工作区，立即可以对话

常规使用：
1. 打开 mutbot → 工作区列表页（仅多个 Workspace 时有意义）
2. 选择/创建工作区 → 进入工作区（恢复面板布局）
3. 默认布局：
   - 左侧：Session 列表面板（类似聊天软件的会话列表）
   - 右侧主区域：当前 Session 的 Agent 对话面板
   - 可选：终端面板、代码编辑面板
4. 在 Session 列表中点击 → 快速切换到该 Session 的对话
5. 新建 Session → 列表中出现新条目，自动切换过去
6. 关闭浏览器 → 工作区状态自动保存
7. 重新打开 → 恢复到离开时的状态（含上次选中的 Session）
```

#### API 设计

```
工作区:
  GET    /api/workspaces              — 列出所有工作区
  POST   /api/workspaces              — 创建工作区
  GET    /api/workspaces/{id}         — 获取工作区详情（含布局、Session 列表）
  PUT    /api/workspaces/{id}         — 更新工作区（布局变更等）
  DELETE /api/workspaces/{id}         — 删除工作区

会话:
  POST   /api/workspaces/{wid}/sessions       — 在工作区中创建 Session
  GET    /api/workspaces/{wid}/sessions       — 列出工作区的 Session
  GET    /api/sessions/{id}                    — 获取 Session 历史（重连恢复）
  DELETE /api/sessions/{id}                    — 终止 Session
  WS     /ws/session/{id}                      — Session 实时事件流

终端:
  POST   /api/workspaces/{wid}/terminals      — 创建终端
  WS     /ws/terminal/{id}                     — 终端 I/O
```

### 3.4 配置架构

复用 mutagent 的 `Config` 配置系统。mutagent 的配置系统本身已设计为可扩展、可被下游项目使用。

**当前方案**：
- **Agent 配置**（API key、model、工具集）：直接使用 `Config.load()` 读取 `.mutagent/config.json`，与 mutagent 终端模式共享
- **mutbot 配置**（端口、认证、存储路径）：同样通过 `Config.load()` 读取 `.mutbot/config.json`
- **Session 创建 API**：可在请求体中传入 `agent_config` 覆盖默认模型配置

**未来演进**：扩展 mutagent 的 `Config` 系统，允许自定义从多个位置按优先级、按层级读取配置，统一 mutagent 和 mutbot 的配置加载机制。

### 3.5 依赖配置

```toml
# pyproject.toml
[project]
name = "mutbot"
dependencies = [
    "mutagent>=0.1.0",
    "fastapi>=0.100.0",
    "uvicorn[standard]>=0.20.0",
    "pywinpty>=2.0.0; sys_platform == 'win32'",
]
```

## 4. 功能设计

### 4.1 多用户模型

所有连接的用户都是**操作者**（无观察者角色）。类似多人聊天工具：

- **同账号同视图**：同一账号的不同客户端看到相同内容
- **会话持久化**：掉线重连/刷新页面后恢复完整会话
- **多客户端广播**：任一客户端的输入和 Agent 响应实时同步

### 4.2 认证

简单的用户名密码认证，配置在 mutbot 配置文件中：

- 本地模式（127.0.0.1）：可跳过认证
- 远程模式（`--host 0.0.0.0`）：要求认证
- HTTP POST 登录 → session token → WebSocket 连接携带

### 4.3 Agent 对话面板

| 功能 | 技术 |
|------|------|
| 流式文本渲染 | requestAnimationFrame 批量刷新 |
| Markdown + 代码高亮 | react-markdown + Shiki |
| 工具调用卡片 | 可展开/折叠，显示工具名、参数、结果、耗时 |
| 交互块 | ask → 选择列表，confirm → 确认/取消按钮 |
| 虚拟滚动 | @tanstack/virtual |

### 4.4 可演化的内容块系统

mutagent 的块类型（`mutagent:code`、`mutagent:tasks` 等）需要在 Web 端渲染。设计目标：**Agent 可以在运行时定义新的块类型和渲染方式**，无需重新构建前端。

#### 块渲染的三层架构

```
第一层：内置块渲染器（React 组件，预构建）
  → 已知块类型使用优化过的 React 组件渲染
  → code, tasks, status, thinking, ask, confirm, agents

第二层：声明式块（JSON Schema 驱动，无需 JS）
  → Agent 通过 define_module 注册新块类型的渲染 schema
  → 前端通用渲染器根据 schema 生成 UI（表格、列表、键值对、进度条等）
  → 类似低代码表单引擎

第三层：自定义块（HTML/CSS/JS，沙箱执行）
  → Agent 生成完整的 HTML+CSS+JS 渲染代码
  → 前端在 sandboxed iframe 中执行，通过 postMessage 通信
  → 最大灵活性，Agent 可创造任意可视化
```

#### 块注册协议

Agent 通过 mutagent 的 `define_module` 机制注册新块类型：

```python
# Agent 在运行时定义一个新的块类型
define_module("mutbot.blocks.progress_bar", '''
block_type = "progress"
schema = {
    "type": "declarative",
    "layout": [
        {"field": "label", "render": "text", "style": "bold"},
        {"field": "value", "render": "progress_bar", "max_field": "total"},
        {"field": "status", "render": "badge", "color_map": {"done": "green", "running": "blue"}}
    ]
}
''')
```

前端收到 `block_start` 事件时：
1. 查找内置渲染器 → 找到则使用
2. 查找已注册的声明式 schema → 找到则用通用渲染器
3. 查找自定义 HTML 渲染器 → 找到则在 iframe 沙箱中执行
4. 都没有 → 降级为纯文本（代码块样式）

#### 内置块类型

| 块类型 | 渲染层 | Web 渲染 |
|--------|--------|---------|
| `code` | 内置 | Shiki 高亮 + 复制按钮 |
| `tasks` | 内置 | 复选框列表 |
| `status` | 内置 | 状态卡片 |
| `thinking` | 内置 | 可折叠区域 |
| `ask` | 内置 | 选择列表 + 提交 |
| `confirm` | 内置 | 确认/取消按钮 |
| `agents` | 内置 | 实时状态仪表板 |
| `image` | 内置 | `<img>` 内联 |
| `chart` | 声明式/自定义 | ECharts/Plotly |
| `mermaid` | 声明式/自定义 | Mermaid.js → SVG |
| (Agent 自定义) | 声明式/自定义 | Agent 运行时定义 |

声明式块的通用渲染器只需一套代码，支持常见的展示模式（表格、列表、键值对、进度条、徽章、树形结构等），不依赖 Node.js 构建。自定义块的 iframe 沙箱也是纯浏览器能力，无需构建步骤。

### 4.5 面板布局

基于 flexlayout-react 的 JSON 模型：

- **分割 + 标签 + 浮动 + 弹出**：完整面板管理
- **拖拽调整**：面板可拖拽到不同位置
- **布局持久化**：保存到 Workspace 状态
- **程序化控制**：通过 `Model.doAction()` API（预留给语音助手等未来扩展）

面板类型：

| 面板 | 技术 | 说明 |
|------|------|------|
| **Session 列表** | React 组件 | 类似聊天软件侧边栏，显示所有 Session，点击切换，显示标题/状态/最后消息预览 |
| Agent 对话 | React 组件 | 当前选中 Session 的对话内容，支持多 Agent |
| 终端 | xterm.js + WebSocket 二进制 | PTY 桥接，resize 同步 |
| 代码编辑 | Monaco Editor（懒加载） | 查看/编辑/diff |
| 日志 | 实时日志流 | 级别过滤 + 搜索 |

Session 列表面板行为：
- 显示当前 Workspace 下所有 Session（活跃在上，历史在下）
- 每个条目显示：Session 名称/标题、状态指示（活跃/已结束）、最后一条消息预览、时间
- 点击条目 → 右侧对话面板切换到该 Session
- 顶部"新建 Session"按钮
- 右键菜单：重命名、删除、在新面板中打开

### 4.6 终端集成

mutbot 内置 PTY 管理：

- 跨平台 PTY：Unix `pty.fork` + Windows `pywinpty`
- WebSocket 二进制桥接（ttyd 风格协议）
- 终端生命周期与 Workspace 关联

### 4.7 语音助手（预留）

预留全局语音助手接口，当前不做具体设计和实现：

- 前端预留语音按钮 UI 位置
- 面板系统的 `Model.doAction()` API 已支持程序化控制
- 未来实现时可通过 Web Speech API + 意图解析 → 面板操控

### 4.8 多媒体内容

| 能力 | 说明 |
|------|------|
| 图片显示 | `<img>` 内联 |
| 交互式图表 | ECharts/Plotly |
| 流程图/架构图 | Mermaid.js → SVG |
| 文件上传 | 拖拽上传作为 Agent 输入 |

## 5. 通信协议

### 5.1 Agent 事件流（WebSocket JSON）

mutbot 桥接层直接转发 mutagent `StreamEvent`，事件类型与 mutagent 保持一致：

```json
// LLM 文本流
{"type": "text_delta", "text": "..."}

// LLM 构造工具调用（流式）
{"type": "tool_use_start", "tool_call": {"id": "tc_001", "name": "inspect_module", "arguments": {}}}
{"type": "tool_use_delta", "tool_json_delta": "{\"module_path\":"}
{"type": "tool_use_end", "tool_call": {"id": "tc_001", "name": "inspect_module", "arguments": {"module_path": "..."}}}

// Agent 执行工具
{"type": "tool_exec_start", "tool_call": {"id": "tc_001", "name": "inspect_module", "arguments": {...}}}
{"type": "tool_exec_end", "tool_call": {"id": "tc_001", ...}, "tool_result": {"content": "...", "is_error": false}}

// 控制事件
{"type": "response_done", "response": {"stop_reason": "end_turn", "usage": {...}}}
{"type": "turn_done"}
{"type": "error", "error": "..."}

// 交互事件（由 present() 捕获转发）
{"type": "interaction", "interaction_type": "ask", "question": "...", "options": [...]}
```

**块检测策略**：mutagent 的 `mutagent:xxx` fenced code block 在 `text_delta` 流中以纯文本出现。块检测（识别 `` ```mutagent:code `` 开头和 `` ``` `` 结尾）由**前端**负责，与 mutagent 终端模式的 `UserIO.render_event()` 逻辑对应。桥接层不做块解析，保持透传。

### 5.2 用户输入（WebSocket JSON）

```json
{"type": "message", "text": "...", "agent_id": "main"}
{"type": "interaction_response", "interaction_id": "iq_001", "value": "A"}
{"type": "control", "action": "cancel"}
```

## 6. 本地开发

```bash
# 终端 1：启动后端（自动重载）
uvicorn mutbot.web.server:app --reload --port 8741

# 终端 2：启动前端 dev server（HMR ~50ms）
cd frontend && npm run dev
# → http://localhost:5173，代理 /ws/* /api/* 到 localhost:8741
```

改前端代码 → Vite HMR 自动刷新；改后端代码 → uvicorn 自动重载。

## 7. 已确认的设计决策

| 决策 | 结论 |
|------|------|
| Workspace 存储 | 两者结合：项目目录 `.mutbot/` 优先，全局 `~/.mutbot/` 兜底 |
| Session 历史格式 | 同时存储 Message 列表（Agent 恢复）+ StreamEvent 列表（前端重放）+ 用户格式化输入 |
| 声明式块 Schema | 先从 5-8 个核心原语开始（text、list、table、key-value、progress、badge、code、link），迭代扩展 |
| 启动模式 | `python -m mutbot` 默认启动 Web 服务器，无需 `--web` 参数 |
| 前端框架 | React 19（flexlayout-react 程序化面板控制是决定性因素） |
| 后端框架 | FastAPI + uvicorn，通过线程桥接 mutagent 同步 Agent |
| 通信协议 | WebSocket（双向通信 + 终端二进制帧） |
| 语音助手 | 预留设计，不做具体实现 |
| Agent 组装 | `session.py` 自建精简版，每个 Session 独立 Agent，复用 `Config.load()` 获取模型配置 |
| WebUserIO 职责 | 精简为 `input_stream()`（Queue 阻塞读取）+ `present()`（非 LLM 输出转发），不实现 `render_event()` |
| 块检测 | 前端负责块检测，桥接层纯透传 StreamEvent |
| 配置来源 | 复用 mutagent Config 系统，Agent 配置读 `.mutagent/`，mutbot 配置读 `.mutbot/`，未来统一扩展 |
| 首次使用 | 自动创建默认 Workspace（当前工作目录）+ 初始 Session，用户立即可以对话 |

## 8. 实施步骤清单

### 阶段一：后端基础 + 前端骨架 [✅ 已完成]

最小可用：启动 Web → 创建 Workspace → 创建 Session → 发送消息 → Agent 响应 → 流式显示。

- [x] **Task 1.1**: 项目基础设施
  - [x] 修正 `src/mutbot/` 包结构，创建 `web/` 子包
  - [x] `pyproject.toml` 添加 mutagent、fastapi、uvicorn 依赖
  - [x] FastAPI 应用骨架 + 静态文件挂载（`frontend_dist/`）
  - [x] `__main__.py` 入口（启动 uvicorn，支持 --host / --port / --debug 参数）
  - 状态：✅ 已完成 (2026-02-22)

- [x] **Task 1.2**: Agent 桥接层（sync/async 桥）
  - [x] `WebUserIO` 子类：`input_stream()`（Queue 阻塞读取）+ `present()`（非 LLM 输出转发）
  - [x] `AgentBridge`：`asyncio.to_thread` + 双队列桥接，内置 event forwarder（一个 session 一个 forwarder，避免多连接竞争）
  - [x] StreamEvent → JSON 序列化（`serializers.py`）
  - [x] Bridge 生命周期与 Session 绑定（不随 WebSocket 断连销毁，支持重连）
  - 状态：✅ 已完成 (2026-02-22)

- [x] **Task 1.3**: Workspace + Session 管理基础
  - [x] `workspace.py`：创建/获取/列出，首次使用自动创建默认 Workspace
  - [x] `session.py`：创建/获取/列出，Agent 组装（`create_agent`），Session ↔ Agent 生命周期
  - [x] WebSocket 连接管理（`connection.py`）+ 广播
  - 状态：✅ 已完成 (2026-02-22)

- [x] **Task 1.4**: 前端骨架
  - [x] 初始化 Vite + React 19 + TypeScript
  - [x] ReconnectingWebSocket 客户端（自动重连 + 指数退避）
  - [x] 工作区自动加载
  - [x] Session 列表面板（侧边栏，点击切换 Session）
  - [x] 基础 Agent 对话面板（消息列表 + 输入组件）
  - [x] 端到端验证
  - 状态：✅ 已完成 (2026-02-22)

### 阶段二：对话体验增强 [✅ 已完成]

- [x] **Task 2.1**: 流式 Markdown + 代码高亮
  - [x] react-markdown + remark-gfm
  - [x] Shiki WASM 高亮（懒加载单例，预加载 14 种常用语言）
  - [x] `mutagent:xxx` 块类型 → BlockRenderer 路由（thinking → ThinkingBlock，其他 → CodeBlock 降级）
  - 状态：✅ 已完成 (2026-02-22)

- [x] **Task 2.2**: 工具调用可视化 + 交互块
  - [x] ToolCallCard（展开/折叠、工具名、参数预览、结果、耗时、状态指示）
  - [x] tool_call_id 匹配机制（tool_exec_start ↔ tool_exec_end 关联）
  - [x] AskBlock / ConfirmBlock → 预留（BlockRenderer 框架已就绪）
  - 状态：✅ 已完成 (2026-02-22)

### 阶段二补充：稳定性与调试基础设施 [✅ 已完成]

- [x] **Task 2.3**: Bug 修复与架构优化
  - [x] 修复 React StrictMode 双挂载导致 Bridge 被误杀的竞态条件
  - [x] Event forwarder 从 per-connection 改为 per-session（内置于 AgentBridge）
  - [x] 修复 text_delta 闭包快照问题（pendingTextRef → snapshot 捕获）
  - [x] Session 切换消息缓存（模块级 messageCache + messagesRef 方式保存/恢复）
  - 状态：✅ 已完成 (2026-02-23)

- [x] **Task 2.4**: 统一日志系统
  - [x] 前端日志通过 WebSocket 转发到后端（`remote-log.ts` → `{type:"log"}` → `mutbot.frontend` logger）
  - [x] 后端统一捕获 `mutbot.*` 和 `mutagent.*` 两个命名空间的日志
  - [x] 内存日志查询 API（`GET /api/logs?pattern=&level=&limit=`，复用 mutagent LogStore）
  - [x] 文件日志（`.mutagent/logs/YYYYMMDD_HHMMSS-log.log`）
  - [x] API 调用录制（`.mutagent/logs/YYYYMMDD_HHMMSS-api.jsonl`，复用 mutagent ApiRecorder）
  - 状态：✅ 已完成 (2026-02-23)

- [x] **Task 2.5**: 生产构建与静态服务
  - [x] Vite build → `frontend_dist/`，FastAPI StaticFiles 挂载
  - [x] 仅启动后端即可提供完整 Web 界面（`python -m mutbot`）
  - [x] 前端有改动时 `npm run build` 重新构建，无需重启后端
  - 状态：✅ 已完成 (2026-02-23)

### 阶段三：面板布局 + 终端 + 文件编辑 [待开始]

- [ ] **Task 3.1**: flexlayout-react 面板系统
  - [ ] JSON 布局模型 + factory 函数
  - [ ] 面板增删、拖拽、布局持久化到 Workspace
  - 状态：⏸️ 待开始

- [ ] **Task 3.2**: 终端面板
  - [ ] PTY 管理（跨平台）
  - [ ] xterm.js + WebSocket 二进制桥接
  - 状态：⏸️ 待开始

- [ ] **Task 3.3**: 文件编辑面板
  - [ ] Monaco Editor 懒加载
  - [ ] 代码查看 + diff 视图
  - 状态：⏸️ 待开始

- [ ] **Task 3.4**: 日志面板
  - [ ] 实时日志流 + 级别过滤
  - 状态：⏸️ 待开始

### 阶段四：会话持久化 + 多用户 + 认证 [待开始]

- [ ] **Task 4.1**: Workspace + Session 持久化
  - [ ] 对话历史存储（Message + StreamEvent）
  - [ ] Workspace 布局状态持久化
  - [ ] 掉线重连自动恢复
  - 状态：⏸️ 待开始

- [ ] **Task 4.2**: 多客户端同步
  - [ ] 所有客户端广播
  - [ ] 连接状态指示
  - 状态：⏸️ 待开始

- [ ] **Task 4.3**: 认证
  - [ ] 配置文件用户名密码
  - [ ] 登录 + session token
  - 状态：⏸️ 待开始

### 阶段五：可演化块系统 + 多媒体 [待开始]

- [ ] **Task 5.1**: 声明式块引擎
  - [ ] 块 Schema 规范定义
  - [ ] 通用声明式渲染器
  - [ ] Agent 注册新块类型的协议
  - 状态：⏸️ 待开始

- [ ] **Task 5.2**: 自定义块沙箱
  - [ ] iframe 沙箱执行环境
  - [ ] postMessage 通信协议
  - 状态：⏸️ 待开始

- [ ] **Task 5.3**: 多媒体内容
  - [ ] 图片、图表、Mermaid、文件上传
  - 状态：⏸️ 待开始

### 阶段六：打包与部署 [待开始]

- [ ] **Task 6.1**: 构建与打包
  - [ ] Vite 生产构建 → frontend_dist/
  - [ ] pyproject.toml package-data
  - [ ] `python -m mutbot` 入口（默认启动 Web 服务器）
  - [ ] 自动端口选择 + 浏览器打开
  - 状态：⏸️ 待开始
