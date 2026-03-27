# 反向 RPC — 后端主动调用前端

**状态**：📝 设计中
**日期**：2026-03-17
**类型**：功能设计

## 背景

现有 WebSocket RPC 是单向的：前端 → 后端（`{type: "rpc", method, params}` → `{type: "rpc_result", result}`）。后端只能向前端推送事件（`{type: "event"}`），无法发起请求并等待前端响应。

`exec_js` 功能通过 ad-hoc 方式实现了类似效果（event 推送 + 前端主动发 RPC 回调 + Future 等待），但这个模式不通用：每个需要"后端调前端"的场景都要自建 pending map + 超时逻辑。

如果有通用的反向 RPC 基础设施，后端可以像前端调后端一样简洁地调用前端方法。

## 需求

### 核心能力

后端能向指定客户端发起 RPC 调用并 await 结果：

```python
# 后端调用前端
result = await client.reverse_call("dom.querySelector", {"selector": "#app"}, timeout=10)
```

前端能注册反向 RPC handler：

```typescript
// 前端注册 handler
rpc.onCall("dom.querySelector", (params) => {
  return document.querySelector(params.selector)?.outerHTML ?? null;
});
```

### 潜在用例

- `exec_js` 重构为反向 RPC（消除 ad-hoc 的 pending map）
- DOM 状态查询（调试用）
- 前端性能数据采集（`performance.getEntries()` 等）
- 客户端能力探测

### 设计约束

- 复用现有 WebSocket 连接，不开新通道
- 消息格式与现有 RPC 对称（`type: "reverse_rpc"` / `type: "reverse_rpc_result"`）
- 需处理：超时、客户端断连、method 不存在
- 避免嵌套调用问题（前端处理反向 RPC 时又发正向 RPC → 后端又发反向 RPC → 死锁）

## 关键参考

### 源码
- `mutbot/src/mutbot/web/rpc.py` — 现有 RPC 框架（RpcDispatcher、RpcContext）
- `mutbot/frontend/src/lib/workspace-rpc.ts` — 前端 RPC 客户端
- `mutbot/src/mutbot/web/mcp.py` — exec_js 的 ad-hoc 反向调用实现（`_eval_js_pending`）

### 相关规范
- `docs/specifications/feature-exec-js-mcp-tool.md` — exec_js，可作为反向 RPC 的重构目标
