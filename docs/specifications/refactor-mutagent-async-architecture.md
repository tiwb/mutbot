# mutagent 全异步架构改造 设计规范

**状态**：✅ 已完成
**日期**：2026-02-26
**类型**：重构

## 1. 背景

mutagent 在 2026-02-16 移除了 asyncio，回归纯同步架构。当时的理由充分：agent 循环串行，无并发需求，`input()` 包装成异步是多此一举。

但 mutbot 作为 Web 层引入后，sync/async 边界产生了显著的架构张力（详见 `analysis-blocking-io-asyncio.md`）：

- `AgentBridge` 需要 daemon thread + `call_soon_threadsafe` + `asyncio.Queue` + Future 包装
- 同步 `requests.post()` 在线程中不可取消，`stop()` 只能等 3 秒超时后放弃
- shutdown 需要三层防御（lifespan cleanup + 双 Ctrl+C + watchdog）
- 跨线程通信有 loop 关闭竞态、Future 状态竞争等边界问题

`analysis-blocking-io-asyncio.md` 第 6.3 节评估了三种方案（全面回归 async / httpx 同步客户端 / 双接口），均无法同时解决 `input()` 和 HTTP 取消两个问题。

本方案是**第四种选择**：将两个问题正交化——`input()` 留在主线程（不进入 asyncio），agent 内部全异步。

## 2. 设计方案

### 2.1 核心思路

**mutagent 内部全异步，CLI 的 `input()` 不在 asyncio 世界中。**

```
CLI 模式:
  Main Thread (sync)              Agent Thread (asyncio event loop)
  ┌────────────────────┐          ┌──────────────────────────────┐
  │ while True:        │          │ async def run(input):        │
  │   inp = input(">") │──Queue──▶│   async for chunk in         │
  │   for ev in ...:   │◀─Queue──│     httpx_client.post(...):  │
  │     render(ev)     │          │       yield StreamEvent      │
  └────────────────────┘          └──────────────────────────────┘

Web 模式 (mutbot):
  asyncio event loop（同线程，无需桥接）
  ┌─────────────────────────────────────────────┐
  │ async def websocket_handler():              │
  │   async for event in agent.run(input_q):    │
  │     await ws.send_json(serialize(event))    │
  └─────────────────────────────────────────────┘
```

### 2.2 mutagent 侧变更

#### 2.2.1 Agent 接口改为 async

```python
class Agent(mutagent.Declaration):
    client: LLMClient
    tool_set: ToolSet
    system_prompt: str
    messages: list
    max_tool_rounds: int

    async def run(
        self, input_stream: AsyncIterator[InputEvent], stream: bool = True
    ) -> AsyncIterator[StreamEvent]:
        """异步 agent 循环。消费 async input_stream，yield StreamEvent。"""
        ...

    async def step(self, stream: bool = True) -> AsyncIterator[StreamEvent]:
        """单次 LLM 调用，yield 流式事件。"""
        ...

    async def handle_tool_calls(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """异步执行工具调用。"""
        ...
```

- `run()` 保持方法名不变，改为 `async def`，返回类型 `Iterator` → `AsyncIterator`
- `input_stream` 参数从 `Iterator[InputEvent]` 改为 `AsyncIterator[InputEvent]`
- 内部 `for event in self.step()` 变为 `async for event in self.step()`

#### 2.2.2 LLMClient / LLMProvider 改为 async

```python
class LLMClient(mutagent.Declaration):
    async def send_message(
        self, messages, tools, system_prompt="", stream=True
    ) -> AsyncIterator[StreamEvent]:
        ...

class LLMProvider(mutagent.Declaration):
    async def send(
        self, model, messages, tools, system_prompt="", stream=True
    ) -> AsyncIterator[StreamEvent]:
        ...
```

- HTTP 调用从 `requests` 改为 `httpx.AsyncClient`
- 流式响应使用 `async for line in response.aiter_lines()`

#### 2.2.3 ToolSet 工具执行

```python
class ToolSet(mutagent.Declaration):
    async def dispatch(self, tool_call: ToolCall) -> ToolResult:
        ...
```

大部分工具是 CPU-bound 或文件 I/O，可以用 `await asyncio.to_thread(sync_fn)` 包装。未来有 async 工具时可直接 `await`。

#### 2.2.4 CLI 适配层

CLI 不使用 `asyncio.run()`，而是在独立线程中运行 asyncio event loop：

