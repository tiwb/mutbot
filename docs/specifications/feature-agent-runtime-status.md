# Agent 运行时状态与上下文展示 设计规范

**状态**：✅ 已完成
**日期**：2026-02-26
**类型**：功能设计

## 1. 背景

当前 mutbot 的 Agent session 界面缺少三项关键的运行时交互能力：

1. **工作状态指示**：Agent 在处理请求时没有状态反馈，用户无法得知 Agent 是否正在思考、调用工具还是已完成
2. **Context 用量显示**：用户无法了解当前对话的 token 消耗情况，可能在不知情的情况下接近或超出上下文窗口限制
3. **中断 Agent 思考**：Agent 在长时间思考或调用工具时，用户无法中断当前操作，只能等待完成或刷新页面

### 现有架构基础

- **事件流**：后端通过 WebSocket 推送 `StreamEvent`（定义在 `mutagent/messages.py`），包含 `text_delta`、`tool_exec_start`、`tool_exec_end`、`response_done`、`turn_done` 等类型
- **事件广播路径**：`Agent → agent_bridge.py (serialize_stream_event) → ConnectionManager.broadcast → WebSocket → Frontend`
- **Token 用量**：`response_done` 事件的 `response.usage` 已包含 `{"input_tokens": N, "output_tokens": N}`，但前端未提取展示
- **模型配置**：`LLMClient` 持有 `model`（模型标识符）和 `provider`，但当前无 `context_window` 信息

## 2. 设计方案

### 2.1 核心设计决策

| 决策点 | 结论 |
|--------|------|
| 状态推导位置 | **后端推送**，前端只负责展示 |
| Context 窗口来源 | 配置 > 内置查找表 > 未知降级 |
| Token 显示内容 | 两项：① Context 使用百分比 ② Session 级累计 token |
| 用量展示形式 | 百分比文字（着色），无进度条 |
| 发送→首事件延迟 | 前端发送消息时立即切 thinking 状态 |
| 模型切换 | 每次 `token_usage` 事件附带当前 model 和 context_window |
| Token 累计范围 | Session 级别，非应用级别 |
| 累计 token 持久化 | 不持久化，重启后从 0 开始 |

### 2.2 Agent 工作状态指示

**后端推送 `agent_status` 事件**：

在 `agent_bridge.py` 的事件广播层注入状态事件，不修改 mutagent 核心的 `StreamEvent` 定义。

```
用户发消息 → agent_status(thinking)
text_delta  → （首个 text_delta 时不需要额外事件，已经是 thinking）
tool_exec_start → agent_status(tool_calling, tool_name)
tool_exec_end   → agent_status(thinking)
turn_done / agent_done → agent_status(idle)
error → agent_status(idle)
```

**事件格式**：

```json
{"type": "agent_status", "status": "idle"}
{"type": "agent_status", "status": "thinking"}
{"type": "agent_status", "status": "tool_calling", "tool_name": "web_search"}
```

**前端展示**（消息列表底部内联指示器）：

| 状态 | 表现 |
|------|------|
| `idle` | 隐藏指示器 |
| `thinking` | 动画圆点 + "思考中..." |
| `tool_calling` | 工具图标 + "调用 {tool_name}..." |

### 2.3 Context 用量与累计 Token 显示

**两项指标**：

1. **Context 使用百分比**：`input_tokens / context_window`，反映当前对话上下文的使用率
2. **累计 Session Token**：`Σ(input_tokens + output_tokens)`，session 级别，用于成本估算

**后端职责**：
- 在 `agent_bridge.py` 中维护 session 级累计 token 计数器（不持久化，重启后归零）
- 在每个 `response_done` 事件之后，额外推送 `token_usage` 事件

**`token_usage` 事件格式**：

```json
{
  "type": "token_usage",
  "context_used": 15234,
  "context_window": 200000,
  "context_percent": 7.6,
  "session_total_tokens": 28500,
  "model": "claude-sonnet-4-20250514"
}
```

| 字段 | 说明 |
|------|------|
| `context_used` | 最近一次 response 的 `input_tokens`（= 当前上下文大小） |
| `context_window` | 当前模型的上下文窗口总量，未知时为 `null` |
| `context_percent` | 已计算好的百分比，`context_window` 未知时为 `null` |
| `session_total_tokens` | session 内所有 response 的 `input_tokens + output_tokens` 累加 |
| `model` | 当前使用的模型（运行中切换模型后前端可感知变更） |

