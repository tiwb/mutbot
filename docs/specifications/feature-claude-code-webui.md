# Claude Code Web 界面集成 设计规范

**状态**：📝 设计中
**日期**：2026-03-13
**类型**：功能设计

## 背景

mutbot 目前可以通过 TerminalSession 嵌入 Claude Code CLI，但终端体验不佳（无法渲染 Markdown、无法展示 diff、权限按钮无法点击等）。

VS Code 的 Claude Code 扩展通过 spawn CLI 子进程 + `--input-format stream-json --output-format stream-json` 模式实现了原生 Web UI，体验远优于终端嵌入。mutbot 要复刻这一方案：**前端自己做 React 渲染，后端 spawn Claude Code CLI 子进程通过 stream-json 管道通信**。

## 设计方案

### 核心架构

```
mutbot 前端 (React)
  ↕ WebSocket (现有 channel 机制)
mutbot 后端 (Python)
  ↕ stdin/stdout 管道 (stream-json, 逐行 JSON)
Claude Code CLI 子进程 (Node.js)
```

### 后端：ClaudeCodeSession

新增 `ClaudeCodeSession`（Declaration 子类），管理一个 Claude Code CLI 子进程。

**子进程启动参数**：
```
claude --output-format stream-json --input-format stream-json --verbose
       --permission-prompt-tool stdio   # 权限请求走 stdio 管道
```

**生命周期**：
- `on_create` — 记录配置（cwd、model、permission_mode 等），不启动进程
- `on_connect` — 首个 channel 连接时 spawn CLI 子进程，启动 stdout 读取循环
- `on_message` — 处理前端消息（用户输入、权限回复、中断等），转发到 CLI stdin
- `on_disconnect` — 最后一个 channel 断开时不杀进程（支持重连）
- `on_stop` — 杀掉 CLI 子进程

**stdout 读取循环**（asyncio）：
- 逐行读取 stdout（每行一个 JSON）
- 解析 JSON，按 `type` 分发：
  - `assistant` / `user` / `result` — 转为事件广播给前端
  - `control_request`（`subtype: can_use_tool`）— 转为权限请求事件发给前端
  - `control_request`（`subtype: hook_callback`）— 转为 hook 回调事件
- 错误处理：进程退出时通知前端，支持重启

**stdin 写入**（宿主 → CLI）：
- 用户发消息 → 前端 → 后端 → 写入 stdin 的 JSON 行
- 权限回复 → 写入 `control_response`
- 控制指令（中断、切换模型等）→ 写入 `control_request`

### stream-json 协议（从 VS Code 扩展逆向）

**CLI stdout 输出的消息类型**：

| type | 含义 | message 格式 |
|------|------|-------------|
| `assistant` | AI 回复 | `{content: [{type: "text", text: "..."}, {type: "tool_use", id, name, input}]}` |
| `user` | 用户消息回显 / tool_result | `{content: [{type: "tool_result", tool_use_id, content}]}` |
| `system` | 系统消息 | — |
| `result` | 对话结束 | 包含最终结果 |
| `control_request` | CLI 向宿主请求 | `{request_id, request: {subtype, ...}}` |

**宿主写入 stdin 的消息类型**：

| type | subtype | 用途 |
|------|---------|------|
| `control_request` | `initialize` | 初始化（hooks、MCP servers 等） |
| `control_request` | `interrupt` | 中断当前任务 |
| `control_request` | `set_model` | 切换模型 |
| `control_request` | `set_permission_mode` | 切换权限模式 |
| `control_request` | `set_max_thinking_tokens` | 设置 thinking tokens |
| `control_request` | `rewind_files` | 回退文件变更 |
| `control_request` | `stop_task` | 停止后台任务 |
| `control_response` | `success` / `error` | 回复 CLI 的 control_request |

**权限交互流程**：
```
CLI stdout → {"type":"control_request", "request_id":"abc", "request":{"subtype":"can_use_tool", "tool_name":"Bash", "input":{"command":"rm -rf /"}, "permission_suggestions":[]}}

前端展示权限 UI → 用户点击"允许"/"拒绝"

宿主 stdin → {"type":"control_response", "response":{"subtype":"success", "request_id":"abc", "response":{"behavior":"allow"}}}
```

### 前端：ClaudeCodePanel

新增前端面板组件，负责渲染 Claude Code 的对话流。

**消息渲染**：
- `assistant` + `text` content → Markdown 渲染（复用现有 MarkdownBlock）
- `assistant` + `tool_use` content → 工具调用卡片（工具名、输入参数、折叠展开）
- `user` + `tool_result` content → 工具执行结果（成功/失败状态）
- `control_request` (can_use_tool) → 权限请求 UI（允许/拒绝按钮 + 输入预览）

**用户输入**：
- 文本输入框 + 发送按钮
- @提及文件（可选，后续）
- 中断按钮（发送 interrupt 控制指令）

