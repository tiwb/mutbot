# 消息元数据重构 设计规范

**状态**：✅ 已完成
**日期**：2026-02-28
**类型**：功能设计 + Bug修复

## 1. 背景

当前聊天界面的消息元数据存在三个问题：

### 1.1 Tool use 时间丢失

- `ToolGroupData` 中的 `startTime`/`endTime` 使用浏览器 `Date.now()`（毫秒时间戳）
- 这些值**没有被序列化到 session 持久化数据中**
- 历史恢复时 `startTime` 被设为 `0`，`endTime` 被设为 `0`，执行时间信息完全丢失
- 当前只有 turn 级最后一条 assistant text 消息才有时间显示，tool_group 消息没有时间元信息

**目标**：每个消息（不仅是每个 turn）都应有时间信息，tool_group 也应显示时间和执行耗时

### 1.2 Model 名称存储结构不合理

- 当前 model 名称在 `turn_timestamps` 数组中与时间信息混在一起
- model 是该轮对话的关键标识（影响头像、名称显示），不应仅作为时间元数据的附属字段
- 消息级 model 只在 `turn_done` 时回填到最后一条 assistant text 消息，tool_group 和其他 assistant 消息没有 model 信息

### 1.3 Agent 首次回复名称错误

- Agent 第一次回复时，在 `turn_done` 事件到达之前，消息的 `model` 字段为空
- 名称回退到 `agentDisplay.name`，该值来自 `currentModel` state
- `currentModel` 初始为空字符串 `""`，进而回退到 `agentDisplayBase.name`（默认 `"Agent"`）
- 虽然有 `rpc.call("session.get")` 异步获取初始 model，但存在时序竞争
- 也有 `token_usage` 事件会更新 `currentModel`，但 `token_usage` 在 `response_done` 时触发，此时文本已经开始显示了

**表现**：第一条 assistant 消息流式输出时名称显示为 "Agent"（或 session 的 display_name），直到 `response_done` 或 `turn_done` 事件到达后才更新为正确的模型名称

## 2. 设计方案

### 2.0 术语与核心理念

#### 术语

| 术语 | 说明 | 示例 |
|------|------|------|
| **消息**（ChatMessage） | 聊天流中的每个可显示项 | user text、assistant text、tool card、error、turn_start、turn_done |
| **Turn** | 一次完整的用户输入 → Agent 响应周期 | 从 turn_start 到 turn_done 之间的所有消息 |

#### 核心理念：Session 是数据源

**`chat_messages` 存储在 `AgentSession` 上**，是 session 持久化的唯一消息格式，替代当前的 `messages`（LLM 级）+ `turn_timestamps`（时间元数据）双结构。

```python
class AgentSession(Session):
    chat_messages: list[dict] = mutobj.field(default_factory=list)
    # ... 已有字段 ...
```

- `AgentSession` 拥有 `chat_messages`，数据生命周期与 session 一致
- `AgentBridge` 运行时通过 `self._session.chat_messages` 读写（不再维护独立副本）
- Bridge 创建/销毁不影响数据，持久化通过 `session.serialize()` 自然完成
- 前端历史恢复直接读取 `chat_messages`，无需复杂的索引匹配重建
- Agent 的 LLM messages 从 `chat_messages` 重建
- **未来目标**：用户通过管理 session 的 chat_messages 和 system_prompt 来控制 Agent 的行为
- **不需要考虑向后兼容**

#### Sender 概念

每条消息都有发送者标识，用户和 agent 使用不同字段：

| 消息类型 | 发送者字段 | 说明 |
|----------|-----------|------|
| user text | `sender: string` | 用户身份（默认 `"User"`，未来支持多用户） |
| assistant text | `model: string` | 生成该文本的模型（如 `"claude-sonnet-4-6"`） |
| tool_group | `model: string` | 发起该 tool call 的模型 |
| error | `model?: string` | 产生错误时的模型上下文（可选） |
| turn_start / turn_done | 无 | 结构标记，非内容消息 |

分开使用 `sender`（人类身份）和 `model`（技术标识）：语义清晰，未来 agent 可扩展 `display_name` 与 `model` 独立。