**前端展示**（Agent 面板头部，连接状态旁）：
- 格式示例：`Context: 7.6% | Session: 28.5K tokens`
- 百分比着色：绿 (< 50%) / 黄 (50%-80%) / 红 (> 80%)
- `context_window` 未知时降级显示：`Context: 15.2K tokens | Session: 28.5K tokens`

### 2.4 Context Window 数据来源

**优先级链**：

1. **模型级配置** — providers 配置中 model 级别指定的 `context_window`
2. **Provider 级配置** — providers 配置中 provider 级别的 `context_window`（同 provider 下所有模型的默认值）
3. **内置查找表** — `mutagent` 中硬编码的常见最新模型 context_window 映射
4. **未知** — 以上都匹配不到时，`context_window = null`，前端降级为只显示绝对 token 数

**模型配置示例**（`.mutagent/config.json`）：

```json
{
  "providers": {
    "anthropic": {
      "provider": "AnthropicProvider",
      "api_key_env": "ANTHROPIC_API_KEY",
      "context_window": 200000,
      "models": ["claude-sonnet-4-20250514", "claude-haiku-4-5-20251001"]
    }
  }
}
```

provider 级别设置默认值，个别模型可通过 dict 形式覆盖：

```json
{
  "models": {
    "claude-sonnet-4-20250514": {},
    "claude-haiku-4-5-20251001": {"context_window": 200000}
  }
}
```

**内置查找表**（`mutagent` 中维护，支持通配符，精确匹配优先）：

```python
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Anthropic — 通配符覆盖所有 Claude 模型
    "claude-*": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "o1": 200_000,
    "o3-mini": 200_000,
}
```

查找顺序：精确匹配 > 通配符匹配（多个匹配时最长 pattern 优先）。如需对特定模型覆盖，添加精确条目即可（如 `"claude-opus-4-6": 1_000_000`）。

**支持运行中切换模型**：
- `LLMClient` 持有 `context_window` 属性
- 模型切换时更新 `context_window`（从配置或查找表重新解析）
- 每次 `token_usage` 事件带上当前 `model` 和 `context_window`，前端自然感知变更

### 2.5 中断 Agent 思考

#### 两种操作

| 操作 | 触发方式 | 行为 | 频率 |
|------|----------|------|------|
| **发送新消息** | 用户在 agent 工作时发消息 | **不强制中断**，消息入队，agent 在下一个自然断点接收并处理 | 常见 |
| **强制停止** | 用户点击 Stop 按钮 | 立即取消 agent task，提交部分状态到 messages | 少见 |

**输入框始终可用**，不因 Agent 工作状态而禁用。Stop 按钮仅在 Agent 工作时可用。

---

#### 方式一：发送新消息（非中断，尽快传达）

类似 Claude Code：发消息**不会**立即中断 AI，而是尽快让 AI 得知用户的新消息。

**当前问题**：Agent 内层循环（tool rounds）最多 25 轮，每轮含 LLM 调用 + 多个工具执行。用户新消息必须等整个内层循环结束后才被处理，延迟可能很大。

**改进**：Agent 在**自然断点**检查是否有待处理的用户消息，有则结束当前 turn，让外层循环读取新消息。

**自然断点** = 每轮工具执行完毕、tool_results 已提交后，进入下一轮 LLM 调用前：

```
内层循环每轮结束时:
    tool_results 已提交到 agent.messages  ← 干净状态
    ↓
    检查: 有待处理的新消息吗? (check_pending)
    ├─ 否 → 继续下一轮 LLM 调用
    └─ 是 → break 内层循环 → yield turn_done
              ↓
        外层循环读取新消息 → 正常处理
```

**消息历史**：在自然断点中断时，所有状态已提交，消息历史是干净的：
```
user: "分析项目中所有的性能问题"
assistant: [tool_calls: grep("performance"), read_file("config.py")]
user: [tool_results: {grep: "Found 8 matches...", read_file: "..."}]    ← 已提交
user: "等一下，先帮我看看 utils.py 的那个 bug"     ← 新消息
assistant: "好的，让我先看 utils.py..."
```

**连续 user 消息处理**：上例中 `user(tool_results)` 和 `user(text)` 相邻。Anthropic API 不允许连续同角色消息，需要在 provider 消息转换层合并：
```json
{"role": "user", "content": [
  {"type": "tool_result", "tool_use_id": "...", "content": "..."},
  {"type": "text", "text": "等一下，先帮我看看 utils.py 的那个 bug"}
]}
```