**状态展示**：
- 连接状态（CLI 进程是否运行中）
- 模型选择
- 权限模式
- token 用量

### 与现有架构的融合

- `ClaudeCodeSession` 作为 `Session` 的 Declaration 子类，自动被 `resolve_class` 发现
- 复用现有 channel 机制：前端 connect session → 获得 channel → 收发消息
- 复用 panelFactory 注册机制：按 session type 选择 ClaudeCodePanel
- 复用 SessionRpc 的 create/connect/disconnect/stop 流程
- 不复用 AgentBridge / mutagent Agent — 这是独立于 mutagent 的 Claude Code 进程

### 进程管理

- 每个 ClaudeCodeSession 对应一个 CLI 子进程
- 使用 `asyncio.create_subprocess_exec` spawn
- stdin/stdout 用 `asyncio.StreamReader/StreamWriter`
- 需要设置环境变量 `CLAUDECODE=`（unset）避免嵌套检测
- Windows 上需要设置 `CLAUDE_CODE_GIT_BASH_PATH`
- 子进程退出时广播 `process_exited` 事件，前端展示重启选项

## 待定问题

### QUEST Q1: 用户消息如何发送给 CLI
**问题**：stream-json 的 input-format 下，用户新消息是通过 stdin 发送的。但具体格式需要确认——是直接写纯文本行，还是 JSON 格式？VS Code 扩展的代码中看到是通过 `control_request` 发送的，还是有其他通道？
**建议**：从扩展代码看，用户消息似乎也是通过某种 `control_request` 写入 stdin。需要实际测试 stream-json 协议，或者查阅 Claude Agent SDK 的 TypeScript 源码（`claude-agent-sdk-typescript` 仓库是公开的）来确认精确格式。先实现一个 MVP 验证协议。

### QUEST Q2: Claude Code 认证复用
**问题**：CLI 子进程的认证如何处理？是否自动复用本机已有的 Claude Code 登录状态？
**建议**：是的，Claude Code CLI 的认证状态保存在 `~/.claude/` 下，spawn 的子进程会自动继承。不需要额外处理认证。如果是 API key 模式，通过环境变量 `ANTHROPIC_API_KEY` 传入即可。

### QUEST Q3: 前端渲染复用程度
**问题**：ClaudeCodePanel 的消息渲染应该复用现有 AgentPanel 的组件（MessageList、MarkdownBlock、ToolBlock），还是从头实现？
**建议**：Claude Code 的 `assistant` 消息格式（Anthropic Messages API）与 mutagent 的 StreamEvent 格式不同，但渲染需求相似（Markdown + 工具卡片）。建议抽取通用渲染组件（MarkdownBlock、CodeBlock），但 ClaudeCodePanel 独立处理消息流和状态。

### QUEST Q4: Session 持久化
**问题**：ClaudeCodeSession 是否需要支持持久化和恢复？CLI 子进程无法 serialize/deserialize。
**建议**：Session 元数据（cwd、model、配置）持久化，但不持久化进程状态。恢复时 session 进入 "disconnected" 状态，用户可选择重新启动。Claude Code CLI 自身支持 `--resume` 恢复之前的对话，可以在重启时传入 `--resume <session_id>`。

## 关键参考

### 源码

**后端**：
- `mutbot/src/mutbot/session.py` — Session Declaration 基类，AgentSession/TerminalSession 定义
- `mutbot/src/mutbot/channel.py` — Channel 抽象，broadcast_json/send_json
- `mutbot/src/mutbot/runtime/session_manager.py` — Session CRUD + 生命周期管理
- `mutbot/src/mutbot/runtime/terminal.py` — TerminalSession 实现（on_connect/on_data 的参考模式）
- `mutbot/src/mutbot/runtime/agent_bridge.py` — AgentBridge 的 StreamEvent 序列化模式

**前端**：
- `mutbot/frontend/src/panels/AgentPanel.tsx` — 现有 Agent 面板（消息渲染参考）
- `mutbot/frontend/src/components/MessageList.tsx` — 消息列表组件
- `mutbot/frontend/src/lib/workspace-rpc.ts` — WebSocket RPC 客户端

**VS Code 扩展**（逆向参考）：
- `~/.vscode/extensions/anthropic.claude-code-2.1.63-win32-x64/extension.js` — 扩展主逻辑
- `~/.vscode/extensions/anthropic.claude-code-2.1.63-win32-x64/webview/index.js` — React 前端

**Claude Agent SDK**：
- `@anthropic-ai/claude-agent-sdk` npm 包 — TypeScript SDK（公开源码在 github.com/anthropics/claude-agent-sdk-typescript）
- `claude-agent-sdk-python` — Python SDK

### 协议参考
- CLI 参数：`claude --help` → `--output-format stream-json --input-format stream-json`
- `--permission-prompt-tool stdio` → 权限请求走 stdin/stdout
- 传输层：readline 逐行读取 stdout，每行一个 JSON 对象