未来 session 可能在 turn 间切换 model，因此 model 必须是 per-message 而非 per-turn。

#### 消息 ID

每条 `chat_message` 都有持久化的 `id` 字段（后端生成，`uuid4().hex[:12]`），用于：
- 消息定位和引用
- 前端 React key（直接使用，不再 `crypto.randomUUID()`）
- 事件去重
- 未来的消息编辑/删除操作

### 2.1 ChatMessage 类型定义

```typescript
export type ChatMessage =
  | { id: string; role: "user"; type: "text"; content: string; timestamp?: string; sender?: string }
  | { id: string; role: "assistant"; type: "text"; content: string; timestamp?: string; durationMs?: number; model?: string }
  | { id: string; role: "assistant"; type: "tool_group"; data: ToolGroupData; timestamp?: string; durationMs?: number; model?: string }
  | { id: string; role: "assistant"; type: "error"; content: string; timestamp?: string; model?: string }
  | { id: string; type: "turn_start"; turnId: string; timestamp: string }
  | { id: string; type: "turn_done"; turnId: string; timestamp: string; durationSeconds: number };
```

变更点（vs 当前）：
- **新增**：user text 的 `sender`（替代无标识，默认 `"User"`）
- **删除**：user text 的 `turnId`（由 turn_start/turn_done 标记替代）
- **改名**：assistant text 的 `durationSeconds` → `durationMs`（毫秒精度统一）
- **新增**：tool_group 的 `timestamp`、`durationMs`、`model`（`durationMs` 由后端计算，前端直接显示）
- **新增**：error 的 `timestamp`、`model`
- **新增**：`turn_start` 类型（turn 开始标记，参与持久化）
- **新增**：`turn_done` 类型（turn 结束标记，参与持久化，客户端决定是否/如何显示）
- **`id` 来自后端**：持久化 ID，前端直接用作 React key，不再 `crypto.randomUUID()`

#### ToolGroupData 变更

```typescript
export interface ToolGroupData {
  toolCallId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  result?: string;
  isError?: boolean;
  startTime: string;      // ISO 时间戳（后端生成）
  endTime?: string;       // ISO 时间戳（后端生成）
}
```

- `startTime: number` → `startTime: string`（从浏览器 `Date.now()` 改为后端 ISO 时间戳）
- `endTime?: number` → `endTime?: string`

### 2.2 持久化结构

**替代当前的 `messages` + `turn_timestamps`**，改为单一的 `chat_messages` 数组。

消息级信息存在每条消息上，turn 级信息存在 turn_start/turn_done 上。

#### 2.2.1 完整数据范例

