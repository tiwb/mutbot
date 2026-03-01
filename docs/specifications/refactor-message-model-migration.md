# mutbot 迁移指南：mutagent Message 模型重构

**状态**：📝 设计中
**日期**：2026-03-01
**类型**：重构

## 背景

mutagent 完成了 Message 模型的破坏性重构（`feature-message-model.md`），旧类型已移除。mutbot 作为 mutagent 的上层消费者，需要适配不兼容变更。

本次迁移**不做数据兼容**——不保留旧格式、旧路径、旧迁移逻辑。同时借此机会解决 mutagent 设计文档明确指出的 mutbot 架构问题：双重消息存储。

### mutagent 不兼容变更

| 变更 | 旧 | 新 |
|------|----|---|
| Message 内容 | `content: str`, `tool_calls`, `tool_results` | `blocks: list[ContentBlock]` |
| 工具调用/结果 | `ToolCall` + `ToolResult` 独立类型 | `ToolUseBlock`（合并调用与结果，原地更新） |
| Agent 字段 | `client`, `tool_set`, `system_prompt`, `messages`, `max_tool_rounds` | `llm`, `tools`, `context: AgentContext` |
| Provider.send() | `system_prompt: str` 参数 | `prompts: list[Message]` 参数 |
| StreamEvent | `tool_result` 字段（tool_exec_end） | 移除；复用 `tool_call` 字段传递已完成 ToolUseBlock |
| StreamEvent | 无 `response_start` | 新增 `response_start` 事件（携带预生成的 Message 元数据） |
| ToolSet.dispatch() | 返回 `ToolResult` | 返回 None，原地更新 ToolUseBlock |

### mutagent 新增类型

- `ContentBlock` — 基类（`type: str`）
- `TextBlock` — 文本（`text: str`）
- `ImageBlock` — 图片（`data: str`, `media_type: str`, `url: str`）
- `DocumentBlock` — 文档（`data: str`, `media_type: str`）
- `ThinkingBlock` — 推理（`thinking: str`, `signature: str`, `data: str`）
- `ToolUseBlock` — 工具调用+结果（`id`, `name`, `input`, `status`, `result`, `is_error`, `duration`）
- `AgentContext` — 上下文管理（`context_window`, `prompts`, `messages`，prepare/usage 方法）

### Message 新字段

`id`, `label`, `sender`, `model`, `timestamp`, `duration`, `input_tokens`, `output_tokens`, `cacheable`, `priority`

---

## 设计方案

### 核心变更：消除双重存储

mutagent 设计文档（`feature-message-model.md` "设计验证：消除应用层双重存储"一节）指出：

> 当前 mutbot 维护两套消息格式：`mutagent.Message`（LLM 格式，4 个字段）和 `chat_messages: list[dict]`（UI 格式）。导致 `_rebuild_llm_messages()` 80+ 行重建逻辑、AgentBridge 200+ 行消息构建状态机、双重序列化。
>
> 新 Message 的设计目标：**应用层直接使用 `AgentContext.messages` 作为唯一存储**。

新 Message 已覆盖 chat_messages 的所有字段：

| mutbot chat_messages 字段 | 新 Message 对应 |
|---|---|
| id | Message.id |
| type (text/tool_group/...) | blocks 内容隐含 |
| role | Message.role |
| content | TextBlock.text |
| timestamp | Message.timestamp |
| model | Message.model |
| sender | Message.sender |
| duration_ms | Message.duration |
| tool_call_id / tool_name / arguments | ToolUseBlock |
| result / is_error | ToolUseBlock.result / .is_error |
| turn_id / turn_start / turn_done | TurnStartBlock / TurnEndBlock |

**迁移方向**：`AgentContext.messages` 作为唯一消息存储，消除 `chat_messages` 持久化和 `_rebuild_llm_messages()`。

### mutagent 前置变更（✅ 已完成）

以下变更由 `mutagent/docs/specifications/refactor-agent-run.md` 完成：

- **InputEvent 删除** — agent.run() 输入流从 `AsyncIterator[InputEvent]` 改为 `AsyncIterator[Message]`
- **TurnStartBlock / TurnEndBlock** — 新 ContentBlock 子类，输入 Message 含 TurnStartBlock 时触发处理
- **response_start 事件** — 新 StreamEvent 类型，step() 前 yield，携带预生成的 Message 元数据（id, model, timestamp）
- **Message 元数据** — agent.run() 设置 id/timestamp/model/duration/tokens；user Message 由应用层构建完整 Message 直接传入
- **中断清理** — agent.run() finally 块：提交 partial text、标记未完成 ToolUseBlock
- **StreamEvent.turn_id** — turn_done 事件携带 turn_id