**实现要点**：

1. **Agent 层**：`Agent.run()` 接受 `check_pending: Callable[[], bool] | None` 回调参数。内层循环每轮 tool_results 提交后调用，返回 `True` 则 break。
2. **Bridge 层**：传入 `check_pending=lambda: not self._input_queue.empty()`。
3. **Provider 层**：`_messages_to_claude()` / `_messages_to_openai()` 合并连续同角色消息。

---

#### 方式二：强制停止（Stop）

用于用户需要立即终止 agent 操作的场景（如 agent 陷入无意义循环、调用了错误工具、LLM 输出离题等）。

**实现方案：asyncio Task 取消 + 状态追踪**

取消 agent asyncio Task，利用 `CancelledError` 传播机制立即中断。Bridge 追踪飞行中状态，在 `CancelledError` 处理中提交部分消息到 `agent.messages`，使下次 LLM 调用能感知被中断的上下文。

**`agent.messages` 变更时序**（理解哪些状态需要补提交）：

```
[Point 1] 收到用户消息 → agent.messages.append(user_msg)     ← 已提交
    ↓
[IN-FLIGHT] Agent.step() → LLM 流式输出 text_delta, tool_use 事件
    ↓
[Point 2] response_done → agent.messages.append(assistant_msg)  ← 已提交
    ↓
[IN-FLIGHT] 逐个执行 tool_calls → tool_exec_start/end 事件
    ↓
[Point 3] 全部工具完成 → agent.messages.append(user_msg(tool_results))  ← 已提交
```

**强制停止时的状态提交**：

| 中断阶段 | agent.messages 状态 | 需要补提交 |
|----------|---------------------|-----------|
| LLM 流式输出中（Point 2 前） | `[..., user_msg]` | 部分 assistant 消息（已累积 text + `[interrupted]`） |
| 工具执行中（Point 3 前） | `[..., user_msg, assistant(tool_calls)]` | tool_results（已完成的 + 未完成标记 interrupted） |
| turn 间隙（clean state） | 完整 | 无需补提交 |

**强制停止后的消息历史示例**：

场景 A — LLM 输出中被停止：
```
user: "分析这段代码的性能问题"
assistant: "让我来分析一下。首先这段代码有几个明显的问题：\n1. 循环中的数据库查询\n\n[interrupted]"
```

场景 B — 工具执行中被停止：
```
user: "搜索并分析项目中的所有 TODO"
assistant: [tool_calls: grep_code("TODO"), read_file("README.md")]
user: [tool_results: {grep_code: "Found 15 TODOs...", read_file: "[Tool execution interrupted by user]"}]
```
→ 下次发消息时，LLM 能看到完整的中断上下文。

---

#### 事件流

**发送新消息（非中断）**：
```
用户发消息 → send_message() → 消息入队 + 广播到前端
    ↓
Agent 当前轮工具执行完毕，tool_results 已提交
    ↓
check_pending() 返回 True → break 内层循环 → turn_done
    ↓
外层循环读取新消息 → agent.messages.append(user_msg) → 正常 LLM 调用
```

**强制停止**：
```
用户点击 Stop → 前端发送 {"type": "cancel"}
    ↓
routes.py → await bridge.cancel()
    ↓
bridge.cancel(): task.cancel() → CancelledError 传播
    ↓
_run() catch CancelledError:
    1. _commit_partial_state() → 补提交部分消息到 agent.messages
    2. 广播 agent_status(idle) + agent_cancelled
    ↓
bridge 就绪，等待下一条消息
```

---

#### 后端变更

**`agent_impl.py`** — `Agent.run()` 增加 `check_pending` 回调：

```python
@mutagent.impl(Agent.run)
async def run(self, input_stream, *, check_pending=None, stream=True):
    async for input_event in input_stream:
        ...
        while True:
            # LLM call → response_done → append assistant msg
            ...
            if not response.message.tool_calls:
                break
            # Tool execution → append tool_results
            ...
            self.messages.append(Message(role="user", tool_results=results))

            # 自然断点：检查是否有待处理的用户消息
            if check_pending and check_pending():
                break
        yield StreamEvent(type="turn_done")
```

**`agent_bridge.py`** — 状态追踪与 cancel：

