# Claude Code Web 界面集成 设计规范

**状态**：✅ 已完成
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

**启动时序**：
1. spawn CLI 子进程（带 `--input-format stream-json --output-format stream-json`）
2. 启动 stdout 读取循环
3. 等待 stdout 收到 `{type: "system", subtype: "init"}` 消息 → 提取 `session_id`、`tools`、`claude_code_version` 等
4. 广播 `init` 事件给前端（前端展示就绪状态）
5. 收到用户第一条消息后写入 stdin（不提前写，确保 CLI 初始化完成）

**注意**：`--permission-prompt-tool stdio` 是**必须的**参数。不传此参数时，CLI 会用内置终端 TUI 弹出权限提示，在管道模式下会卡死。

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

新增前端面板组件，独立于 AgentPanel，还原终端 Claude Code 的视觉体验。

**视觉风格——终端还原**：
- 等宽字体，全局统一字号（不区分 h1/h2/h3 大小）
- Markdown heading 渲染为加粗/换色，不放大字号
- 代码块与正文视觉融合（都是等宽字体，代码块仅加背景色区分）
- 紧凑布局，无聊天气泡，消息间用细分隔线或留白区分
- 自定义 Markdown 渲染：基于 markdown-it/remark + 自定义 CSS + renderer override

**流式渲染**：
- 必须处理 `stream_event` 类型（`SDKPartialAssistantMessage`）实现逐字流式输出
- `stream_event.event` 是 Anthropic `BetaRawMessageStreamEvent`，包含 `content_block_start`、`content_block_delta`、`content_block_stop` 等
- `assistant` 消息作为完整消息用于最终状态确认和持久化回显
- 流式文本增量拼接 → 实时 Markdown 渲染

**消息渲染**：
- `assistant` text → 终端风格 Markdown（等宽、统一字号）
- `assistant` tool_use → 紧凑折叠块（工具名 + 输入摘要，可展开查看完整 JSON）
- `user` tool_result → 工具执行结果（成功/失败标记 + 输出内容）
- `control_request` (can_use_tool) → inline 权限请求（工具名 + 输入预览 + 允许/拒绝按钮）
- `system` init → 会话信息摘要（版本、工具列表等）
- `system` status → 状态指示（如 "compacting..." 进度）

**用户输入**：
- 文本输入框 + 发送按钮
- 中断按钮（发送 interrupt 控制指令）
- @提及文件（后续版本）

**状态展示**：
- 连接状态（CLI 进程是否运行中）
- 模型 / 权限模式
- token 用量（从 result 消息中提取）

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

## 已确认决策

### 认证复用
CLI 子进程自动继承本机 `~/.claude/` 的登录状态，不需要额外处理。API key 模式通过环境变量 `ANTHROPIC_API_KEY` 传入。

### 前端独立 UI（终端风格）
ClaudeCodePanel 完全独立于现有 AgentPanel。原因不是"过渡性"，而是**视觉体系根本不同**：Claude Code 追求终端还原（等宽字体、统一字号、紧凑布局），AgentPanel 是聊天 UI 风格（气泡、大标题、卡片）。共享组件反而需要大量 override，不如独立写。

Markdown 渲染独立实现：同一个 markdown-it/remark 库，但 CSS 和 renderer rules 完全不同——heading 不放大、等宽字体、代码块无视觉跳跃。

### Session 持久化 + Claude Code session 追踪
Session 元数据（cwd、model、配置、claude_session_id）持久化。CLI 子进程本身不可序列化，恢复时 session 进入 "stopped" 状态。用户重新启动时通过 `--resume <claude_session_id>` 恢复 Claude Code 之前的对话。需要从 CLI 的 `system` init 消息中提取 `session_id` 并保存。

### 用户消息 stdin 格式（Q1 调研结论）

**已确认**：用户消息通过 stdin 写入 `SDKUserMessage` 格式的 JSON 行。

格式（从 VS Code 扩展 extension.js 逆向 + Agent SDK `sdk.d.ts` 类型定义交叉验证）：

```json
{"type":"user","session_id":"","message":{"role":"user","content":[{"type":"text","text":"用户输入内容"}]},"parent_tool_use_id":null}
```

**完整的多轮对话机制**：

1. **启动时**：`query()` 接受 `prompt: string | AsyncIterable<SDKUserMessage>`
   - 如果是 string，SDK 内部转为一条 `SDKUserMessage` 写入 stdin，然后关闭 stdin（单轮模式）
   - 如果是 AsyncIterable，SDK 持续消费队列中的消息写入 stdin（多轮模式）