完成后 `context.messages` 自包含（元数据完整），应用层无需计算或注入任何元数据。

### 前端保持 flat ChatMessage

前端保持当前的 flat `ChatMessage` 联合类型设计。后端发送序列化 `Message[]` blocks 格式，前端 `restoreChatMessages()` 展开 blocks 为 flat `ChatMessage[]`。

前端流式协议变为：
```
response_start(id, model, timestamp) → 创建消息卡片
text_delta(text) → 追加文本
tool_use_start/delta/end → 工具调用构建
response_done(duration, tokens) → 完成本次 LLM 调用
tool_exec_start/end → 工具执行
... (可能多轮 response_start → response_done)
turn_done(turn_id, duration) → 整轮结束
```

### AgentBridge 重新设计：纯事件转发

当前 AgentBridge 同时构建两套格式：agent.messages（LLM 格式）和 chat_message dict（WebSocket + 持久化格式），并计算所有元数据。这是"不该处理的事务"的核心。

消除 chat_messages 持久化和元数据计算后，bridge 变成纯事件转发层。

**消除的逻辑**：
- `_pending_text: list[str]` — mutagent agent.run() 内部追踪
- `_pending_tool_calls: list[ToolCall]` — agent 内部已管理 ToolUseBlock 生命周期
- `_completed_results: list[ToolResult]` — 结果在 ToolUseBlock 上原地更新
- `_response_committed: bool` — 不再需要手动判断
- `_response_first_delta: bool` — 被 `response_start` 事件替代
- `_response_start_ts` / `_response_start_mono` — agent 计算 duration
- `_tool_start_times: dict` — agent 已计算 ToolUseBlock.duration
- 手动构建 `Message(role="assistant", ...)` / `Message(role="user", ...)` — agent.run() 内部已管理
- **chat_message dict 构建**（6 个 `_handle_*` 方法）— 不再需要
- **元数据计算**（`_gen_msg_id`, `_local_iso_now`, `_get_model`, `monotonic` 差值）— agent 内部计算
- **`_commit_partial_state()` 中断恢复** — 移至 mutagent agent.run() 内部处理

**bridge 新职责（极简）**：
1. 接收用户输入 → 构建完整 Message（含 id/timestamp/sender + TurnStartBlock）→ 入队
2. 监听 agent.run() 的 StreamEvents → serialize → 转发 WebSocket（不计算、不注入）
3. 持久化 → 触发 session_impl 序列化 `agent.context.messages`

中断时 bridge 只需取消 task + 广播状态。agent.run() 的 `finally` 块保证 `context.messages` 始终处于有效状态（详见 `mutagent/docs/specifications/refactor-agent-run.md`）。

### 持久化重新设计

**当前流程**：
1. AgentBridge 构建 chat_messages (UI dict) + agent.messages (Message) — 双重构建
2. 持久化：chat_messages → session JSON，agent.messages → 分别序列化
3. 恢复：chat_messages → `_rebuild_llm_messages()` 80+ 行 → 重建 agent.messages

**新流程**：
1. agent 内部管理 context.messages — 唯一消息存储
2. 持久化：直接序列化 `agent.context.messages` → JSON
3. 恢复：反序列化 JSON → Message 列表 → 传入 AgentContext

`_rebuild_llm_messages()` 整个删除。`chat_messages` 作为持久化格式消除。

**prompts 不需要持久化**——它们是配置级的，每次创建 Agent 时从 session 配置重建。

### 前端影响分析

#### 流式事件协议

流式事件结构调整：

| 事件 | 变更 |
|------|------|
| **response_start** | **新增** — 携带 Message 元数据（id, model, timestamp），前端用于创建消息卡片 |
| text_delta | 无变化（不再由 bridge 注入 id/timestamp/model，这些在 response_start 中） |
| tool_exec_start | `tool_call.arguments` → `tool_call.input` |
| tool_exec_end | **`event.tool_result`** → **`event.tool_call`**（已完成的 ToolUseBlock，含 duration） |
| response_done | 携带 duration, input_tokens, output_tokens（agent 计算） |
| turn_done | 无变化 |