```python
# mutagent/builtins/main_impl.py

import asyncio
import queue
import threading

def run(self: App) -> None:
    self.setup_agent(system_prompt=SYSTEM_PROMPT)

    # 启动 asyncio event loop 线程
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    event_q: queue.Queue[StreamEvent | None] = queue.Queue()

    print("mutagent ready. Ctrl+C to exit.\n")

    while True:
        try:
            user_input = self.userio.read_input()
            if not user_input:
                continue

            # 构造 async input source（单条消息）
            async def single_input():
                yield InputEvent(type="user_message", text=user_input)

            # 提交 agent 任务到 asyncio 线程
            async def run_agent():
                async for event in self.agent.run(single_input()):
                    event_q.put(event)
                event_q.put(None)  # sentinel

            future = asyncio.run_coroutine_threadsafe(run_agent(), loop)

            # 主线程同步消费事件
            for event in iter(event_q.get, None):
                self.userio.render_event(event)

        except KeyboardInterrupt:
            # 取消正在运行的 agent 任务
            if not future.done():
                future.cancel()
            print("\n[User interrupted]")
```

关键点：
- `input()` 在主线程直接调用，无 `run_in_executor`
- agent 在 asyncio 线程中运行，HTTP 可取消
- Ctrl+C 在主线程触发 `KeyboardInterrupt`，通过 `future.cancel()` 传播到 asyncio 侧
- 跨线程通信用标准库 `queue.Queue`，简单可靠

### 2.3 mutbot 侧变更

#### 2.3.1 AgentBridge 大幅简化

agent 本身就是 async，mutbot 可以直接在自己的 asyncio event loop 中调用：

```python
class AgentBridge:
    """简化后的 bridge —— 不再需要 daemon thread。"""

    def __init__(self, session_id, agent, loop, broadcast_fn, event_recorder=None):
        self.session_id = session_id
        self.agent = agent
        self.loop = loop
        self.broadcast_fn = broadcast_fn
        self.event_recorder = event_recorder
        self._input_queue: asyncio.Queue[InputEvent | None] = asyncio.Queue()
        self._agent_task: asyncio.Task | None = None

    async def _input_stream(self):
        """Async generator: 从 asyncio.Queue 读取 InputEvent。"""
        while True:
            item = await self._input_queue.get()
            if item is None:
                return
            yield item

    def start(self) -> None:
        async def _run():
            async for event in self.agent.run(self._input_stream()):
                data = serialize_stream_event(event)
                if self.event_recorder:
                    self.event_recorder(data)
                await self.broadcast_fn(self.session_id, data)
            await self.broadcast_fn(self.session_id, {"type": "agent_done"})

        self._agent_task = self.loop.create_task(_run())

    def send_message(self, text: str, data: dict | None = None) -> None:
        event = InputEvent(type="user_message", text=text, data=data or {})
        self._input_queue.put_nowait(event)
        # 广播 user_message 给其他客户端
        user_event = {"type": "user_message", "text": text, "data": data or {}}
        asyncio.ensure_future(self.broadcast_fn(self.session_id, user_event))

    async def stop(self) -> None:
        self._input_queue.put_nowait(None)  # 停止 input_stream
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
            try:
                await self._agent_task
            except asyncio.CancelledError:
                pass
```

消除的复杂度：
- ~~daemon thread~~ → asyncio task（同一事件循环）
- ~~`queue.Queue` + `threading.Event` + 0.5s poll~~ → `asyncio.Queue`（原生 async）
- ~~`call_soon_threadsafe` + RuntimeError 防护~~ → 直接 `await`
- ~~Future 包装 + InvalidStateError 处理~~ → 标准 task 取消
- ~~3 秒超时等待~~ → `task.cancel()` 真正生效（httpx 响应 CancelledError）

#### 2.3.2 WebUserIO 简化

`WebUserIO` 不再需要作为独立类。input 流直接由 `AgentBridge._input_stream()` 提供。`present()` 回调可以集成到 agent 的事件流中，或作为 bridge 的方法。

### 2.4 与 2026-02-16 移除方案的对比

| 痛点 | 移除前（全 async） | 本方案 |
|------|-------------------|--------|
| `input()` 包装 | `run_in_executor` | 主线程直接调用，不在 asyncio 中 |
| Ctrl+C 异常类型 | 3 种 | 主线程 1 种（`KeyboardInterrupt`） |
| async 传染 | 全栈（CLI → agent → client） | agent 内部（CLI adapter 是纯 sync） |
| Windows 信号处理 | asyncio 层不一致 | 主线程处理，无 asyncio 信号问题 |
| HTTP 可取消 | 可取消（aiohttp） | 可取消（httpx async） |
| mutbot 桥接 | N/A（当时无 mutbot） | 无需桥接，直接 await |