2. **VS Code 扩展的做法**：
   - 启动时传入 AsyncIterable（内部是一个 `V1` 异步队列）
   - 用户每次发消息 → webview → extension → `channel.in.enqueue(userMessage)` → SDK 消费 → 写入 stdin
   - 关键代码：`if(U.type==="user") v.in.enqueue(U)`

3. **mutbot 的实现方式**：
   - 不用 SDK，直接 spawn CLI 进程 + 管道通信
   - 启动时写第一条用户消息到 stdin，**不关闭 stdin**
   - 后续每条用户消息直接写入 stdin（同格式的 JSON 行 + 换行符）
   - control_request / control_response 也走同一个 stdin

**SDKUserMessage 类型定义**（来自 `@anthropic-ai/claude-agent-sdk/sdk.d.ts`）：
```typescript
type SDKUserMessage = {
    type: 'user';
    message: MessageParam;           // Anthropic Messages API 的 MessageParam
    parent_tool_use_id: string | null;
    isSynthetic?: boolean;
    tool_use_result?: unknown;
    priority?: 'now' | 'next' | 'later';
    uuid?: UUID;
    session_id: string;              // 可以为空字符串
};
```

**SDKMessage（stdout 输出）完整类型列表**：
- `SDKAssistantMessage` — `{type: "assistant", message: BetaMessage, uuid, session_id}`
- `SDKUserMessage` / `SDKUserMessageReplay` — 用户消息回显
- `SDKResultMessage` — `{type: "result", subtype: "success"|"error", duration_ms, num_turns, ...}`
- `SDKSystemMessage` — `{type: "system", subtype: "init", claude_code_version, cwd, tools, ...}`
- `SDKStatusMessage` — `{type: "system", subtype: "status", status: "compacting"|null}`
- `SDKPartialAssistantMessage` — `{type: "stream_event", event: BetaRawMessageStreamEvent}`（流式增量）
- `SDKToolProgressMessage` — 工具执行进度
- `SDKToolUseSummaryMessage` — 工具使用摘要
- `SDKTaskNotificationMessage` / `SDKTaskStartedMessage` / `SDKTaskProgressMessage` — 后台任务
- `SDKRateLimitEvent` — 限流事件
- `SDKHookStartedMessage` / `SDKHookProgressMessage` / `SDKHookResponseMessage` — Hooks
- `SDKAuthStatusMessage` — 认证状态
- `SDKPromptSuggestionMessage` — 建议提示
- `SDKElicitationCompleteMessage` — 澄清问题完成
- `SDKFilesPersistedEvent` — 文件持久化
- `SDKCompactBoundaryMessage` — 上下文压缩边界

## 关键参考

### 源码

**后端**：
- `mutbot/src/mutbot/session.py` — Session Declaration 基类，AgentSession/TerminalSession/ClaudeCodeSession 定义
- `mutbot/src/mutbot/runtime/claude_code.py` — ClaudeCodeProcess + ClaudeCodeSession @impl（生命周期 + Channel 通信）
- `mutbot/src/mutbot/channel.py` — Channel 抽象，broadcast_json/send_json
- `mutbot/src/mutbot/runtime/session_manager.py` — Session CRUD + 生命周期管理
- `mutbot/src/mutbot/runtime/terminal.py` — TerminalSession 实现（on_connect/on_data 的参考模式）
- `mutbot/src/mutbot/runtime/agent_bridge.py` — AgentBridge 的 StreamEvent 序列化模式

**前端**：
- `mutbot/frontend/src/panels/ClaudeCodePanel.tsx` — Claude Code 面板组件（消息渲染 + 交互 + 状态管理）
- `mutbot/frontend/src/panels/claude-code.css` — 终端风格 CSS
- `mutbot/frontend/src/panels/PanelFactory.tsx` — 面板注册（PANEL_CLAUDE_CODE case）
- `mutbot/frontend/src/lib/layout.ts` — PANEL_CLAUDE_CODE 常量
- `mutbot/frontend/src/panels/AgentPanel.tsx` — 现有 Agent 面板（消息渲染参考）
- `mutbot/frontend/src/components/MessageList.tsx` — 消息列表组件
- `mutbot/frontend/src/components/WelcomePage.tsx` — 欢迎页（含 Claude Code 创建按钮）
- `mutbot/frontend/src/lib/workspace-rpc.ts` — WebSocket RPC 客户端