前端需调整的具体字段（`AgentPanel.tsx`）：

```
新增 response_start 处理:
  data.id, data.model, data.timestamp → 创建消息卡片

text_delta:
  不再从首个 text_delta 提取 id/model/timestamp（改从 response_start 获取）

tool_exec_start:
  data.tool_call.arguments  →  data.tool_call.input

tool_exec_end:
  data.tool_result.tool_call_id  →  data.tool_call.id
  data.tool_result.content       →  data.tool_call.result
  data.tool_result.is_error      →  data.tool_call.is_error
  data.tool_call.duration        →  工具耗时（agent 已计算，不再由 bridge 注入 duration_ms）

response_done:
  data.duration, data.input_tokens, data.output_tokens  →  更新消息卡片元数据
```

`ToolGroupData` 接口（`ToolCallCard.tsx`）需同步更新字段名。

#### 会话恢复协议

当前前端通过 `session.messages` RPC 获取 `chat_messages: list[dict]`，在 `restoreChatMessages()` 中重建 `ChatMessage[]` state。

消除 chat_messages 后，后端改为发送序列化的 `Message[]` blocks 格式。前端的 `restoreChatMessages()` 改为从 blocks 展开为 `ChatMessage[]`：

```
Message(role="user", blocks=[TextBlock(text="hello")])
  → { role: "user", type: "text", content: "hello" }

Message(role="assistant", blocks=[
    TextBlock(text="让我查一下"),
    ToolUseBlock(id="t1", name="search", input={...}, status="done", result="...")
])
  → { role: "assistant", type: "text", content: "让我查一下" }
  → { role: "assistant", type: "tool_group", data: { toolCallId: "t1", ... } }
```

Message 的元数据字段（id, timestamp, model, sender, duration）直接映射到 ChatMessage 对应字段。

### copilot/provider.py 依赖验证

mutbot 的 `copilot/provider.py` 直接导入 mutagent 内部函数：

```python
from mutagent.builtins.openai_provider import (
    _messages_to_openai, _tools_to_openai, _send_no_stream, _send_stream,
)
```

**验证结果**：mutagent 的 `_messages_to_openai()` 已完全适配 blocks 模型：
- TextBlock → content string 或 content array
- ToolUseBlock → `tool_calls` 字段 + `role:"tool"` 结果消息（仅 status="done"）
- ImageBlock → `image_url` 格式
- ThinkingBlock → 忽略

mutbot copilot provider 可继续复用这些函数，只需适配 `send()` 签名和 prompts 处理。

---

## 受影响文件分析

### 1. `src/mutbot/web/agent_bridge.py` — 影响：高（概念重新设计）

**当前用法**：
- 导入 `ToolCall`, `ToolResult`, `Message`, `StreamEvent`, `InputEvent`（line 16）— InputEvent 已删除，需改为 `Message`, `StreamEvent`, `TextBlock`, `TurnStartBlock` 等
- `_pending_text: list[str]`（line 78）、`_pending_tool_calls: list[ToolCall]`（line 79）、`_completed_results: list[ToolResult]`（line 80）、`_response_committed: bool`（line 81）
- `_track_event()` 中 `event.tool_result` 追加到 `_completed_results`（line 182）
- 手动构建 `Message(role="assistant", content=..., tool_calls=[...])`（lines 207-210）
- 手动构建 `Message(role="user", tool_results=results)`（lines 229, 248）
- `_commit_partial_state()` — 中断恢复 60 行（lines 196-255）
- `_chat_messages` 访问/构建/查找方法（lines 96-118）
- `_handle_text_delta/response_done/tool_exec_start/tool_exec_end/turn_done/error` — chat_message dict 构建（lines 262-410）
- `self.agent.client` — context_window（line 143）、model（lines 159, 260）
- `self.agent.messages` — append（lines 207, 229, 248）、read（lines 234-235）