```python
class AgentBridge:
    def __init__(self, ...):
        # 飞行中状态追踪（用于强制停止时补提交）
        self._pending_text: list[str] = []
        self._pending_tool_calls: list[ToolCall] = []
        self._completed_results: list[ToolResult] = []
        self._response_committed: bool = False

    async def _run(self):
        try:
            async for event in self.agent.run(
                self._input_stream(),
                check_pending=lambda: not self._input_queue.empty(),
            ):
                self._track_event(event)    # 追踪飞行中状态
                # ... 广播事件（同现有逻辑）
        except asyncio.CancelledError:
            self._commit_partial_state()    # 补提交部分消息
            await self._broadcast_status("idle")
            await self.broadcast_fn(self.session_id, {"type": "agent_cancelled"})

    async def cancel(self) -> None:
        """Force stop current thinking."""
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
            try:
                await self._agent_task
            except asyncio.CancelledError:
                pass
```

**Provider 消息转换** — 合并连续同角色消息：

```python
# _messages_to_claude(): 合并相邻 user 消息
# [user(tool_results), user(text)] → 单个 user(tool_result blocks + text block)
```

**`routes.py`** WebSocket handler：
```python
elif msg_type == "cancel":
    await bridge.cancel()
```

#### 前端变更

**ChatInput 组件**：
- 输入框始终可用（`disabled` 仅在未连接时）
- Send 按钮始终可用（agent 工作时发送 = 消息入队，agent 尽快处理）
- Stop 按钮：仅 `agentStatus !== "idle"` 时可用，发送 `{"type": "cancel"}`

**AgentPanel**：
- 处理 `agent_cancelled` 事件 → `agentStatus` 设为 `idle`
- 已渲染的部分内容保持不变

## 3. 待定问题

无。所有设计问题已确认。

## 4. 实施步骤清单

### 阶段一：后端 - context_window 数据链路 [✅ 已完成]

- [x] **Task 1.1**: 内置模型 context_window 查找表
  - [x] 在 `mutagent/client.py` 中添加 `MODEL_CONTEXT_WINDOWS` 常量字典
  - [x] 包含最新常见模型（Claude、GPT-4o、o1 等）
  - 状态：✅ 已完成

- [x] **Task 1.2**: LLMClient 增加 context_window 属性
  - [x] `LLMClient` Declaration 增加 `context_window: int | None` 字段
  - [x] `Config.get_model()` 支持 dict 形式模型的 context_window 覆盖
  - [x] `create_llm_client()` 从配置提取 context_window，配置无则查内置表
  - 状态：✅ 已完成

### 阶段二：后端 - 事件推送 [✅ 已完成]

- [x] **Task 2.1**: agent_bridge 推送 agent_status 事件
  - [x] 在事件处理循环中，根据 StreamEvent 类型插入 `agent_status` 事件
  - [x] 用户消息接收时推送 `thinking`
  - [x] `tool_exec_start` 时推送 `tool_calling`（含 tool_name）
  - [x] `tool_exec_end` 时推送 `thinking`
  - [x] `turn_done` / `agent_done` / `error` 时推送 `idle`
  - 状态：✅ 已完成

- [x] **Task 2.2**: agent_bridge 推送 token_usage 事件
  - [x] 维护 session 级累计 token 计数器（`_session_total_tokens`）
  - [x] 拦截 `response_done` 事件，提取 `usage`，更新累计值
  - [x] 从 `agent.client.context_window` 获取 context_window
  - [x] 推送 `token_usage` 事件（含所有字段，context_window 未知时为 null）
  - 状态：✅ 已完成

### 阶段三：前端展示 [✅ 已完成]

- [x] **Task 3.1**: Agent 状态指示器
  - [x] 处理 `agent_status` 事件，维护状态 state
  - [x] 创建 AgentStatusIndicator 组件（动画圆点 / 工具图标 + 文字）
  - [x] 集成到消息列表底部
  - [x] 发送消息时前端立即切 thinking（乐观更新）
  - 状态：✅ 已完成

- [x] **Task 3.2**: Token 用量显示
  - [x] 处理 `token_usage` 事件，维护 tokenUsage state
  - [x] 在 Agent 面板头部显示 Context 百分比 + Session 累计 token
  - [x] 百分比着色（绿 < 50% / 黄 50-80% / 红 > 80%）
  - [x] context_window 为 null 时降级显示绝对 token 数
  - 状态：✅ 已完成