```json
{
  "id": "a1b2c3d4e5f6",
  "workspace_id": "ws_main",
  "title": "Agent 1",
  "type": "mutbot.session.AgentSession",
  "status": "active",
  "created_at": "2026-02-28T06:29:00Z",
  "updated_at": "2026-02-28T06:32:30Z",
  "config": {},
  "model": "claude-sonnet-4-6",
  "total_tokens": 15234,
  "context_used": 8500,
  "context_window": 200000,

  "chat_messages": [

    {
      "id": "m_a1b2c3d4",
      "type": "turn_start",
      "turn_id": "t_8f3a1b",
      "timestamp": "2026-02-28T14:30:00+08:00"
    },

    {
      "id": "m_e5f6a7b8",
      "type": "text",
      "role": "user",
      "content": "帮我查一下 Python 的最新版本",
      "timestamp": "2026-02-28T14:30:00+08:00",
      "sender": "User"
    },

    {
      "id": "m_c9d0e1f2",
      "type": "text",
      "role": "assistant",
      "content": "好的，让我帮你查一下。",
      "timestamp": "2026-02-28T14:30:02+08:00",
      "duration_ms": 1500,
      "model": "claude-sonnet-4-6"
    },

    {
      "id": "m_a3b4c5d6",
      "type": "tool_group",
      "tool_call_id": "toolu_01abc",
      "tool_name": "web_search",
      "arguments": {"query": "Python latest version 2026"},
      "result": "Python 3.14.0 was released on October 7, 2025...",
      "is_error": false,
      "timestamp": "2026-02-28T14:30:04+08:00",
      "duration_ms": 1850,
      "model": "claude-sonnet-4-6"
    },

    {
      "id": "m_e7f8a9b0",
      "type": "text",
      "role": "assistant",
      "content": "Python 最新版本是 **3.14.0**，发布于 2025 年 10 月。",
      "timestamp": "2026-02-28T14:30:06+08:00",
      "duration_ms": 2800,
      "model": "claude-sonnet-4-6"
    },

    {
      "id": "m_c1d2e3f4",
      "type": "turn_done",
      "turn_id": "t_8f3a1b",
      "timestamp": "2026-02-28T14:30:09+08:00",
      "duration_seconds": 9
    },

    {
      "id": "m_a5b6c7d8",
      "type": "turn_start",
      "turn_id": "t_c7d2e9",
      "timestamp": "2026-02-28T14:31:00+08:00"
    },

    {
      "id": "m_e9f0a1b2",
      "type": "text",
      "role": "user",
      "content": "帮我写一份 Python 3.14 新特性完整总结文档",
      "timestamp": "2026-02-28T14:31:00+08:00",
      "sender": "User"
    },

    {
      "id": "m_c3d4e5f6",
      "type": "text",
      "role": "assistant",
      "content": "好的，我来为你整理一份完整的新特性总结文档...",
      "timestamp": "2026-02-28T14:31:03+08:00",
      "duration_ms": 45000,
      "model": "claude-sonnet-4-6"
    },

    {
      "id": "m_a7b8c9d0",
      "type": "tool_group",
      "tool_call_id": "toolu_02def",
      "tool_name": "write_file",
      "arguments": {"path": "python314-features.md", "content": "# Python 3.14 ..."},
      "result": "File written successfully",
      "is_error": false,
      "timestamp": "2026-02-28T14:31:48+08:00",
      "duration_ms": 120,
      "model": "claude-sonnet-4-6"
    },

    {
      "id": "m_e1f2a3b4",
      "type": "text",
      "role": "assistant",
      "content": "文档已写入 `python314-features.md`。",
      "timestamp": "2026-02-28T14:31:49+08:00",
      "duration_ms": 1200,
      "model": "claude-sonnet-4-6"
    },

    {
      "id": "m_c5d6e7f8",
      "type": "turn_done",
      "turn_id": "t_c7d2e9",
      "timestamp": "2026-02-28T14:32:30+08:00",
      "duration_seconds": 90
    }
  ]
}
```

#### 2.2.2 与 LLM messages 的关系

`chat_messages` 是唯一持久化格式。Agent 恢复时，`_load_agent_messages()` 从 `chat_messages` 重建 LLM messages：

| chat_messages 类型 | → LLM Message |
|---|---|
| `text` + `role: "user"` | `Message(role="user", content=...)` |
| `text` + `role: "assistant"` | `Message(role="assistant", content=..., tool_calls=[...])` — 后续的 tool_group 合并为该消息的 tool_calls |
| `tool_group`（带 result） | 收集到 `ToolResult` 列表 → `Message(role="user", tool_results=[...])` |
| `turn_start` / `turn_done` | 跳过（结构标记） |
| `error` | 跳过（不参与 LLM 上下文） |

### 2.3 双层时间模型

时间信息分两层独立记录，互不依赖：

#### 消息级

**每条消息**有独立的 `timestamp`（开始时间）和 `duration_ms`（持续时间，可选）：

| 消息类型 | timestamp 来源 | duration_ms 来源 |
|----------|---------------|-----------------|
| user text | 后端 `send_message()` | 无 |
| assistant text | 后端首个 `text_delta` 时记录 | 后端 `response_done` 时计算 |
| tool_group | 后端 `tool_exec_start` 时记录 | 后端 `tool_exec_end` 时计算 |
| error | 前端创建时记录 | 无 |

#### Turn 级

Turn 信息存在标记消息上：

| 标记 | 字段 |
|------|------|
| `turn_start` | `turn_id`, `timestamp`（turn 开始时间） |
| `turn_done` | `turn_id`, `timestamp`（turn 结束时间）, `duration_seconds`（总耗时） |

Turn 时间 ≠ 消息时间之和。Turn 包含消息之间的等待、LLM 思考、网络延迟等。

