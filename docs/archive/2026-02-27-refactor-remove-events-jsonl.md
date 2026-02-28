# 移除 `.events.jsonl` 以减少存储开销

**状态**：✅ 已完成
**日期**：2026-02-27
**类型**：重构

## 1. 背景

每个 session 在 `~/.mutbot/sessions/` 下会产生两个持久化文件：

| 文件 | 内容 | 增长特征 |
|------|------|---------|
| `{prefix}.json` | session 元数据 + messages | 每轮对话结束更新一次（整体覆写） |
| `{prefix}.events.jsonl` | 全量流式事件（text_delta、tool_use_start、tool_exec_end、token_usage 等） | 每个 streaming token 追加一行，增长极快 |

`.events.jsonl` 记录了细粒度的流式事件（包括每个 text_delta token），文件体积远大于 `.json`。而 session 恢复的核心数据（messages）已经独立保存在 `.json` 中。

`.events.jsonl` 目前有两处消费者：
1. **前端历史回放**：WebSocket 连接后，若前端无本地缓存，通过 `session.events` RPC 拉取全量事件回放以重建 UI
2. **Token 计数恢复**：后端重启 session 时从最后一条 `token_usage` 事件恢复 `session_total_tokens`

这两个需求可以用更轻量的方式满足，不需要持久化全量流式事件。

## 2. 设计方案

### 2.1 移除 `.events.jsonl` 写入

- 删除 `storage.append_session_event()` 和 `storage.load_session_events()`
- 删除 `SessionManager.record_event()` 和 `SessionManager.get_session_events()`
- 删除 `AgentBridge` 中的 `event_recorder` 回调参数及所有调用点

### 2.2 Token 计数持久化迁移到 session 元数据

在 `Session` 对象中新增 `total_tokens: int` 字段，随 session 元数据一起序列化到 `.json`。

- `_persist()` 时自动保存 `total_tokens`
- `start()` 时从 `.json` 元数据恢复，不再遍历 events
- `AgentBridge._broadcast_token_usage()` 更新 session 的 `total_tokens` 后，由现有的 `response_done`/`turn_done` 触发持久化

### 2.3 前端历史恢复改用 messages

现有前端回放流程：无本地缓存 → `session.events` RPC → 逐条 replay 流式事件重建 UI。

替换为：无本地缓存 → 新 RPC `session.messages` → 从后端 messages 直接构建消息列表。

- 新增 `session.messages` RPC，返回 `messages` 列表（已序列化的 user/assistant 消息）
- 前端将 messages 直接映射为 UI 消息，无需逐条 replay 流式事件
- 删除 `session.events` RPC

## 3. 已确认决策

- **旧文件处理**：不做自动迁移，仅移除代码。旧 `.events.jsonl` 保留在磁盘上不影响运行，用户可自行删除。
- **前端历史恢复粒度**：历史 session 不需要 streaming 级别还原，显示完整 assistant 消息（含 tool_calls 和 tool results）即可。
- **Token 持久化时机**：等 `_persist()` 自然触发，中途崩溃丢失最后一轮 delta 可接受。

## 4. 实施步骤清单

### 阶段一：后端移除 events.jsonl [✅ 已完成]

- [x] **Task 1.1**: `Session` 模型增加 `total_tokens` 字段
  - [x] 在 `AgentSession` 中添加 `total_tokens: int = 0`
  - [x] `serialize()` / `_session_from_dict()` 中处理该字段
  - 状态：✅ 已完成

- [x] **Task 1.2**: `AgentBridge` 改为直接更新 session token 计数
  - [x] 移除 `event_recorder` 参数，替换为 `session` + `persist_fn`
  - [x] `_broadcast_token_usage()` 中更新 `session.total_tokens`
  - [x] 移除所有 `event_recorder()` 调用
  - [x] 在 `response_done`/`turn_done` 时调用 `persist_fn()`
  - 状态：✅ 已完成

- [x] **Task 1.3**: `SessionManager` 清理
  - [x] 删除 `record_event()` 和 `get_session_events()`
  - [x] `start()` 中传递 session + persist_fn 给 AgentBridge，移除 events 遍历
  - 状态：✅ 已完成

- [x] **Task 1.4**: `storage.py` 清理
  - [x] 删除 `append_session_event()` 和 `load_session_events()`
  - [x] 删除 `append_jsonl()` / `load_jsonl()`（无其他调用者）
  - 状态：✅ 已完成

### 阶段二：前端历史恢复改造 [✅ 已完成]

- [x] **Task 2.1**: 新增 `session.messages` RPC（替换 `session.events`）
  - [x] 返回序列化后的 messages 列表 + total_tokens
  - 状态：✅ 已完成

- [x] **Task 2.2**: 前端 `AgentPanel` 改用 messages 恢复
  - [x] 替换 `session.events` 调用为 `session.messages`
  - [x] 实现 messages → UI 消息的映射逻辑（含 tool_calls/tool_results）
  - 状态：✅ 已完成

### 阶段三：Bug 修复与测试 [✅ 已完成]

- [x] **Task 3.1**: `serialize_stream_event()` 仍被 WebSocket 实时广播使用，保留
  - 状态：✅ 已完成（无需删除）

- [x] **Task 3.2**: 修复 `_persist` 无 runtime 时覆写丢失 messages
  - [x] `_persist()` 在没有 runtime 时从磁盘加载已有 messages 保留
  - 状态：✅ 已完成

- [x] **Task 3.3**: 修复 ended session 重新激活不广播
  - [x] WebSocket handler 在 ended session 被激活后广播 `session_updated`
  - 状态：✅ 已完成

- [x] **Task 3.4**: 修复 `stop()` 两次 `_persist` 覆写 messages
  - [x] 合并为一次：先设 status=ended，persist 时 runtime 仍在，写完后再 pop
  - 状态：✅ 已完成

- [x] **Task 3.5**: 恢复误删的 `import uuid`
  - 状态：✅ 已完成

- [x] **Task 3.6**: 消息持久化单元测试（16 个用例全部通过）
  - [x] total_tokens 序列化/反序列化（4 例）
  - [x] _persist 有/无 runtime 行为（4 例）
  - [x] messages 往返（text / tool_call / error）（3 例）
  - [x] 模拟 server 多次重启（3 例）
  - [x] stop() 后 messages 保留（2 例）
  - 状态：✅ 已完成

## 5. 测试验证

### 单元测试
- [x] `tests/test_session_persistence.py` — 16/16 通过
- [x] `tests/test_runtime_session.py` — 40/40 通过（回归）

### 功能测试（待手动验证）
- [x] session 对话后刷新页面，历史消息完整显示
- [x] 服务重启后 resume session，token 计数正确恢复
- [x] 新建 session 后 `~/.mutbot/sessions/` 下不再产生 `.events.jsonl`

### 回归测试（待手动验证）
- [x] streaming 实时显示不受影响（WebSocket 推送不变）
- [x] tool call 结果在历史恢复中正确显示
- [x] ended session 发送消息后状态变为 active