### 阶段四：中断 Agent 思考 [✅ 已完成]

- [x] **Task 4.1**: Agent.run() 增加 check_pending 回调
  - [x] `Agent.run()` 签名增加 `check_pending: Callable[[], bool] | None = None`
  - [x] 内层循环每轮 tool_results 提交后调用 `check_pending()`，返回 True 则 break
  - [x] 不影响无 `check_pending` 时的原有行为
  - 状态：✅ 已完成

- [x] **Task 4.2**: Provider 消息转换合并连续同角色消息
  - [x] `_messages_to_claude()` 合并相邻 user 消息（tool_result blocks + text block）
  - [x] `_messages_to_openai()` 合并相邻 user 消息
  - [x] user 消息含 tool_results 时也包含 text content
  - 状态：✅ 已完成

- [x] **Task 4.3**: AgentBridge 状态追踪与强制停止
  - [x] 在 `_run()` 中追踪飞行中状态（pending_text, pending_tool_calls, completed_results, response_committed）
  - [x] 实现 `_commit_partial_state()`：根据中断阶段补提交部分消息到 agent.messages
  - [x] 实现 `cancel()` 方法：取消 agent task，等待 CancelledError 处理完成，重启 task
  - [x] `_run()` 的 CancelledError 处理中调用 `_commit_partial_state()` + 广播 agent_cancelled
  - [x] 传入 `check_pending=lambda: not self._input_queue.empty()` 给 agent.run()
  - 状态：✅ 已完成

- [x] **Task 4.4**: WebSocket 路由增加 cancel 处理
  - [x] routes.py 中 WebSocket handler 处理 `{"type": "cancel"}` 消息
  - [x] 调用 `await bridge.cancel()`
  - 状态：✅ 已完成

- [x] **Task 4.5**: 前端 Stop 按钮与交互
  - [x] 输入框始终可用（不因 agent 工作状态禁用）
  - [x] Send 按钮始终可用（agent 工作时发送 = 消息入队，agent 尽快处理）
  - [x] Stop 按钮：始终可见，仅 agent 工作时可用，发送 `{"type": "cancel"}`
  - [x] AgentPanel 处理 `agent_cancelled` 事件，agentStatus → idle
  - [x] 已渲染的部分内容保持不变
  - 状态：✅ 已完成

---

### 实施进度总结
- ✅ **阶段一：后端 context_window 数据链路** - 100% 完成 (2/2 任务)
- ✅ **阶段二：后端事件推送** - 100% 完成 (2/2 任务)
- ✅ **阶段三：前端展示** - 100% 完成 (2/2 任务)
- ✅ **阶段四：中断 Agent 思考** - 100% 完成 (5/5 任务)

**状态/用量功能完成度：100%** (6/6 任务)
**中断功能完成度：100%** (5/5 任务)
**测试：mutagent 705 passed, mutbot 250 passed, frontend build OK**

## 5. 测试验证

### 功能测试（状态与用量）
- [x] `agent_status` 事件在正确时机推送（thinking / tool_calling / idle）
- [x] `token_usage` 事件在每个 response_done 后推送，数值正确
- [x] 累计 token 跨多轮对话正确累加（session 级别）
- [x] context_window 按优先级链正确解析（model 配置 > provider 配置 > 内置表 > null）
- [x] 前端状态指示器正确响应各状态
- [x] Token 用量数值正确显示和着色

### 功能测试（中断）
- [x] Agent thinking 时发送新消息：中断当前 LLM 流 → 部分文本 + [interrupted] 提交到 messages → 新消息被处理
- [x] Agent tool_calling 时发送新消息：已完成工具结果保留，未完成标记 interrupted → 新消息被处理
- [x] Agent thinking 时点击 Stop：中断，状态回到 idle，不发新消息
- [x] 中断后 LLM 能看到中断历史（部分文本、工具结果、interrupted 标记）
- [x] Cancel 后已渲染的前端内容保持不变（不跳变）
- [x] 快速连续中断不导致状态异常
- [x] 输入框在 agent 工作时始终可用

### 边界测试
- [x] 模型配置和内置表均无 context_window 时降级显示绝对 token 数
- [x] Agent 出错时状态回到 idle
- [x] 页面刷新后状态恢复为 idle、token 用量清零
- [x] 多客户端连接时事件同步广播