### 2.4 后端变更

#### 2.4.1 AgentSession 持有 chat_messages

`AgentSession` 新增 `chat_messages` 字段：

```python
class AgentSession(Session):
    chat_messages: list[dict] = mutobj.field(default_factory=list)
    # ... model, system_prompt, total_tokens 等已有字段 ...
```

`AgentBridge` 通过 `self._session.chat_messages` 读写，随事件流实时追加/更新：

| 事件 | chat_messages 操作 |
|------|-------------------|
| `send_message()` (idle 时) | 追加 `turn_start`（含生成的 `id`） |
| `send_message()` | 追加 user `text`（含 `id`、`sender`） |
| 首个 `text_delta` | 追加 assistant `text`（含 `id`、`model`，content 为空，后续累加） |
| 后续 `text_delta` | 更新最后一条 assistant text 的 content |
| `response_done` | 更新最后一条 assistant text 的 `duration_ms` |
| `tool_exec_start` | 追加 `tool_group`（含 `id`、`model`、`timestamp`，无 result） |
| `tool_exec_end` | 更新对应 tool_group 的 result、is_error、`duration_ms` |
| `turn_done` | 追加 `turn_done`（含 `id`） |
| `error` | 追加 `error`（含 `id`） |

消息 `id` 由后端生成（`uuid4().hex[:12]`，带 `m_` 前缀）。

持久化通过 `session.serialize()` 自然包含 `chat_messages`。Bridge 不再维护独立副本。

#### 2.4.2 事件增强

后端事件注入新字段，前端直接读取（不再需要回填）：

**text_delta**（仅首个）注入 `timestamp`、`model`、`id`：
```json
{ "type": "text_delta", "text": "Hello", "timestamp": "...", "model": "claude-sonnet-4-6", "id": "m_c9d0e1f2" }
```

**response_done** 注入 `duration_ms`：
```json
{ "type": "response_done", "response": {...}, "duration_ms": 3200 }
```

**tool_exec_start** 注入 `timestamp`、`model`、`id`：
```json
{ "type": "tool_exec_start", "tool_call": {...}, "timestamp": "...", "model": "claude-sonnet-4-6", "id": "m_a3b4c5d6" }
```

**tool_exec_end** 注入 `timestamp` 和 `duration_ms`（后端计算，前端不算）：
```json
{ "type": "tool_exec_end", "tool_result": {...}, "timestamp": "...", "duration_ms": 1850 }
```

**user_message** 注入 `model`（当前 agent 的 model，修复首次回复名称问题）和 `id`：
```json
{ "type": "user_message", "text": "...", "timestamp": "...", "turn_id": "...", "model": "claude-sonnet-4-6", "sender": "User", "id": "m_e5f6a7b8" }
```

**turn_start**（新事件）：
```json
{ "type": "turn_start", "turn_id": "t_8f3a1b", "timestamp": "...", "id": "m_a1b2c3d4" }
```

**turn_done** 保持已有结构，新增 `id`：
```json
{ "type": "turn_done", "turn_id": "...", "timestamp": "...", "duration_seconds": 90, "id": "m_c5d6e7f8" }
```

#### 2.4.3 持久化变更

`serialize_session()`：包含 `chat_messages`（直接从 `session.chat_messages` 序列化）。

`_session_from_dict()`：恢复 `chat_messages` 到 AgentSession。

`_persist()`：不再从 bridge 取 messages/turn_timestamps，`session.serialize()` 已包含一切。

`_load_agent_messages()`：从 `session.chat_messages` 重建 LLM Messages（见 §2.2.2 映射表）。

`session.messages` RPC：返回 `session.chat_messages` 和 `agent_display`，前端直接使用。

### 2.5 前端变更

#### 2.5.1 事件处理简化

事件处理不再需要复杂的"回填"逻辑。时间、model、id 在事件中直接提供，前端只做显示：

