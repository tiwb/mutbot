# 服务器 Graceful Shutdown 修复 设计规范

**状态**：✅ 已完成
**日期**：2026-03-07
**类型**：Bug修复

## 背景

按 Ctrl+C 后服务器无法正常退出，等待 10 秒超时后强制 `os._exit(0)`。经测试，即使不带终端也有此问题，说明阻塞源不仅限于终端读线程。

## 症状

1. 按 Ctrl+C 后服务器不退出
2. 等待 10 秒后被 watchdog 强制 `os._exit(0)` 杀掉
3. 无终端 session 时也复现

## 根因分析

### 阻塞机制

经 8 次测试（纯 agent session、含 terminal session、客户端连接中/已断开），定位到**两层阻塞**：

**第一层：uvicorn 的 `_wait_tasks_to_complete()` 中 `server.wait_closed()` 挂起**

Python 3.12+ 改变了 `asyncio.Server.wait_closed()` 的语义——它会等待所有 protocol 连接真正 detach。当 WebSocket 断开后 protocol 层的连接对象未被及时清理时，`wait_closed()` 永远不会返回。uvicorn 的 `timeout_graceful_shutdown` 默认为 `None`（无限等待），导致 shutdown 卡在这一步，**lifespan exit 永远执行不到**。

```
Ctrl+C → uvicorn shutdown() → _wait_tasks_to_complete()
  → wait connections ✓（已空）
  → wait tasks ✓（已空）
  → server.wait_closed() ← 永久挂起（Python 3.12+ 行为变更）
  → lifespan.shutdown() ← 永远执行不到
```

**第二层：`Client._send_worker` task 阻塞 event loop**

即使 uvicorn 能进入 lifespan exit，`Client._send_worker` 是在 WebSocket handler 内部创建的独立 asyncio task，无限循环 `await _send_queue.get()`。Client 断开后进入 buffering 状态，`_send_worker` 继续运行（为支持 30 秒内重连）。`Client.stop()` 方法存在但 shutdown 流程中从未调用。

### Client 追踪现状

`routes.py` 模块级字典追踪：
- `_clients: dict[str, Client]` — client_id → Client（用于重连匹配）
- `_workspace_clients: dict[str, set[Client]]` — workspace_id → Client 集合（用于广播）

## 设计方案

### 修复点

**修复 A：设置 `timeout_graceful_shutdown`（解决第一层阻塞）**

在 uvicorn Config 中设置 `timeout_graceful_shutdown=3`，使 `_wait_tasks_to_complete()` 在 3 秒后超时，uvicorn 能进入 lifespan exit 执行我们的清理逻辑。

**修复 B：在 `_shutdown_cleanup()` 中停止所有 Client（解决第二层阻塞）**

提取 `_stop_all_clients()` 函数，在 lifespan exit 的 `_shutdown_cleanup()` 中调用 `client.stop()` 取消所有 `_send_worker` task。

### Shutdown 流程（修复后）

```
Ctrl+C → uvicorn shutdown()
  → _wait_tasks_to_complete()
  → 3s timeout → 进入 lifespan exit
  → _shutdown_cleanup()
    → session_manager.stop(sid) for each session
    → _stop_all_clients()  ← 新增
    → terminal_manager.kill_all()
  → Application shutdown complete
```

## 关键参考

### 源码
- `src/mutbot/web/server.py` — `_shutdown_cleanup()`、`_stop_all_clients()`、lifespan shutdown、uvicorn Config
- `src/mutbot/web/transport.py` — `Client.start()` 创建 `_send_task`、`Client.stop()` 取消 task
- `src/mutbot/web/routes.py` — `_clients` 字典、Client 创建、disconnect 时 `enter_buffering()`

### 外部参考
- [uvicorn Discussion #2122](https://github.com/encode/uvicorn/discussions/2122) — Python 3.12+ `wait_closed()` 行为变更导致 shutdown 挂起
- Python 3.12+ `asyncio.Server.wait_closed()` 语义变更

## 实施步骤清单

- [x] **Task 1**: 设置 uvicorn `timeout_graceful_shutdown=3`
  - [x] 在 `uvicorn.Config()` 中添加参数
  - 状态：✅ 已完成

- [x] **Task 2**: 在 `_shutdown_cleanup()` 中添加 Client 清理
  - [x] 提取 `_stop_all_clients()` 函数
  - [x] 在 session 清理后调用 `_stop_all_clients()`
  - 状态：✅ 已完成

- [x] **Task 3**: 清理诊断代码
  - [x] 移除 watchdog 中的诊断日志
  - [x] 移除 `_cancel_non_uvicorn_tasks()` 函数
  - [x] 简化 signal handler
  - 状态：✅ 已完成

- [x] **Task 4**: 完整测试验证
  - [x] 场景 A：客户端连接中 Ctrl+C → 正常退出（< 1s）✓
  - [x] 场景 B：客户端先关闭 → Ctrl+C → 3 秒后正常退出 ✓
  - [x] 场景 C：含 terminal + 客户端连接中 Ctrl+C → 正常退出（< 1s）✓
  - [x] 场景 D：含 terminal + agent + 客户端先关闭 → Ctrl+C → 3 秒后正常退出 ✓
  - 状态：✅ 已完成

## 测试验证

| 场景 | 结果 | 退出耗时 |
|------|------|---------|
| 客户端连接中 Ctrl+C | ✅ 正常退出 | < 1s |
| 客户端先关闭 → Ctrl+C | ✅ 正常退出 | 3s |
| 含 terminal + 客户端连接中 | ✅ 正常退出 | < 1s |
| 含 terminal + agent + 客户端先关闭 | ✅ 正常退出 | 3s |