**迁移方案**：
- **删除** `_pending_text`、`_pending_tool_calls`、`_completed_results`、`_response_committed` — 全部由 mutagent agent.run() 内部管理
- **删除** `_commit_partial_state()` — 中断清理移至 mutagent agent.run() finally 块
- **删除** `_track_event()` — 不再需要追踪飞行中状态
- **删除** `_chat_messages` 相关方法（`_append_chat_message`, `_find_chat_message`, `_find_last_chat_message`）
- **删除** `_response_first_delta`、`_response_start_ts`、`_response_start_mono` — 被 `response_start` 事件替代
- **删除** `_tool_start_times` — agent 已计算 ToolUseBlock.duration
- **简化** `_handle_*` 事件处理方法 — 从"serialize + chat_message 构建 + 元数据计算"变为纯 serialize + forward
- **新增** `response_start` 事件处理 — 直接转发（元数据已在事件中）
- `self.agent.client` → `self.agent.llm`
- `self.agent.messages` → `self.agent.context.messages`
- `event.tool_result` → `event.tool_call`

### 2. `src/mutbot/runtime/session_impl.py` — 影响：高（持久化重新设计）

**当前用法**：
- `_rebuild_llm_messages(chat_messages)` — 从 UI dict 重建 Message，80+ 行（lines 184-266）
- Agent 构造：`Agent(client=client, tool_set=tool_set, system_prompt=..., messages=...)`（line 428）
- `_persist()` 中序列化 `agent.messages`（line 501）
- `_load_agent_messages()` 从 chat_messages 恢复（line 520）
- `agent.client` 热切换（line 560）

**迁移方案**：
- **删除** `_rebuild_llm_messages()` — 不再需要
- Agent 构造：`Agent(llm=client, tools=tool_set, context=AgentContext(prompts=[...], messages=[...]))`
- 持久化：序列化 `agent.context.messages` → JSON（使用 serializers 的 serialize_message）
- 恢复：反序列化 JSON → Message 列表 → 传入 AgentContext（新增 deserialize_message）
- `agent.client` → `agent.llm`（热切换同理）
- `agent.messages` → `agent.context.messages`

### 3. `src/mutbot/web/serializers.py` — 影响：高（重写）

**当前用法**：
- `serialize_tool_call(tc: ToolCall)` → `{id, name, arguments}`（line 18）
- `serialize_tool_result(tr: ToolResult)` → `{tool_call_id, content, is_error}`（line 22）
- `serialize_message(msg: Message)` → `{role, content, tool_calls, tool_results}`（line 30）
- `serialize_stream_event(event)` → 含 `event.tool_result` 访问（line 58）

**迁移方案**：
- 移除 `serialize_tool_call()` 和 `serialize_tool_result()`
- 新增 `serialize_block(block: ContentBlock) -> dict` — 按 block.type 分发
- 重写 `serialize_message(msg: Message) -> dict` — 遍历 blocks + 元数据（id, timestamp, model, sender, duration 等）
- 新增 `deserialize_message(data: dict) -> Message` — 从 JSON 恢复 Message（含 blocks 反序列化，持久化恢复用）
- `serialize_stream_event()` — `tool_exec_end` 使用 `event.tool_call` 替代 `event.tool_result`

### 4. `src/mutbot/builtins/setup_provider.py` — 影响：中

**当前用法**：
- `send(model, messages, tools, system_prompt="", stream=True)`（line 115）
- `msg.content` 访问用户输入（line 134）
- `Message(role="assistant", content=text)` 构建响应（line 149）

**迁移方案**：
- `send()` 签名：`system_prompt: str` → `prompts: list[Message] | None = None`
- `msg.content` → 遍历 `msg.blocks`，取第一个 TextBlock.text
- `Message(role="assistant", content=text)` → `Message(role="assistant", blocks=[TextBlock(text=text)])`
- StreamEvent 构造无需改动

### 5. `src/mutbot/copilot/provider.py` — 影响：中

**当前用法**：
- `send(model, messages, tools, system_prompt="", stream=True)`（line 58）
- 导入 mutagent 内部函数 `_messages_to_openai`, `_tools_to_openai`, `_send_no_stream`, `_send_stream`（line 6）
- `system_prompt` 插入到 openai_messages 头部（line 69）

**迁移方案**：
- `send()` 签名：`system_prompt: str` → `prompts: list[Message] | None = None`
- `_messages_to_openai()` — **已适配 blocks，可直接复用**，无需改动
- prompts 处理：参考 mutagent `OpenAIProvider.send()`（lines 56-61）— 遍历 prompts（reversed），提取 TextBlock.text，插入为 `{role: "system", content: ...}` 到 openai_messages 头部

