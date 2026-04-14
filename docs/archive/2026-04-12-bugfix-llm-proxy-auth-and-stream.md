# LLM Proxy 端点认证拦截与流式响应修复

**状态**：✅ 已完成
**日期**：2026-04-12
**类型**：Bug修复

## 需求

1. LLM proxy 端点（`/llm/*`）被 auth 中间件拦截，本地 Claude Code 无法访问
2. 流式响应 `StreamingResponse` 创建时位置参数错误，导致 stream 请求全部失败

## 关键参考

- `src/mutbot/auth/middleware.py` — auth 中间件，`_LOCAL_ONLY_PREFIXES` 白名单
- `src/mutbot/proxy/routes.py` — LLM proxy 路由，`_proxy_stream()` 函数
- `src/mutagent/net/server.py:44` — `StreamingResponse` Declaration 定义
- `src/mutagent/net/_protocol.py:458` — `_send_response_start` 中 h11 发送 status_code

## 设计方案

### Bug 1：`/llm` 路径未加入本地访问白名单

auth 中间件在有 auth 配置时，对所有非白名单路径要求认证。`/llm` 端点设计为本地 Claude Code 使用，应与 `/mcp` 一样只允许 loopback 访问、无需认证。

**修复**：将 `/llm` 加入 `_LOCAL_ONLY_PREFIXES`，loopback 放行，外部 403。

### Bug 2：`StreamingResponse` 位置参数导致 status 被覆盖

`_proxy_stream()` 返回：

```python
return StreamingResponse(
    event_generator(),          # 位置参数 → 赋给第一个字段 status
    media_type="text/event-stream",
)
```

`StreamingResponse` 是 mutobj Declaration，字段顺序为 `status, headers, body_iterator, media_type`。第一个位置参数 `event_generator()`（async_generator）被赋给了 `status` 而非 `body_iterator`。

h11 收到非 int 的 status_code 后抛出 `LocalProtocolError("status code must be integer")`，protocol 关闭连接，后续 access log 格式化 `%d` 遇到 async_generator 再次报错。

**修复**：使用关键字参数 `body_iterator=event_generator()`。

## 实施步骤清单

- [x] `middleware.py`：`_LOCAL_ONLY_PREFIXES` 添加 `"/llm"`
- [x] `routes.py`：`_proxy_stream` 的 `StreamingResponse` 改用 `body_iterator=` 关键字参数
- [x] 远端服务器 patch 验证（非 stream 和 stream 均正常）