**VS Code 扩展**（逆向参考）：
- `~/.vscode/extensions/anthropic.claude-code-2.1.63-win32-x64/extension.js` — 扩展主逻辑
- `~/.vscode/extensions/anthropic.claude-code-2.1.63-win32-x64/webview/index.js` — React 前端

**Claude Agent SDK**：
- `@anthropic-ai/claude-agent-sdk` npm 包（`npm pack` 后解压查看）
  - `sdk.d.ts` — 完整类型定义（SDKMessage、SDKUserMessage、Query 等）
  - `sdk.mjs` — SDK 实现
- 公开源码：`github.com/anthropics/claude-agent-sdk-typescript`
- Python SDK：`github.com/anthropics/claude-agent-sdk-python`

### 协议参考
- CLI 参数：`claude --help` → `--output-format stream-json --input-format stream-json`
- `--permission-prompt-tool stdio` → 权限请求走 stdin/stdout
- 传输层：readline 逐行读取 stdout，每行一个 JSON 对象
- stdin 消息格式：JSON 行 + `\n`，用 `JSON.stringify(msg) + "\n"` 写入
- stdout 解析：扩展用 `readline.createInterface({input: process.stdout})`，逐行 `JSON.parse`

-------- 以下章节在"🔄 实施中"阶段生成 --------

## 实施步骤清单

### 阶段一：后端核心 [✅ 已完成]

- [x] **Task 1.1**: ClaudeCodeSession Declaration
  - [x] 在 `session.py` 中新增 `ClaudeCodeSession(Session)` 声明
  - [x] 字段：cwd、model、permission_mode、claude_session_id、status
  - [x] serialize/deserialize 支持元数据持久化（自动基于 __annotations__）
  - 状态：✅ 已完成

- [x] **Task 1.2**: ClaudeCodeProcess — CLI 子进程管理
  - [x] 新建 `mutbot/runtime/claude_code.py`
  - [x] `start()` — `asyncio.create_subprocess_exec` spawn CLI，设置 env（unset CLAUDECODE、CLAUDE_CODE_GIT_BASH_PATH）
  - [x] `_read_loop()` — asyncio readline 逐行读取 stdout JSON，回调分发
  - [x] `_read_stderr()` — stderr 读取，记录到 mutbot 日志
  - [x] `write(msg)` — JSON 序列化 + `\n` 写入 stdin
  - [x] `stop()` — 优雅关闭（先关 stdin，等进程退出，超时后 kill）
  - [x] 进程退出检测 + 回调通知
  - 状态：✅ 已完成

- [x] **Task 1.3**: ClaudeCodeSession 生命周期实现（`@impl`）
  - [x] `on_create` — 初始化配置，创建 ClaudeCodeProcess 实例（不启动）
  - [x] `on_connect` — 首个 channel 连接时启动进程，等待 `system` init，提取 session_id 保存，广播 init 给前端
  - [x] `on_message` — 按 type 分发：`user_message` → 写 stdin，`permission_response` → 写 control_response，`interrupt` → 写 control_request，`control` → 其他控制指令
  - [x] `on_disconnect` — 不杀进程
  - [x] `on_stop` — 停止进程，清理资源
  - [x] stdout 消息统一透传前端（`broadcast_json`），仅 `control_request` 做特殊处理
  - [x] 消息历史缓存：后端保存已收到的 stdout 消息列表，重连时回放
  - 状态：✅ 已完成

- [x] **Task 1.4**: 后端冒烟测试
  - [x] 验证 ClaudeCodeSession 创建、序列化/反序列化
  - [x] 验证 @impl 注册和 resolve_class
  - [x] 验证服务器启动无报错
  - 状态：✅ 已完成

### 阶段二：前端核心 [✅ 已完成]

- [x] **Task 2.1**: ClaudeCodePanel 基础框架
  - [x] 新建 `frontend/src/panels/ClaudeCodePanel.tsx`
  - [x] 在 panelFactory 中注册 ClaudeCodeSession type 映射（`PANEL_CLAUDE_CODE`）
  - [x] App.tsx kind→component 映射（`claudecode` → `PANEL_CLAUDE_CODE`）
  - [x] WelcomePage 添加 "Claude Code" 创建按钮
  - [x] 基础布局：消息区（滚动） + 输入区（底部固定）
  - [x] channel 连接/断开处理，接收后端广播消息
  - 状态：✅ 已完成

