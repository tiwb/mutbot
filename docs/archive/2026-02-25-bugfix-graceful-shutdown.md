# 服务器 Ctrl+C 优雅退出 设计规范

**状态**：✅ 已完成
**日期**：2026-02-24 ~ 2026-02-25
**类型**：Bug修复

## 1. 背景

MutBot Web 服务器在按 Ctrl+C 后无法正常退出。显示 "Connection closed" 后进程挂起，需要手动 kill。

### 根因链

1. `AgentBridge.start()` 使用 `asyncio.to_thread(self._run_agent)` 运行 Agent 线程
2. `asyncio.to_thread()` 使用默认 `ThreadPoolExecutor`，线程为**非 daemon** 模式（Python 3.14 已移除早期版本中的 `t.daemon = True`）
3. Agent 线程在 `_run_agent()` 中调用 `agent.run(input_stream)`，内部阻塞于 mutagent `LLMClient` 的同步 HTTP 请求（`requests.post`）
4. `AgentBridge.stop()` 调用 `task.cancel()` 只取消 asyncio Task 包装，底层线程不受影响
5. `asyncio.wait(tasks, timeout=3)` 超时后，线程仍在运行
6. Python 解释器退出时，`concurrent.futures.thread._python_exit()` 对所有注册在 `_threads_queues` 中的 executor 线程调用 `t.join()`，无限期等待 → 进程挂起

> **关键细节**：即使将 executor 线程标记为 daemon，`_python_exit()` 的 `t.join()` 仍会阻塞（join 不区分 daemon 与否）。因此自定义 Daemon ThreadPoolExecutor **单独使用无法解决问题**。真正有效的方式是让线程完全绕过 `_threads_queues` 注册。

### 额外发现：uvicorn shutdown 偶发卡死

实施过程中发现，即使 Agent 线程问题解决后，uvicorn 自身的 shutdown 序列在 Windows 上会偶发卡死——WebSocket 连接关闭后，lifespan shutdown 代码永远不会被调用。这意味着任何仅依赖 lifespan shutdown 的退出机制都不够可靠。

## 2. 最终实现方案

采用三层防御策略：

### 2.1 第一层：Daemon Thread 直接替换

**变更文件**：`web/agent_bridge.py` — `AgentBridge.start()`

将 `asyncio.to_thread()` 替换为 `threading.Thread(daemon=True)` + `asyncio.Future` 包装。线程不经过 `ThreadPoolExecutor`，完全绕过 `_python_exit()` 的 `t.join()` 阻塞。

同时对 `_run_agent()` 和 `_thread_target()` 中所有 `call_soon_threadsafe` 调用添加 `RuntimeError` / `InvalidStateError` 保护，防止 event loop 关闭后或 future 已取消时抛异常到控制台。

### 2.2 第二层：链式 SIGINT handler（双 Ctrl+C）

**变更文件**：`web/server.py` — `_install_double_ctrlc_handler()`

在 lifespan **startup**（yield 之前）安装链式 SIGINT handler，捕获 uvicorn 的 `Server.handle_exit` 作为前置 handler：

- 第一次 Ctrl+C：打印提示，启动 watchdog 线程（见 2.3），然后调用 uvicorn 原有 handler 触发优雅退出
- 第二次 Ctrl+C：直接 `os._exit(0)` 强制退出

> **为什么在 startup 而非 shutdown 安装**：测试发现 uvicorn 在 Windows 上偶发卡死于 shutdown 序列中（WebSocket 关闭后、lifespan shutdown 前），导致 shutdown 阶段注册的 handler 永远不会被安装。在 startup 注册确保 handler 在整个服务器生命周期内可用。

### 2.3 第三层：Watchdog 超时兜底

**变更文件**：`web/server.py` — `_start_exit_watchdog()`

第一次 Ctrl+C 触发时同时启动一个 daemon watchdog 线程，`time.sleep(10)` 后调用 `os._exit(0)`。覆盖以下场景：

- uvicorn shutdown 卡死，lifespan shutdown 未执行
- 用户未手动按第二次 Ctrl+C（如无人值守部署）

Watchdog 是 daemon 线程，正常退出时自动消亡，无副作用。

### 退出行为矩阵

| 场景 | 机制 | 预期耗时 |
|------|------|----------|
| 正常退出（无阻塞 Agent） | uvicorn → lifespan cleanup → 正常结束 | < 3s |
| uvicorn shutdown 卡住 | watchdog 10s 超时 → `os._exit(0)` | 10s |
| 用户不想等 | 第二次 Ctrl+C → `os._exit(0)` | 立即 |
| lifespan cleanup 超时 | `asyncio.wait_for` 10s → `os._exit(0)` | 10s |

## 3. 变更清单

### `web/agent_bridge.py`

- `start()`：`asyncio.to_thread()` → `threading.Thread(daemon=True)` + `asyncio.Future` + coroutine wrapper
- `_thread_target()`：`future.set_result` 前检查 `future.done()` 防 `InvalidStateError`
- `_run_agent()`：所有 `call_soon_threadsafe` 添加 `try/except RuntimeError`

### `web/server.py`

- 新增 `_force_exit_flush()`：`os._exit()` 前 flush stdout/stderr/日志 handler
- 新增 `_start_exit_watchdog()`：daemon 线程 10s 超时强制退出
- 新增 `_install_double_ctrlc_handler()`：链式 SIGINT handler，startup 阶段安装
- lifespan shutdown：`_shutdown_cleanup()` + `asyncio.wait_for` 10s 超时

## 4. 测试结果

| 场景 | 预期结果 | 实际结果 |
|------|----------|----------|
| 无活跃 Agent，Ctrl+C | < 2 秒正常退出 | ✅ 通过 |
| Agent 空闲，Ctrl+C | < 3 秒正常退出 | ✅ 通过 |
| uvicorn shutdown 卡死，等待自动退出 | 10 秒 watchdog 兜底退出 | ✅ 通过 |
| uvicorn shutdown 卡死，二次 Ctrl+C | 立即强制退出 | ✅ 通过 |
| 多 session 活跃，Ctrl+C | 正常清理并退出 | ✅ 通过 |

## 5. 遗留问题

- **uvicorn Windows shutdown 卡死**：偶发，根因未知（可能与 WebSocket 关闭时序有关）。当前通过 watchdog 兜底，非阻塞问题。如需彻底解决，需要调查 uvicorn/starlette 在 Windows 上的 WebSocket shutdown 行为。
- **LLM HTTP 调用可中断性**：当前方案通过 daemon 线程绕过 HTTP 阻塞问题，线程在 `os._exit()` 时被强制终止。长期来看，如果 mutagent 支持异步 HTTP 客户端或可中断的调用，可以实现更优雅的线程退出。
- **阻塞 I/O + asyncio 架构张力**：mutagent 使用同步阻塞 I/O，mutbot 使用 asyncio。两者的生命周期管理存在固有矛盾。详见 `docs/specifications/analysis-blocking-io-asyncio.md`。