### 6. `src/mutbot/builtins/guide.py` — 影响：中

**当前用法**：
- `Agent(client=client, tool_set=tool_set, system_prompt=..., messages=...)`（line 80）

**迁移方案**：
```python
Agent(
    llm=client,
    tools=tool_set,
    context=AgentContext(
        prompts=[Message(role="system", blocks=[TextBlock(text=system_prompt)], label="base")],
        messages=messages or [],
    ),
)
```

### 7. `src/mutbot/session.py` — 影响：低

TYPE_CHECKING 导入，类型标注自动适配新 Message 定义，可能无需改动。

---

## 测试文件

### `tests/test_session_persistence.py` — 影响：高

- `_make_messages()` 中 ToolCall/ToolResult → blocks + ToolUseBlock
- `agent.messages` → `agent.context.messages`
- 持久化验证逻辑适配新序列化格式（serialize_message / deserialize_message）

### `tests/test_setup_provider.py` — 影响：中

- `Message(role="user", content=text)` → `Message(role="user", blocks=[TextBlock(text=text)])`
- `provider.send()` 签名变更

---

## 设计目标评估

mutagent 重构的目标：**让 mutbot 极度简化，不处理不该处理的事务**。

### 消除的职责

| 当前 mutbot 职责 | 代码量 | 迁移后 |
|---|---|---|
| 手动构建 assistant/user Message | ~50 行 | **消除** — agent.run() 内部管理 |
| 手动构建 tool_result Message | ~20 行 | **消除** — Provider 自动生成 |
| `_rebuild_llm_messages()` | ~80 行 | **消除** — 不再需要双向转换 |
| chat_message dict 构建（6 个 `_handle_*` 方法） | ~150 行 | **消除** — 直接序列化 StreamEvent |
| `_pending_tool_calls` + `_completed_results` 状态机 | ~30 行 | **消除** — agent 内部管理 |
| 中断恢复状态机 (`_commit_partial_state`) | ~60 行 | **消除** — 移至 mutagent agent.run() finally 块 |
| 元数据计算（id, timestamp, model, duration） | ~40 行 | **消除** — agent 内部计算，context.messages 自包含 |

### mutbot 剩余职责（合理的应用层关注点）

| 职责 | 说明 |
|---|---|
| 用户输入 → agent input_stream | 构建完整 Message（含 id/timestamp/sender + TurnStartBlock）→ 入队 |
| StreamEvent → WebSocket 转发 | 纯序列化 + 转发（不计算、不注入元数据） |
| 中断信号 | 取消 task + 广播状态（agent.run() 自行清理 context.messages） |
| 持久化触发 | 应用层存储决策 |
| 前端 Message → ChatMessage 展开 | 前端渲染决策（frontend 内部） |

### 前端消息转换的定性

前端从 `Message[]` blocks 展开为 flat `ChatMessage[]` 是**前端渲染层的职责**，不是"不该处理的转换"。类比：React 组件从 props 数据映射到 DOM 是渲染的本职工作。后端不做这个转换——后端只发送 `Message[]`，前端自行决定如何展示。

---

## 实施影响评估

### 代码量变化预估

| 文件 | 删除 | 新增/修改 | 净变化 | 说明 |
|------|------|-----------|--------|------|
| `agent_bridge.py` | ~280 行 | ~30 行 | **-250** | 移除全部飞行中状态 + chat_message 构建 + 元数据计算 + 中断恢复 |
| `session_impl.py` | ~100 行 | ~30 行 | **-70** | 删除 `_rebuild_llm_messages()`，简化持久化 |
| `serializers.py` | ~50 行 | ~80 行 | **+30** | 重写序列化 + 新增反序列化（含 blocks 分发）|
| `setup_provider.py` | ~5 行 | ~10 行 | **+5** | 签名 + blocks 构造 |
| `copilot/provider.py` | ~5 行 | ~10 行 | **+5** | 签名 + prompts 处理 |
| `guide.py` | ~3 行 | ~8 行 | **+5** | Agent 构造 |
| `session.py` | 0 | ~2 行 | **+2** | 类型导入（可能无需改动） |
| 测试文件 | ~40 行 | ~50 行 | **+10** | 适配新模型 |
| **前端** | ~15 行 | ~60 行 | **+45** | response_start 处理 + 字段调整 + Message→ChatMessage 展开 |
| **mutagent** | 0 | ~50 行 | **+50** | TurnBlock + response_start 事件 + 元数据设置 + 中断清理 |
| **合计** | **~498 行** | **~330 行** | **-168** | 净减少代码 |

