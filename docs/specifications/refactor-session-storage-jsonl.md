# Agent 会话存储格式：JSON 迁移到 JSONL

**状态**：📋 需求中
**日期**：2026-04-14
**类型**：重构

## 需求

1. 当前 agent 会话以单个 JSON 文件存储（元数据 + 全部消息在一个文件中），每次持久化需要重写整个文件
2. Agent 会话的消息历史是只增不改的（append-only），更适合 JSONL 格式——类似 Claude Code 的会话存储方式
3. API 录制（`session-*-api.jsonl`）已经在用 JSONL，会话存储应保持一致

## 现状

### 当前存储结构

- **位置**：`~/.mutbot/sessions/{YYYYMMDD_HHMMSS}-{session_id}.json`
- **格式**：单个 JSON 文件，包含元数据 + 所有消息
- **写入方式**：原子写入（temp file + `os.replace()`），每次写入完整文件
- **触发时机**：session 创建/更新/停止 + dirty loop 每 5 秒检查

```json
{
  "id": "ef38d601749a",
  "workspace_id": "...",
  "title": "Agent 1",
  "type": "mutbot.session.AgentSession",
  "messages": [
    {"role": "user", "blocks": [...], "id": "m_xxx", "timestamp": ...},
    {"role": "assistant", "blocks": [...], "id": "xxx", "model": "...", "timestamp": ...}
  ]
}
```

### 已有的 JSONL 先例

API 录制文件（`mutagent/runtime/api_recorder.py`）已使用 JSONL：
```jsonl
{"type": "session", "session_id": "...", "model": "...", "ts": "..."}
{"type": "call", "ts": "...", "input": {...}, "response": {...}, "usage": {...}, "duration_ms": 5908}
```

### 持久化代码

- 存储层：`mutbot/src/mutbot/runtime/storage.py`（`save_json` / `load_json`）
- 会话管理：`mutbot/src/mutbot/runtime/session_manager.py`（`_persist` / `_load_agent_messages`）
- 消息序列化：`mutbot/src/mutbot/web/serializers.py`（`serialize_message` / `deserialize_message`）
- 持久化测试：`mutbot/tests/test_session_persistence.py`

## 关键参考

- 现有存储实现：`mutbot/src/mutbot/runtime/storage.py`
- 会话管理器：`mutbot/src/mutbot/runtime/session_manager.py` L429-467（`_persist` / `_load_agent_messages`）
- API 录制（JSONL 参考实现）：`mutagent/src/mutagent/runtime/api_recorder.py`
- Claude Code 会话格式参考：`~/.claude/projects/` 下的 JSONL 文件