### 2.5 依赖变更

| 变更 | 旧 | 新 |
|------|----|----|
| HTTP 客户端 | `requests` | `httpx`（async） |
| SSE 解析 | `requests` + 手动解析 | `httpx` + `httpx-sse` 或手动解析 |
| 新增依赖 | — | `httpx` |

`httpx` 同时支持 sync 和 async 接口，API 与 `requests` 高度兼容，迁移成本低。

### 2.6 已确认的设计决策

#### D1: 只提供 async 接口，不保留 sync `run()`
CLI 适配层已展示了如何在 sync 上下文中使用 async agent。保留 sync `run()` 意味着双接口维护（analysis 文档方案 C 的问题）。第三方如需 sync 调用，可自行用 `asyncio.run()` 或线程模式包装。

#### D2: ToolSet.dispatch 改为 async，透明处理同步工具
`dispatch()` 内部自动检测：async callable 直接 `await`，sync callable 用 `asyncio.to_thread()` 包装。工具作者无需关心 async/sync，写哪种都行。

#### D3: CLI 适配层代码放在 `mutagent/builtins/main_impl.py`
`App.run()` 的实现中包含独立线程 event loop + queue 通信逻辑。这是 mutagent 自带的 CLI 入口，职责明确。mutbot 不需要这段代码。

#### D4: present() 统一为 StreamEvent 事件类型
**现状分析**：

`UserIO.present(content: Content)` 的设计意图是渲染**非 LLM 来源**的结构化输出——系统状态变更、工具副作用、Sub-Agent 输出等。它是 `render_event()` 的补充路径：

- `render_event(event)` — 处理 LLM 流式输出（`text_delta`、`tool_use_*` 等）
- `present(content)` — 处理非流式的完整 `Content` 块

两条实现路径：
- CLI 基础终端（`builtins/userio_impl.py:194`）：委托给 `BlockHandler.render(content)` 渲染
- CLI Rich 终端（`extras/rich/userio_impl.py:242`）：用 Rich Console 渲染
- Web 层（`agent_bridge.py:60`）：`WebUserIO.present()` 序列化后通过 `event_callback` 推入事件队列

**当前调用者**：Agent 代码本身**不调用** `present()`。Agent 只 yield `StreamEvent`。`present()` 目前仅在测试中被调用，是为未来扩展（Sub-Agent 输出、系统通知）预留的 API。

**改造方案**：增加 `StreamEvent(type="present", content=Content(...))` 事件类型。所有输出统一走事件流，`present()` 回调不再需要。CLI 和 Web 都通过 `render_event()` / 事件消费统一处理。

#### D5: 文档归属
- 主文档：`mutbot/docs/specifications/refactor-mutagent-async-architecture.md`（本文档）
- mutagent 侧：在 `mutagent/docs/` 下放一篇架构决策记录（ADR），说明 async 改造的原因和决策，供独立查阅

#### D6: 方法命名保持 `run()`，不加 `a` 前缀
只有 async 一个版本，不需要 `a` 前缀区分。`a` 前缀惯例（Django `aget()`/`asave()`）来自"同一方法有 sync/async 两版"的场景，不适用于本项目。所有方法保持原名：`run()`、`step()`、`send_message()`、`send()`、`dispatch()`。

## 3. 实施步骤清单

### 阶段一：mutagent 核心 async 改造 [✅ 已完成]

- [x] **Task 1.1**: Agent 声明改为 async
  - [x] `agent.py`: `run()` 改为 `async def`，返回类型 `Iterator` → `AsyncIterator`
  - [x] `agent.py`: `step()` 改为 `async def`，返回 `AsyncIterator[StreamEvent]`
  - [x] `agent.py`: `handle_tool_calls()` 改为 `async def`
  - 状态：✅ 已完成

- [x] **Task 1.2**: LLMClient / LLMProvider 改为 async
  - [x] `client.py`: `send_message()` 改为 `async def`，返回 `AsyncIterator[StreamEvent]`
  - [x] `provider.py`: `send()` 改为 `async def`，返回 `AsyncIterator[StreamEvent]`
  - [x] Provider 实现：`requests` → `httpx.AsyncClient`
  - [x] SSE 流式解析适配 httpx (`aiter_lines()`)
  - 状态：✅ 已完成

