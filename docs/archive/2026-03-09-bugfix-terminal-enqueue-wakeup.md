# Client.enqueue 跨线程调用缺少事件循环唤醒

**状态**：✅ 已完成
**日期**：2026-03-09
**类型**：Bug修复

## 背景

传输层重构（Channel 抽象层引入）后，终端交互有细微延迟——手感不跟手，延迟不规律。

终端 PTY reader 线程通过同步回调链投递数据：

```
reader thread → _on_pty_output() → on_output(data) → channel.send_binary()
  → client.enqueue() → asyncio.Queue.put_nowait()
```

`asyncio.Queue` 非线程安全。从后台线程调用 `put_nowait()` 时，CPython GIL 保护了数据入队，但事件循环不会被唤醒。`_send_worker` 的 `await queue.get()` 要等到下一个自然事件（ACK 定时器 5s、keepalive 20s 等）才处理。

重构前使用 async 回调 + `run_coroutine_threadsafe`（内含 `call_soon_threadsafe`），能正确唤醒事件循环。重构后改为同步回调直接调用 `put_nowait`，丢失了唤醒机制。

## 设计方案

### 核心修复

`Client.enqueue()` 改用 `loop.call_soon_threadsafe(queue.put_nowait, item)` 投递数据。删除 `send_json()` 方法，调用方统一使用 `enqueue("json", data)`（消除不对称接口：有 `send_json` 无 `send_binary`）。

```python
def enqueue(self, frame_type: FrameType, data: Any) -> None:
    """线程安全入队，通过 call_soon_threadsafe 唤醒事件循环。"""
    loop = self._get_loop()
    loop.call_soon_threadsafe(self._send_queue.put_nowait, (frame_type, data))
```

### 风险评估

**事件循环线程调用的语义变化**：从事件循环线程调用时（agent streaming 路径），`put_nowait` 从同步执行变为延迟一轮 event loop 调度（< 0.1ms）。对 streaming 文本不可感知。

**`_get_loop()` 线程安全**：Client 在事件循环线程创建，`_loop` 在 `start()` 时已初始化。后台线程读取引用赋值在 CPython 下是原子的，无风险。

**高频调用开销**：峰值 70 帧/秒，每帧一次 `call_soon_threadsafe`（pipe write ~1μs），可忽略。

**`reset_for_fresh_connection` 队列清空**：该方法在事件循环线程清空 `_send_queue`。改用 `call_soon_threadsafe` 后，reader 线程可能有已提交但尚未执行的 `put_nowait` 回调，这些回调会在清空后执行，将少量数据放入"新"队列。这些数据是 PTY 的实时输出（非历史数据），新 `_send_worker` 正常处理即可，无副作用。

### 调用点审查

`Client.send_json()` 的调用方（均在事件循环线程，语义变化无影响）：
- `routes.py` — workspace 级广播（welcome、config_changed、RPC response、事件推送）
- `routes.py:480` — terminal ready 事件

`Client.enqueue()` 的调用方（通过 Channel @impl）：
- `_channel_send_json` — Session 消息广播（agent streaming、terminal JSON 控制）
- `_channel_send_binary` — 终端二进制数据（**此路径从 reader 线程调用，是本 bug 的触发点**）

## 关键参考

### 源码
- `src/mutbot/web/transport.py:263-269` — `send_json` 和 `enqueue` 方法
- `src/mutbot/web/transport.py:327-346` — `reset_for_fresh_connection` 队列清空
- `src/mutbot/web/transport.py:356-373` — `_send_worker` 消费队列
- `src/mutbot/runtime/terminal.py:291-321` — `_on_pty_output` reader 线程广播
- `src/mutbot/runtime/terminal.py:611-612` — `on_output` 同步回调（`channel.send_binary`）

### 日志数据
- 终端峰值帧率 60-70 帧/秒，平均帧大小 61 bytes，峰值带宽 ~4.4 KB/s
- 帧大小范围 3-109 bytes（PTY read(4096) 在 Windows 上分包碎片化）

## 实施步骤清单

- [x] **Task 1**: 修改 `Client.enqueue()` 使用 `call_soon_threadsafe`
  - [x] `enqueue()` 改为 `loop.call_soon_threadsafe(self._send_queue.put_nowait, (frame_type, data))`
  - [x] 更新 docstring（已是线程安全，说明唤醒机制）
  - 状态：✅ 已完成

- [x] **Task 2**: 删除 `Client.send_json()`，调用方改为 `enqueue("json", data)`
  - [x] 删除 `send_json` 方法
  - [x] 更新 `routes.py` 中所有 `client.send_json(data)` → `client.enqueue("json", data)`（6 处）
  - 状态：✅ 已完成

- [x] **Task 3**: 验证
  - [x] 启动 mutbot，终端输入输出正常
  - [x] agent 对话 streaming 正常
  - 状态：✅ 已完成

## 测试验证

（实施阶段填写）