### 风险点

**低风险**（机械替换，容易验证）：
- Provider 签名变更（setup_provider, copilot/provider）— 编译即可发现
- Agent 构造变更（guide, session_impl）— 编译即可发现
- 导入替换（ToolCall → ToolUseBlock 等）— 编译即可发现

**中风险**（逻辑变更，需要功能测试）：
- serializers 重写 — 序列化/反序列化是持久化的基础，round-trip 正确性关键
- 前端 Message→ChatMessage 展开 — 新增的 `restoreChatMessages()` 逻辑，需覆盖各种 block 组合（含 TurnStartBlock/TurnEndBlock）
- 前端 tool_exec_end 字段调整 — `tool_result` → `tool_call`，TypeScript 编译可捕获大部分

**高风险**（架构变更，需要端到端测试）：
- AgentBridge 消除飞行中状态 — 当前是 200+ 行状态机，改为依赖 agent 内部管理。需要验证 bridge 极简化后事件流和持久化的正确性
- 持久化 round-trip — 新格式 serialize → 持久化 → deserialize → AgentContext → agent.run() 全链路正确性
- mutagent agent.run() 中断清理 — finally 块在各中断场景（LLM 流中断、工具执行中断、并行工具部分完成、GeneratorExit）下的正确性

### 不影响的部分

- **LLM 调用链**：mutagent agent.run() → context.prepare_*() → llm.send_message() → Provider.send() — 这部分由 mutagent 内部处理，mutbot 不直接参与
- **工具注册和发现**：ToolSet auto_discover 机制不变
- **WebSocket 连接管理**：底层连接、重连、事件广播基础设施不变
- **配置系统**：Config 读取、provider 解析、model 切换逻辑不变（只是 `agent.client` → `agent.llm`）

### 实施顺序建议

```
Phase 0: mutagent 前置 ✅ 已完成
  → mutagent/docs/specifications/refactor-agent-run.md
  InputEvent 删除、TurnBlock、response_start、元数据、中断清理 — 全部完成

Phase 1: mutbot 基础层（无运行时依赖，可独立测试）
  serializers.py → setup_provider.py → copilot/provider.py → guide.py

Phase 2: mutbot 核心层（依赖 Phase 1 的序列化）
  session_impl.py → agent_bridge.py → session.py

Phase 3: 测试 + 前端
  test_session_persistence.py → test_setup_provider.py → 前端字段调整 + Message→ChatMessage 展开
```

---

## 关键参考

### 源码（mutbot）

- `mutbot/src/mutbot/web/agent_bridge.py` — 流式事件处理 + 消息构建状态机（核心重构对象）
- `mutbot/src/mutbot/runtime/session_impl.py:184` — `_rebuild_llm_messages()`（将被删除）
- `mutbot/src/mutbot/web/serializers.py` — 序列化层（将被重写）
- `mutbot/src/mutbot/copilot/provider.py:6` — 导入 mutagent 内部函数（已验证兼容）
- `mutbot/frontend/src/panels/AgentPanel.tsx:61` — 前端事件处理（response_start + tool 字段调整）
- `mutbot/frontend/src/components/ToolCallCard.tsx:3` — `ToolGroupData` 接口（字段名调整）
- `mutbot/frontend/src/components/MessageList.tsx:9` — `ChatMessage` 类型定义

### 源码（mutagent — 前置依赖）

- `mutagent/src/mutagent/messages.py` — Message/ContentBlock/StreamEvent 定义
- `mutagent/src/mutagent/builtins/agent_impl.py` — agent.run() 主循环
- `mutagent/src/mutagent/context.py` — AgentContext
- `mutagent/src/mutagent/builtins/openai_provider.py` — `_messages_to_openai()` 已适配 blocks

### 相关规范

- **`mutagent/docs/specifications/refactor-agent-run.md`** — mutagent 前置变更（✅ 已完成：InputEvent 删除 + TurnBlock + response_start + 元数据 + 中断清理）
- `mutagent/docs/specifications/feature-message-model.md` — Message 模型设计（已完成）
- `mutagent/docs/specifications/feature-multichat.md` — Turn 完整设计（后续展开）