- [x] **Task 1.3**: Agent 实现改为 async
  - [x] `builtins/agent_impl.py`: `run()` 改为 async generator
  - [x] `builtins/agent_impl.py`: `step()` 改为 async generator
  - [x] `builtins/agent_impl.py`: `handle_tool_calls()` 改为 async
  - 状态：✅ 已完成

- [x] **Task 1.4**: ToolSet.dispatch 改为 async
  - [x] 声明改为 `async def dispatch()`
  - [x] 实现中自动检测同步/异步工具，透明包装（`inspect.iscoroutinefunction` + `asyncio.to_thread`）
  - 状态：✅ 已完成

### 阶段二：mutagent CLI 适配 [✅ 已完成]

- [x] **Task 2.1**: CLI 适配层实现
  - [x] `builtins/main_impl.py`: `App.run()` 使用独立线程 event loop 模式
  - [x] 主线程 `input()` + `queue.Queue` 通信
  - [x] Ctrl+C 通过 `future.cancel()` 传播
  - 状态：✅ 已完成

- [x] **Task 2.2**: UserIO 适配
  - [x] CLI 层直接构造 async input source，不再经过 `input_stream()`
  - [x] `UserIO.input_stream()` 保留为 CLI 内部使用（向后兼容）
  - [x] `UserIO.present()` 保留（D4 事件统一留到后续迭代）
  - 状态：✅ 已完成

### 阶段三：mutbot 侧适配 [✅ 已完成]

- [x] **Task 3.1**: AgentBridge 重写
  - [x] 移除 daemon thread / `call_soon_threadsafe` / threading.Event
  - [x] 使用 `asyncio.Queue` + `asyncio.Task` 替代
  - [x] `stop()` 使用 `task.cancel()` 真正取消 HTTP 请求
  - 状态：✅ 已完成

- [x] **Task 3.2**: WebUserIO 完全移除
  - [x] WebUserIO 类已删除，input 流由 bridge 的 `_input_stream()` async generator 直接提供
  - 状态：✅ 已完成

- [x] **Task 3.3**: SessionManager 适配
  - [x] `start()` 适配新的 AgentBridge 接口（不再创建 WebUserIO、input_queue）
  - [x] 移除 `import queue` 和线程相关的生命周期管理代码
  - 状态：✅ 已完成

### 阶段四：测试与验证 [✅ 已完成]

- [x] **Task 4.1**: mutagent 单元测试更新
  - [x] 所有 agent/client/provider/toolset 相关测试改用 `pytest-asyncio` (auto mode)
  - [x] 验证 async generator 的事件流正确性
  - [x] 705 passed, 4 skipped
  - 状态：✅ 已完成

- [x] **Task 4.2**: mutbot 测试验证
  - [x] mutbot 全部 250 测试通过
  - 状态：✅ 已完成

### 阶段五：文档 [✅ 已完成]

- [x] **Task 5.1**: mutagent ADR 文档
  - [x] 在 `mutagent/docs/specifications/` 下编写架构决策记录，说明 async 改造的原因和决策
  - 状态：✅ 已完成

## 4. 测试验证

### 单元测试
- [x] Agent.run() async generator 正确 yield 事件
- [x] LLMClient.send_message() async 流式响应
- [x] ToolSet.dispatch() 同步/异步工具透明处理
- [x] CLI 适配层 queue 通信正确性
- 执行结果：mutagent 705/705 通过（4 跳过）

### 集成测试
- [ ] CLI 模式：input → agent → event 完整流程（需手动验证）
- [ ] CLI 模式：Ctrl+C 取消正在进行的 HTTP 请求（需手动验证）
- [x] Web 模式：mutbot 250/250 测试通过
- [ ] Web 模式：session stop 真正取消 HTTP 请求（需端到端验证）
- [ ] Web 模式：多 session 并发独立运行（需端到端验证）

---

### 实施进度总结
- ✅ **阶段一：mutagent 核心 async 改造** - 100% 完成 (4/4 任务)
- ✅ **阶段二：mutagent CLI 适配** - 100% 完成 (2/2 任务)
- ✅ **阶段三：mutbot 侧适配** - 100% 完成 (3/3 任务)
- ✅ **阶段四：测试与验证** - 100% 完成 (2/2 任务)
- ✅ **阶段五：文档** - 100% 完成 (1/1 任务)

**全部完成度：100%** (12/12 任务)
**mutagent 单元测试：705 passed, 4 skipped**
**mutbot 单元测试：250 passed**