- [x] **Task 2.2**: 终端风格 Markdown 渲染
  - [x] 新建 `frontend/src/panels/claude-code.css`（终端风格 CSS）
  - [x] 复用项目 `Markdown` 组件，通过 `.cc-terminal-md` CSS 覆写实现终端风格
  - [x] 等宽字体、统一字号、heading 渲染为 bold 不放大、代码块仅背景色区分
  - [x] 代码高亮复用 shiki（通过 Markdown → CodeBlock 组件链路）
  - 状态：✅ 已完成

- [x] **Task 2.3**: 消息流渲染
  - [x] 消息状态管理：接收 `claude_code_event` 事件，维护 CCMessage 列表
  - [x] `stream_event` 处理：解析 `content_block_start/delta/stop`，按 index 增量拼接文本
  - [x] `assistant` 消息：作为完整消息替换流式中间状态
  - [x] `tool_use` 渲染：紧凑折叠块（工具名 + ID，可展开查看完整输入 JSON）
  - [x] `tool_result` 渲染：结果输出 + 成功/失败标记（从 `user` 消息的 `tool_result` block 提取）
  - [x] `system` init：版本号 + 模型名
  - [x] `result`：duration + turns + cost
  - [x] `process_exited`：进程退出通知
  - 状态：✅ 已完成

- [x] **Task 2.4**: 用户交互
  - [x] 输入框：发送 `user_message` → 后端转 SDKUserMessage 写入 stdin
  - [x] 中断按钮：发送 `interrupt` 控制指令
  - [x] 权限请求 UI：inline 允许/拒绝按钮，展示工具名 + 可展开输入预览
  - 状态：✅ 已完成

### 阶段三：完善 [✅ 已完成]

- [x] **Task 3.1**: Session 恢复
  - [x] 重连时回放后端缓存的消息历史（on_connect 检测已有 running 进程时回放）
  - [x] 进程退出后，前端展示状态（process_exited 消息）
  - [x] 重启时使用 `--resume <claude_session_id>` 恢复对话（ClaudeCodeProcess 接受 resume_session_id）
  - 状态：✅ 已完成

- [x] **Task 3.2**: 状态栏与控制
  - [x] 进程状态指示（绿色/灰色状态点 + "disconnected" 标签）
  - [x] Thinking 动画（脉冲闪烁）
  - [x] compacting 状态通过 `system` status 消息展示
  - 状态：✅ 已完成

- [x] **Task 3.3**: 构建与集成测试
  - [x] 前端 build 通过（`npm run build` 无错误）
  - [x] 服务器启动无报错
  - [x] discover_subclasses 正确发现 ClaudeCodeSession
  - [x] AddSessionMenu 动态菜单自动包含 Claude Code 选项
  - 状态：✅ 已完成

## 实施风险

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| **stream_event 高频消息成为瓶颈** | 后端处理不过来，前端渲染卡顿 | 后端原样透传不处理；前端用 requestAnimationFrame 节流渲染 |
| **stream-json 协议未公开文档** | 格式可能随 CLI 版本变化 | 已从 SDK 类型定义交叉验证；实施时做容错处理（unknown type 忽略不崩溃） |
| **Windows 上 CLI spawn 路径问题** | CLAUDE_CODE_GIT_BASH_PATH 检测严格 | 实施时优先验证 Windows 环境；必要时通过 `claude` 命令的绝对路径 spawn |
| **`--resume` 是否在 stream-json 模式下回放历史** | 影响 session 恢复体验 | 先用后端消息缓存兜底；后续验证 resume 行为 |
| **权限请求超时** | CLI 等待权限回复无响应 | 前端权限 UI 需要醒目提示；后端可设置默认超时自动拒绝 |

## 测试验证

- [x] 后端：ClaudeCodeSession 创建/序列化/反序列化 round-trip
- [x] 后端：@impl 注册完整（on_create/on_connect/on_message/on_disconnect/on_stop）
- [x] 后端：resolve_class / discover_subclasses 正确发现 ClaudeCodeSession
- [x] 后端：服务器启动无报错
- [x] 前端：npm run build 无编译错误
- [x] 前端：AddSessionMenu 动态菜单自动包含 Claude Code
- [ ] 端到端：创建 session → 发消息 → 流式渲染 → 权限交互 → 对话结束（待手动验证）