| 事件 | 处理 |
|------|------|
| `turn_start` | 追加 turn_start 消息（用事件中的 id） |
| `user_message` | 追加 user text 消息（用事件中的 id、sender）；提取 model 更新 `currentModel` |
| `text_delta`（首个，带 timestamp+model+id） | 创建 assistant text 消息 |
| `text_delta`（后续） | 累加 content |
| `response_done` | 回填最后一条 assistant text 的 `durationMs` |
| `tool_exec_start` | 追加 tool_group 消息（用事件中的 id、timestamp、model） |
| `tool_exec_end` | 更新 tool_group 的 result 和 `durationMs`（后端已计算） |
| `turn_done` | 追加 turn_done 消息（用事件中的 id） |
| `error` | 追加 error 消息 |

#### 2.5.2 历史恢复简化

`session.messages` RPC 返回 `chat_messages`，前端直接映射为 `ChatMessage[]`：

```typescript
for (const cm of result.chat_messages) {
  switch (cm.type) {
    case "turn_start":
      restored.push({ id: cm.id, type: "turn_start", turnId: cm.turn_id, timestamp: cm.timestamp });
      break;
    case "text":
      if (cm.role === "user") {
        restored.push({ id: cm.id, role: "user", type: "text", content: cm.content, timestamp: cm.timestamp, sender: cm.sender });
      } else {
        restored.push({ id: cm.id, role: "assistant", type: "text", content: cm.content,
          timestamp: cm.timestamp, durationMs: cm.duration_ms, model: cm.model });
      }
      break;
    case "tool_group":
      restored.push({ id: cm.id, role: "assistant", type: "tool_group", timestamp: cm.timestamp, durationMs: cm.duration_ms, model: cm.model,
        data: { toolCallId: cm.tool_call_id, toolName: cm.tool_name, arguments: cm.arguments,
                result: cm.result, isError: cm.is_error, startTime: cm.timestamp } });
      break;
    case "turn_done":
      restored.push({ id: cm.id, type: "turn_done", turnId: cm.turn_id, timestamp: cm.timestamp, durationSeconds: cm.duration_seconds });
      break;
    // error 类似
  }
}
```

**对比当前**：删除全部 turn_timestamps 索引匹配逻辑和回填逻辑。ID 直接用后端的，不再 `crypto.randomUUID()`。

#### 2.5.3 Agent 名称显示

`agentName` 计算逻辑变为：

```typescript
const agentName = (msg.role === "assistant" && "model" in msg && msg.model)
  ? msg.model
  : agentDisplay.name;
```

首次回复名称问题修复：`user_message` 事件携带 `model` → 前端立即 `setCurrentModel(model)`。`text_delta` 首个事件直接携带 `model`，创建消息时就已正确。

### 2.6 turn_done 显示

`turn_done` 消息的显示规则由客户端决定：

- `durationSeconds < 60`：不显示
- `durationSeconds ≥ 60`：在消息流中显示为无气泡的行内文字（跟在前面气泡后面），格式为 `Worked for X minutes`

```
[Avatar] claude-sonnet-4-6
         ┌─────────────────────────────┐
    ◁    │ 文档已写入 python314.md。   │  1.2s · 14:31
         └─────────────────────────────┘
         Worked for 1 minute 30 seconds         ← turn_done（无气泡，无头像，行内文字）
```

样式：无气泡、无头像，淡色小字号，在 content-col 区域内显示（与 assistant 消息左对齐）。

### 2.7 时间显示规则

**每条消息右侧**显示 meta：

| 消息类型 | 格式 | 示例 |
|----------|------|------|
| user text | `时间` | `14:30` |
| assistant text（无 durationMs 或 < 10s） | `时间` | `14:30` |
| assistant text（durationMs ≥ 10s） | `✻ 耗时 · 时间` | `✻ 35s · 14:30` |
| tool_group（执行中） | `时间` | `14:30` |
| tool_group（已完成） | `耗时 · 时间` | `328ms · 14:30` |
| error | `时间` | `14:30` |
| turn_start | 无 meta | — |
| turn_done | 无 meta | — |

耗时格式化：`< 1000ms → 328ms`、`≥ 1000ms → 1.2s`、`≥ 60s → 1m 23s`

ToolCallCard 内部不再显示 duration（移除），统一到消息 meta。

### 2.8 连续消息气泡箭头

只有每组连续同角色消息的**第一个气泡**显示箭头，后续 continuation 消息不显示。

