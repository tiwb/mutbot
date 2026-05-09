# mutbot.session_messages 历史 session 支持

**状态**：📋 需求中
**日期**：2026-04-14
**类型**：Bug修复

## 需求

1. `mutbot.session_messages` 当 agent runtime 不存在时报错 "no agent runtime"，无法查看已结束或 idle 的 AgentSession 对话历史
2. 应 fallback 到读取 API 录制文件（`session-*-api.jsonl`），使任何可见 session 都能查到对话内容
3. `mutbot.sessions` 能列出该 session，但 `mutbot.session_messages` 拒绝服务，体验不一致

## 复现

```
mutbot.sessions() → 9f6aa99d  AgentSession  []  Agent 1   ← 存在
mutbot.session_messages(session_id="9f6aa99d") → error: no agent runtime  ← 报错
```

session 的 agent runtime 已销毁（idle 超时或用户停止），但 session 仍在注册表中，且 API 录制文件完整存在。

## 关键参考

- sandbox MutbotTools 实现：`mutbot/src/mutbot/builtins/debug_tools.py`(`session_messages` 方法)
- API 录制文件格式：`~/.mutbot/logs/session-*-api.jsonl`（每行一个 JSON，含 input/response/usage/duration）
- 日志查询 CLI：`python -m mutbot log` 子命令体系

