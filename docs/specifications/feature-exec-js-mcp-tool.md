# 浏览器 JavaScript 执行 MCP 工具

**状态**：✅ 已完成
**日期**：2026-03-17
**类型**：功能设计

## 背景

调试前端问题（如移动端终端滚动异常）时，需要在浏览器中执行 JS 检查 DOM 状态、xterm 实例、React state 等。现有 CDP 方案（`cdp_debug.py`）仅适用于 PC（需要 Chrome 以 `--remote-debugging-port` 启动），手机上无法使用。

需要一个 `exec_js` MCP 工具，通过已有的 WebSocket 连接向任意客户端（PC 或手机）发送 JS 代码并获取执行结果。

## 设计方案

### 通信流程

```
Claude Code → MCP exec_js(code, client_id?)
  → 后端生成 eval_id，直接推送给目标客户端
    → {type: "event", event: "eval_js", data: {id: "abc123", code: "document.title"}}
  → await Future(eval_id)，10 秒超时

前端收到 eval_js 事件
  → try { result = eval(code) } catch (e) { error = e.message }
  → 通过 RPC 回复: {type: "rpc", method: "debug.eval_result", params: {id: "abc123", result: "...", error: null}}

后端 RPC handler debug.eval_result
  → resolve Future(eval_id)
  → MCP 返回结果
```

### MCP 工具接口

```python
# tool: exec_js
# 参数:
#   code: str          — JavaScript 代码
#   client_id: str     — 目标客户端 ID（可选，前缀匹配，默认第一个连接的客户端）
# 返回: str            — 执行结果或错误信息
```

### 后端实现

**MCP tool**（`mcp.py`）：
1. 根据 `client_id` 找到 Client 对象（不指定则取第一个 connected 的客户端，指定则前缀匹配）
2. 生成唯一 `eval_id`（uuid4 hex[:8]）
3. 通过 `client.enqueue("json", {type: "event", event: "eval_js", data: {id, code}})` 推送
4. 创建 `asyncio.Future`，存入模块级 `_eval_js_pending: dict[str, Future]`
5. `await asyncio.wait_for(future, timeout=10)`
6. 返回结果

**RPC handler**（`mcp.py` 或新文件）：
- 注册 `debug.eval_result` RPC handler
- 从 `_eval_js_pending` 取出 Future 并 resolve

### 前端实现

在 WebSocket 事件处理中新增 `eval_js` 分支：

```typescript
// 收到 eval_js 事件时
if (event === "eval_js") {
  const { id, code } = data;
  let result: string | null = null;
  let error: string | null = null;
  try {
    const value = eval(code);
    result = typeof value === "object" ? JSON.stringify(value) : String(value);
  } catch (e) {
    error = e instanceof Error ? e.message : String(e);
  }
  // 通过 RPC 回复结果
  rpc.call("debug.eval_result", { id, result, error });
}
```

### 超时与错误处理

- 默认 10 秒超时，超时返回 `"error: timeout"`
- JS 执行异常通过 error 字段返回
- 客户端未找到返回 `"error: client not found"`

## 实施步骤清单

- [x] **Task 1**: 后端 — MCP tool + RPC handler
  - [x] `mcp.py` 新增 `BrowserTools.exec_js`，含 pending map、推送、await
  - [x] `rpc_workspace.py` 新增 `DebugRpc.eval_result` RPC handler
  - 状态：✅ 已完成

- [x] **Task 2**: 前端 — eval_js 事件处理
  - [x] `workspace-rpc.ts` 事件处理中新增 `eval_js` 分支
  - 状态：✅ 已完成

- [x] **Task 3**: 测试验证
  - [x] 热重启后通过 MCP 执行 JS 代码验证（document.title、navigator.userAgent、对象返回、错误处理）
  - 状态：✅ 已完成

## 关键参考

### 源码
- `mutbot/src/mutbot/web/mcp.py` — BrowserTools.exec_js + _eval_js_pending
- `mutbot/src/mutbot/web/rpc_workspace.py` — DebugRpc.eval_result RPC handler
- `mutbot/src/mutbot/web/routes.py` — WorkspaceWebSocket 消息循环、事件推送
- `mutbot/src/mutbot/web/transport.py:512-527` — Client.enqueue
- `mutbot/frontend/src/lib/workspace-rpc.ts` — eval_js 事件处理

### 相关规范
- `docs/specifications/feature-exec-python-mcp-tool.md` — exec_python 实现参考