CSS：`.message-row.continuation .message-bubble::before, .message-row.continuation .message-bubble::after { display: none; }`

## 3. 待定问题

（已全部确认）

## 4. 实施步骤清单

### 阶段一：后端 — AgentSession chat_messages 与事件增强 [✅ 已完成]

- [x] **Task 1.1**: AgentSession 新增 chat_messages 字段
  - [x] `AgentSession` 新增 `chat_messages: list[dict] = mutobj.field(default_factory=list)`
  - [x] `serialize_session()` 包含 `chat_messages`
  - [x] `_session_from_dict()` 恢复 `chat_messages`
  - 状态：✅ 已完成

- [x] **Task 1.2**: AgentBridge 通过 session 读写 chat_messages
  - [x] 删除 `self.turn_timestamps`，改用 `self._session.chat_messages`
  - [x] 新增 `_gen_msg_id()` 模块级函数（`"m_" + uuid4().hex[:10]`）
  - [x] 新增 response 级状态追踪：`_response_first_delta`, `_response_start_ts`, `_response_start_mono`
  - [x] 新增 tool 时间追踪：`_tool_start_times: dict[str, tuple[str, float]]`
  - [x] `send_message()`：idle 时追加 turn_start（含 id）；追加 user text（含 id、sender）
  - [x] `text_delta`（首个）：追加 assistant text（含 id、timestamp、model）
  - [x] `text_delta`（后续）：更新最后一条 assistant text 的 content
  - [x] `response_done`：更新最后一条 assistant text 的 duration_ms
  - [x] `tool_exec_start`：追加 tool_group（含 id、timestamp、model）
  - [x] `tool_exec_end`：更新对应 tool_group 的 result、is_error、duration_ms（后端计算）
  - [x] `turn_done`：追加 turn_done（含 id）
  - [x] `error`：追加 error（含 id）
  - 状态：✅ 已完成

- [x] **Task 1.3**: 事件注入时间、model 和 id
  - [x] 首个 text_delta 注入 `timestamp`、`model`、`id`
  - [x] response_done 注入 `duration_ms`
  - [x] tool_exec_start 注入 `timestamp`、`model`、`id`
  - [x] tool_exec_end 注入 `timestamp`、`duration_ms`（后端计算）
  - [x] user_message 注入 `model`、`sender`、`id`
  - [x] 新增 turn_start 事件广播（含 `id`）
  - [x] turn_done 事件注入 `id`
  - 状态：✅ 已完成

- [x] **Task 1.4**: 持久化和恢复切换
  - [x] `_persist()`：不再从 bridge 取 turn_timestamps（session.serialize() 已含 chat_messages）
  - [x] `_load_agent_messages()`：从 `session.chat_messages` 重建 LLM Messages（`_rebuild_llm_messages`）
  - [x] `start()`：不再单独加载 turn_timestamps
  - 状态：✅ 已完成

- [x] **Task 1.5**: session.messages RPC 更新
  - [x] 返回 `session.chat_messages` 代替 `messages` + `turn_timestamps`
  - 状态：✅ 已完成

### 阶段二：前端 — 类型、事件处理、历史恢复 [✅ 已完成]

- [x] **Task 2.1**: ChatMessage 类型重构
  - [x] 更新 ChatMessage union type（新增 turn_start、turn_done、sender；删除 user turnId；text durationSeconds → durationMs；tool_group 新增 timestamp/durationMs/model）
  - [x] ToolGroupData: startTime/endTime 从 number 改为 string
  - [x] tool_group 和 error 消息新增 timestamp、model
  - [x] 前端优先使用后端 id，回退到 `crypto.randomUUID()`
  - 状态：✅ 已完成

- [x] **Task 2.2**: AgentPanel 事件处理重写
  - [x] turn_start → 追加 turn_start 消息（用事件 id）
  - [x] user_message → 追加 user text（用事件 id、sender）；提取 model 更新 currentModel
  - [x] text_delta（首个，带 timestamp+model+id）→ 创建 assistant text
  - [x] response_done → 回填 durationMs
  - [x] tool_exec_start → 追加 tool_group（用事件 id、timestamp、model）
  - [x] tool_exec_end → 更新 tool_group 的 result 和 durationMs（直接用后端计算值）
  - [x] turn_done → 追加 turn_done 消息（用事件 id）
  - [x] error → 追加 error 消息
  - [x] 删除旧的 turn_done 回填 model/timestamp/durationSeconds 逻辑
  - 状态：✅ 已完成

- [x] **Task 2.3**: 历史恢复重写
  - [x] 从 `result.chat_messages` 直接映射为 `ChatMessage[]`（`restoreChatMessages` 函数）
  - [x] 删除旧的 turn_timestamps 索引匹配和回填逻辑
  - 状态：✅ 已完成

### 阶段三：前端 — 显示与 UI [✅ 已完成]

- [x] **Task 3.1**: turn_start / turn_done 渲染
  - [x] turn_start：不渲染（`return null`）
  - [x] turn_done（durationSeconds ≥ 60）：无气泡行内文字 "Worked for X minutes"
  - [x] turn_done（durationSeconds < 60）：不显示
  - [x] CSS 样式：淡色小字号（`.turn-done-text`），content-col 内左对齐
  - 状态：✅ 已完成

- [x] **Task 3.2**: renderMeta 扩展
  - [x] tool_group 消息：已完成 `耗时 · 时间`（durationMs 直接从消息读取），执行中 `时间`
  - [x] error 消息：`时间`
  - [x] assistant text: durationMs 适配（≥10s 显示 ✻ 耗时 · 时间）
  - [x] 新增 `formatDurationMs()` 函数（ms/s/m 级）
  - 状态：✅ 已完成

- [x] **Task 3.3**: ToolCallCard 移除内部 duration
  - [x] 移除 header 中的 duration 元素
  - [x] 清理相关 CSS（`.tool-card-duration` 已删除）
  - [x] ToolCallCard 用 `result === undefined` 判断 running 状态（不再依赖 endTime）
  - 状态：✅ 已完成

- [x] **Task 3.4**: agentName 逻辑更新
  - [x] 优先使用消息自身的 model 字段（所有 assistant 消息类型）
  - [x] 回退到 currentModel → agentDisplayBase.name
  - [x] tool_group 和 error 消息也参与 agentName 计算
  - 状态：✅ 已完成

- [x] **Task 3.5**: 连续消息箭头修正
  - [x] continuation 消息隐藏气泡箭头（CSS `.message-row.continuation .message-bubble::before/::after`）
  - [x] continuation 判断跳过 turn_start/turn_done 标记消息
  - 状态：✅ 已完成

---

### 实施进度总结
- ✅ **阶段一：后端** - 100% 完成 (5/5 任务)
- ✅ **阶段二：前端类型/事件** - 100% 完成 (3/3 任务)
- ✅ **阶段三：前端 UI** - 100% 完成 (5/5 任务)

**核心功能完成度：100%** (13/13 任务)
**TypeScript 编译：通过**
**生产构建：通过**

## 5. 测试验证

### 手动测试
- [ ] user 消息显示时间，带 sender 字段
- [ ] assistant text 流式输出开始时就有正确的名称和时间
- [ ] assistant text 的 durationMs 在 response_done 后显示
- [ ] tool_group 消息显示时间和执行耗时（后端计算的 durationMs）
- [ ] error 消息显示时间
- [ ] Turn ≥ 60s 时 turn_done 显示 "Worked for X minutes"
- [ ] Turn < 60s 时 turn_done 不显示
- [ ] turn_start 不显示
- [ ] 重启后历史恢复：所有消息时间、耗时、model/sender 正确
- [ ] 重启后历史恢复：tool_group 执行耗时正确
- [ ] 重启后历史恢复：turn_done 提示正确
- [ ] 重启后历史恢复：消息 ID 保持一致（后端 ID，非前端随机）
- [ ] ToolCallCard 内部不再显示耗时
- [ ] 首次 assistant 消息名称正确
- [ ] 每条 assistant 消息都有独立的 model
- [ ] 连续同角色消息只有第一个气泡有箭头
- [ ] 切换 model 后新消息名称正确
- [ ] 持久化文件格式符合 §2.2.1 范例结构（含 id、sender、duration_ms）
- [ ] Agent LLM messages 从 chat_messages 正确重建
