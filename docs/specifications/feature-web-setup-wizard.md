# Web 配置向导 — 实施规范

**状态**：✅ 已完成
**日期**：2026-02-26
**类型**：功能设计
**总体规划**：[mutbot.ai feature-website-github-pages.md](../../mutbot.ai/docs/specifications/feature-website-github-pages.md) Phase 4

## 1. 背景

当前 mutbot 首次运行无 LLM 配置时，使用 CLI 交互式向导（`mutbot/cli/setup.py`）完成 provider 配置。用户必须在终端中选择 provider、输入 API Key，完成后重启进入 Web 界面。

**目标**：mutbot 无 LLM 配置时直接启动 Web 服务器，用户通过聊天界面完成首次配置。配置完成后，同一 session 无缝切换为真实 LLM 驱动，用户可直接对话测试。

**核心设计**：配置向导集成在 GuideSession 中。`create_agent()` 检测无 LLM 配置时使用 `SetupProvider`（脚本化状态机）替代真实 LLM。配置完成后，Provider 内部切换为真实 LLMProvider，同一 session 变成可用的 AI 向导。不使用 tool call，前端零改动。

**不包含**：`${browser:key}` 配置来源（Phase 3）、GitHub 登录（Phase 5）、跨设备同步（Phase 5）。

## 2. 设计方案

### 2.1 架构概览

```
┌───────────────────────────────────────────────────────────┐
│  GuideSession.create_agent()                              │
│                                                           │
│  config 有 providers?                                     │
│  ├─ 是 → create_llm_client(config) → 真实 LLMProvider    │
│  └─ 否 → SetupProvider()          → 脚本化状态机         │
│                                                           │
│  其余完全一致：tools、system_prompt、Agent 构造           │
└───────────────────────┬───────────────────────────────────┘
                        │
         ┌──────────────┴──────────────┐
         │  现有基础设施（完全复用）     │
         │                             │
         │  Agent.run() → LLMClient    │
         │  AgentBridge → WebSocket    │
         └─────────────────────────────┘
```

### 2.2 GuideSession 变更

仅在 `create_agent()` 中增加一个 `if` 分支：

```python
# mutbot/builtins/guide.py

class GuideSession(AgentSession):
    """向导 Agent Session"""

    display_name = "Guide"
    display_icon = "circle-question-mark"

    system_prompt: str = "你是 MutBot 的向导 ..."  # 不变

    def create_agent(self, config, log_dir=None, session_ts="", messages=None, **kwargs):
        from mutagent.client import LLMClient
        from mutbot.toolkits.session_toolkit import SessionToolkit
        from mutbot.runtime.session_impl import setup_environment, create_llm_client

        setup_environment(config)

        # --- 唯一变更点 ---
        if config.get("providers"):
            client = create_llm_client(config, self.model, log_dir, session_ts)
        else:
            from mutbot.builtins.setup_provider import SetupProvider
            client = LLMClient(
                provider=SetupProvider(),
                model="setup-wizard",
            )
        # --- 变更结束 ---

        # tools + system_prompt 始终设置
        # setup 阶段 SetupProvider 忽略；切换后真实 LLM 直接可用
        session_manager = kwargs.get("session_manager")
        session_tools = SessionToolkit(
            session_manager=session_manager,
            workspace_id=self.workspace_id,
        )

        tool_set = ToolSet()
        tool_set.add(session_tools)

        agent = Agent(
            client=client,
            tool_set=tool_set,
            system_prompt=self.system_prompt,
            messages=messages if messages is not None else [],
        )
        tool_set.agent = agent
        return agent
```

### 2.3 SetupProvider

#### 整体结构

`send()` 是 async generator。Setup 阶段由各 handler 生成事件流（可能含异步等待，如 OAuth 轮询）。配置完成后直接代理到真实 provider。

```python
# mutbot/builtins/setup_provider.py

class SetupProvider(LLMProvider):
    """脚本化 LLM Provider — 配置完成后代理到真实 provider。

    实例变量维护状态机。配置完成后创建真实 LLMProvider，
    后续 send() 直接代理，同一 session 无缝切换。
    """

    def __init__(self):
        self._state: str = "WELCOME"
        self._context: dict = {}
        self._real_provider = None
        self._real_model: str = ""

    @classmethod
    def from_config(cls, model_config):
        return cls()

    async def send(self, model, messages, tools, system_prompt="", stream=True):
        # 已完成配置 → 代理到真实 provider
        if self._real_provider:
            async for event in self._real_provider.send(
                self._real_model, messages, tools, system_prompt, stream
            ):
                yield event
            return

        # Setup 阶段 → 状态机
        last_user_text = ""
        for msg in reversed(messages):
            if msg.role == "user" and msg.content:
                last_user_text = msg.content.strip()
                break

        async for event in self._dispatch(last_user_text):
            yield event
```

#### 事件生成辅助

```python
def _reply(self, text: str) -> AsyncIterator[StreamEvent]:
    """生成一条完整的文本响应（text_delta + response_done）。"""
    yield StreamEvent(type="text_delta", text=text)
    yield StreamEvent(type="response_done", response=Response(
        message=Message(role="assistant", content=text),
        stop_reason="end_turn",
    ))
```

#### 状态机

```python
async def _dispatch(self, user_input: str) -> AsyncIterator[StreamEvent]:
    # WELCOME / AWAIT_CHOICE / AWAIT_KEY / AWAIT_CUSTOM_URL / AWAIT_CUSTOM_KEY
    # / AWAIT_MODEL / AWAIT_MANUAL_MODEL / COPILOT_POLLING
    ...
```

#### 状态流转

```
WELCOME ──→ AWAIT_CHOICE
                 │
    ┌────────┬───┼────────┬──────────┐
    ▼        ▼   ▼        ▼          ▼
  "1"      "2"  "3"     "4"        "5"
    │        │   │        │          │
    ▼        │   │        ▼          ▼
 (copilot   │   │    AWAIT_CUSTOM_URL
  inline    │   │        │
  auth)     │   │        ▼
    │       │   │    AWAIT_CUSTOM_KEY
    │       ▼   ▼        │
    │     AWAIT_KEY       │
    │        │            ├── fetch ok ──→ AWAIT_MODEL
    │        │            └── fetch fail → AWAIT_MANUAL_MODEL
    │        │                                    │
    │        ├── anthropic → hardcoded models ─┐  │
    │        └── openai → fetch + fallback ────┤  │
    │                                          │  │
    │            ┌─────────────────────────────┘  │
    │            ▼                                │
    │        AWAIT_MODEL ←────────────────────────┘
    │            │
    │            ▼ (用户选择模型)
    └────────┬───┘
             ▼
       _activate()  ── 保存 config → 创建真实 provider
             │         设置 self._real_provider
             ▼
       返回完成消息
             │
             │  下一次 send()
             └──→ 代理到真实 provider（兼容 sync/async generator）
```

#### Provider 选择

5 个选项：GitHub Copilot、Anthropic、OpenAI、Custom Anthropic-compatible、Custom OpenAI-compatible。选项 4/5 分开而非合并，避免用户需要在 URL 中嵌入协议标记。

#### Copilot OAuth — 自动轮询

选择 Copilot 后，`send()` 内部完成全部流程：请求 device code → 展示验证码 → 异步轮询 → 自动检测授权完成。用户无需输入 "done"。

```python
async def _do_copilot_auth(self) -> AsyncIterator[StreamEvent]:
    """Copilot OAuth Device Flow — 全流程在一次 send() 内完成。"""
    import asyncio

    # 1. 请求 device code
    device_data = await self._request_device_code()
    verification_uri = device_data["verification_uri"]
    user_code = device_data["user_code"]
    device_code = device_data["device_code"]
    interval = device_data.get("interval", 5)

    # 2. 展示验证码
    code_text = (
        f"Great! Let's connect your GitHub account.\n\n"
        f"Please visit this URL and enter the code:\n\n"
        f"🔗 {verification_uri}\n"
        f"📋 Code: **{user_code}**\n\n"
        f"Waiting for authorization..."
    )
    yield StreamEvent(type="text_delta", text=code_text)

    # 3. 异步轮询（最多 5 分钟）
    # 用户在浏览器授权后自动检测，无需手动确认
    # 用户点取消按钮 → CancelledError → 中断轮询
    self._state = "COPILOT_POLLING"
    token = None
    max_attempts = 300 // interval  # ~5 分钟

    for _ in range(max_attempts):
        await asyncio.sleep(interval)
        token = await self._poll_github_token(device_code)
        if token:
            break

    # 4. 结果
    if token:
        self._context["github_token"] = token
        result_text = await self._activate(provider="copilot")
        yield StreamEvent(type="text_delta", text="\n\n" + result_text)
        full_text = code_text + "\n\n" + result_text
    else:
        self._state = "AWAIT_CHOICE"
        timeout_text = (
            "\n\nAuthorization timed out. "
            "Please choose a provider to try again.\n\n"
            + self._choice_text()
        )
        yield StreamEvent(type="text_delta", text=timeout_text)
        full_text = code_text + timeout_text

    yield StreamEvent(type="response_done", response=Response(
        message=Message(role="assistant", content=full_text),
        stop_reason="end_turn",
    ))
```

**取消处理**：轮询期间用户点击取消按钮 → `AgentBridge.cancel()` 取消 asyncio task → `CancelledError` 在 `asyncio.sleep()` 处传播 → `send()` 中断 → `AgentBridge._commit_partial_state()` 提交部分消息。下一条用户消息时，`_dispatch()` 检测到 `_state == "COPILOT_POLLING"` → 重置为 `AWAIT_CHOICE`。

**GitHub API 异步调用**：

```python
async def _request_device_code(self) -> dict:
    """请求 GitHub device code（在线程中执行同步 HTTP）。"""
    import asyncio, requests
    def _request():
        resp = requests.post(
            "https://github.com/login/device/code",
            headers={"Accept": "application/json"},
            data={"client_id": GITHUB_CLIENT_ID, "scope": "read:user"},
        )
        resp.raise_for_status()
        return resp.json()
    return await asyncio.get_event_loop().run_in_executor(None, _request)

async def _poll_github_token(self, device_code: str) -> str | None:
    """单次轮询 GitHub token（在线程中执行同步 HTTP）。"""
    import asyncio, requests
    def _poll():
        resp = requests.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": GITHUB_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
        )
        data = resp.json()
        error = data.get("error")
        if error in ("authorization_pending", "slow_down"):
            return None
        if error:
            raise RuntimeError(f"OAuth error: {error}")
        return data.get("access_token")
    return await asyncio.get_event_loop().run_in_executor(None, _poll)
```

#### API Key 流程 — 模型发现替代验证

与 CLI setup.py 行为一致：**模型列表获取即验证**，不单独验证 API Key。

- **Anthropic**：硬编码模型列表 `["claude-sonnet-4", "claude-haiku-4.5", "claude-opus-4"]` → 直接进入 AWAIT_MODEL
- **OpenAI**：调用 `/v1/models` 动态获取（chat_filter），失败时 fallback 到 `["gpt-4.1", "gpt-4.1-mini", "o3"]` → AWAIT_MODEL
- **Custom API**：调用 `/models` 或 `/v1/models` → 成功进入 AWAIT_MODEL / 失败进入 AWAIT_MANUAL_MODEL

#### 模型选择（AWAIT_MODEL）

在聊天中展示编号列表，用户输入编号（逗号分隔多选）：

```
Available models:

1. **gpt-4.1** (recommended)
2. **gpt-4.1-mini**
3. **o3**

Type **a** to see all 25 models.

Select models (type numbers separated by commas, or **all** to select all):
```

支持：编号选择（`1,2`）、"all"（全选）、"a"（展开全部列表，超过 10 个时可用）、直接输入模型名。

#### 手动模型输入（AWAIT_MANUAL_MODEL）

Custom API 获取模型失败时，提示用户手动输入 model ID。

#### Sync → Async Generator 兼容

部分 LLMProvider（如 CopilotProvider）的 `send()` 返回同步 generator。SetupProvider 代理时通过 `_wrap_sync_iter()` 在线程中运行同步迭代，避免阻塞事件循环：

```python
async def _wrap_sync_iter(sync_gen):
    """Wrap sync iterator for async consumption (runs in thread pool)."""
    loop = asyncio.get_running_loop()
    q = asyncio.Queue()
    def _producer():
        for item in sync_gen:
            loop.call_soon_threadsafe(q.put_nowait, item)
        loop.call_soon_threadsafe(q.put_nowait, _DONE)
    loop.run_in_executor(None, _producer)
    while True:
        item = await q.get()
        if isinstance(item, _Done): return
        yield item
```

`send()` 代理逻辑：
```python
gen = self._real_provider.send(...)
if hasattr(gen, '__aiter__'):
    async for event in gen: yield event  # Async provider
else:
    async for event in _wrap_sync_iter(gen): yield event  # Sync provider
```

#### 配置保存与 Provider 切换

```python
async def _activate(self, provider: str) -> str:
    """保存配置并切换到真实 LLM provider。"""
    config_data = self._build_provider_config(provider)
    self._save_config(config_data)

    # 创建真实 LLMProvider — 后续 send() 直接代理
    from mutbot.runtime.session_impl import create_llm_client
    from mutbot.runtime.config import load_mutbot_config
    config = load_mutbot_config()
    client = create_llm_client(config)
    self._real_provider = client.provider
    self._real_model = client.model

    return (
        f"✅ {provider.title()} configured! "
        f"Using **{self._real_model}** as default model.\n\n"
        f"You can now chat with me — I'm powered by a real AI! "
        f"Try saying something to test the connection."
    )

def _save_config(self, data: dict) -> None:
    """合并写入 ~/.mutbot/config.json。"""
    # 复用 cli/setup.py 中的 merge 逻辑
    ...
```

### 2.4 为什么集成到 GuideSession

1. **零开销**：`config.get("providers")` 在 `create_agent()` 中执行一次，不影响后续消息。
2. **用户视角统一**：首次用户看到 "Guide"，配置完成后它就是真正的向导。
3. **代码简洁**：GuideSession 只多了一个 `if/else`。SetupProvider 是独立模块。
4. **自然过渡**：setup 完成后 tools（SessionToolkit）和 system_prompt 已就绪，Guide 完整能力立即生效。
5. **不阻塞其他功能**：setup 向导只影响这一个 Guide session。用户可以同时创建 Terminal、Document 等不依赖 LLM 的 session。

### 2.5 对话示例

```
[Guide]:
👋 Welcome to MutBot! Let's set up your AI provider.

Which provider would you like to use?

1. **GitHub Copilot** — free with GitHub account
2. **Anthropic** — Claude API
3. **OpenAI** — GPT API
4. **Custom (Anthropic-compatible)** — third-party Anthropic API
5. **Custom (OpenAI-compatible)** — third-party OpenAI API

Type a number to continue.

[User]: 1

[Guide]:
Great! Let's connect your GitHub account.

Please visit this URL and enter the code:

🔗 https://github.com/login/device
📋 Code: **ABCD-1234**

Waiting for authorization...

✅ Copilot configured! Using **claude-sonnet-4** as default model.

You can now chat with me — I'm powered by a real AI!
Try saying something to test the connection.

[User]: Hello! What can you do?

[Guide]:                                  ← 真实 LLM，Guide 完整能力
你好！我是 MutBot 的向导。我可以帮你：
- 了解 MutBot 的功能
- 创建专业的 Agent Session（研究、编码等）
- 回答基础问题
...
```

注意 Copilot 流程：用户输入 "1" 后，一条消息内完成全部流程。展示验证码 → 自动等待 → 授权完成后自动继续。用户只需在浏览器中授权，无需回来输入 "done"。

### 2.6 启动流程变更

```python
# __main__.py — 移除 CLI 向导
config = load_mutbot_config()
# 始终启动 Web 服务器，不再调用 CLI 向导
uvicorn.run(...)
```

`server.py` lifespan 中：

```python
from mutbot.runtime.config import load_mutbot_config

config = load_mutbot_config()
ws = workspace_manager.ensure_default()

if not config.get("providers"):
    # Setup 模式：跳过 LLM proxy 初始化，自动创建向导 session
    _ensure_setup_session(ws, session_manager, workspace_manager)
else:
    _load_proxy_config()
```

### 2.7 `/api/health` 扩展

```json
{
  "status": "ok",
  "api_version": "1.0.0",
  "setup_required": true
}
```

`setup_required` 字段让 mutbot.ai 前端也能识别 setup 状态。

### 2.8 服务端自动创建向导 Session 并触发欢迎消息

无 LLM 配置时，服务端在启动阶段自动创建 GuideSession 并预设 `initial_message`，确保用户打开浏览器即看到配置向导。

#### 创建逻辑

```python
# server.py

def _ensure_setup_session(ws, session_manager, workspace_manager):
    """确保 setup 模式下 workspace 有一个可用的 Guide session。"""
    guide_type = "mutbot.builtins.guide.GuideSession"
    existing = session_manager.list_by_workspace(ws.id)
    guide = next(
        (s for s in existing
         if s.type == guide_type and s.status == "active"),
        None,
    )

    if guide is None:
        # 首次启动：创建 Guide session，带 initial_message 触发欢迎
        guide = session_manager.create(
            ws.id,
            session_type=guide_type,
            config={"initial_message": "__setup__"},
        )
        ws.sessions.append(guide.id)
        workspace_manager.update(ws)
        logger.info("Setup mode: created Guide session %s", guide.id)
    elif "initial_message" not in guide.config:
        # 重启恢复：上次 initial_message 已消费，重新注入
        guide.config["initial_message"] = "__setup__"
        session_manager._persist(guide)
        logger.info("Setup mode: re-injected initial_message for session %s", guide.id)
```

#### 隐藏触发消息（Hidden InputEvent）

`initial_message` 仅用于触发 SetupProvider 的 WELCOME 状态，不应在聊天界面显示为用户消息。通过 `InputEvent.data` 传递 `hidden` 标记：

**AgentBridge.send_message 变更**：

```python
# agent_bridge.py — send_message 增加 hidden 支持

def send_message(self, text: str, data: dict | None = None) -> None:
    event = InputEvent(type="user_message", text=text, data=data or {})
    hidden = (data or {}).get("hidden", False)
    if not hidden:
        # 正常消息：广播到前端 + 推送 thinking 状态
        user_event = {"type": "user_message", "text": text, "data": data or {}}
        if self.event_recorder:
            self.event_recorder(user_event)
        asyncio.ensure_future(self.broadcast_fn(self.session_id, user_event))
        asyncio.ensure_future(self._broadcast_status("thinking"))
    # 入队放在 ensure_future 之后，确保广播先于 agent 处理（FIFO 调度）
    self._input_queue.put_nowait(event)
```

**Agent.run 变更**：

```python
# mutagent/builtins/agent_impl.py — run() 中处理 hidden

async for input_event in input_stream:
    if input_event.type == "user_message":
        if not input_event.data.get("hidden"):
            self.messages.append(Message(role="user", content=input_event.text))
        # ... 后续 step 循环不变
```

**SessionManager.start 变更**：

```python
# session_impl.py — initial_message 以 hidden 方式发送

initial_message = session.config.pop("initial_message", None)
if initial_message:
    bridge.send_message(initial_message, data={"hidden": True})
    self._persist(session)
```

#### 效果

1. 用户打开浏览器 → Guide session 自动打开（通过 `open_session` 事件，见 2.9）
2. WebSocket 连接 → `SessionManager.start()` → bridge 发送 hidden 触发消息
3. SetupProvider WELCOME 状态生成欢迎消息 → 前端显示 assistant 消息
4. **聊天界面无 "fake" 用户消息**，对话直接从 Guide 的欢迎消息开始
5. 前端零改动：hidden 逻辑完全在后端处理

#### 重启恢复

用户未完成配置就退出时：
- Session 元数据和历史消息已持久化
- 下次启动：`_ensure_setup_session()` 检测到已有活跃 Guide session → 重新注入 `initial_message`
- 用户打开 session → SetupProvider 从 WELCOME 重新开始
- 注意：SetupProvider 状态机不持久化（仅在内存中），重启后总是从 WELCOME 开始

### 2.9 `open_session` 事件推送 — 后端控制前端打开 Session Tab

后端需要能主动让前端打开指定 session 的 tab（如 setup 向导自动打开 Guide）。通过 WebSocket 事件推送实现，配合 pending 队列解决启动时序问题。

#### 机制

**ConnectionManager 扩展**（`connection.py`）：

```python
class ConnectionManager:
    def __init__(self):
        self._connections: dict[str, set[WebSocket]] = {}
        self._pending_events: dict[str, list[dict]] = {}

    def queue_event(self, key: str, event: str, data: dict | None = None) -> None:
        """入队事件。前端连接后自动 flush。"""
        msg = {"type": "event", "event": event, "data": data or {}}
        self._pending_events.setdefault(key, []).append(msg)

    async def connect(self, session_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.setdefault(session_id, set()).add(websocket)
        # Flush pending events to the newly connected client
        pending = self._pending_events.pop(session_id, None)
        if pending:
            for event in pending:
                await websocket.send_json(event)
```

**服务端调用**（`server.py`）：

```python
def _ensure_setup_session(ws, sm, wm):
    # ... 创建或恢复 Guide session ...

    # 入队 open_session 事件，前端 WebSocket 连接后自动 flush
    workspace_connection_manager.queue_event(
        ws.id, "open_session", {"session_id": guide.id},
    )
```

**前端处理**（`App.tsx`）：

```typescript
wsRpc.on("open_session", async (data) => {
    const sessionId = data.session_id as string;
    if (!sessionId) return;
    const session = await wsRpc.call("session.get", { session_id: sessionId });
    addTabForSession(session);  // 复用已有的 tab 打开流程
});
```

#### 时序

```
服务器启动 → _ensure_setup_session()
    → 创建 Guide session
    → queue_event("open_session", {session_id: guide.id})
    → 事件暂存在 pending 队列

前端加载 → 连接 Workspace WebSocket
    → connect() flush pending events
    → 前端收到 open_session 事件
    → session.get RPC 获取 session 详情
    → addTabForSession() 打开 Guide tab
```

#### 通用能力

`open_session` 事件不限于 setup 场景。任何后端代码（如 Agent 创建子 session 后希望自动打开）都可以使用 `queue_event` 或直接 `broadcast` 来触发前端打开 tab。

### 2.10 已知问题：前端 WS 重连时事件丢失

**现象**：React Strict Mode（开发模式）导致组件 mount → unmount → remount，session WebSocket 经历断连重连。如果 agent 响应在第一次 WS 连接期间广播，但前端尚未处理就 unmount，事件可能丢失。

**根因**：AgentPanel 的 `session.events` RPC 仅在首次挂载且无缓存时调用。WS 重连后不会重新加载，依赖实时 WebSocket 推送。

**影响**：开发模式下偶现聊天消息丢失；生产模式不受 Strict Mode 影响，但网络断连场景也可能触发。

**修复方案**：AgentPanel 每次 WS `onOpen` 时都调用 `session.events` RPC 加载事件（event_id 去重机制已存在），确保 catch up 所有历史事件。此修复为通用可靠性改进，不限于 setup 向导场景。

## 3. 后续任务

### 重新配置 / 添加 Provider
已有 LLM 后，Guide 本身由真实 LLM 驱动，用户可以直接用自然语言请求"帮我添加一个新的 provider"或"重新配置 LLM"。Guide 可以通过 tool 调用 SetupProvider 的配置逻辑，或引导用户完成操作。这比首次配置简单得多，作为后续任务实现。

## 4. 实施步骤清单

### 阶段一：SetupProvider [✅ 已完成]
- [x] **Task 1.1**: SetupProvider 核心
  - [x] 实现 `LLMProvider` 接口（async generator `send()`）
  - [x] 实例变量状态机（`_state` + `_context`）
  - [x] 欢迎消息 + 选项展示（5 个选项，Anthropic/OpenAI custom 分开）
  - [x] 代理切换逻辑（`_real_provider` 透传，兼容 sync/async generator）
  - 状态：✅ 已完成

- [x] **Task 1.2**: Copilot OAuth 流程
  - [x] 异步 device code 请求（`run_in_executor`）
  - [x] 异步自动轮询（`asyncio.sleep` 循环，最多 5 分钟）
  - [x] 取消处理（`CancelledError` → `COPILOT_POLLING` → 恢复到选项）
  - [x] 超时处理
  - 状态：✅ 已完成

- [x] **Task 1.3**: API Key / Custom 流程 + 模型发现
  - [x] API Key 输入（Anthropic 硬编码模型，OpenAI 动态 fetch + fallback）
  - [x] Custom API 流程（URL + Key → fetch models / 手动输入）
  - [x] `_fetch_models_async()` — 异步模型发现（ported from CLI `_fetch_models`）
  - [x] 模型优先级排序（`_prioritize_models` ported from CLI）
  - [x] 验证失败回到 AWAIT_CHOICE
  - 状态：✅ 已完成

- [x] **Task 1.4**: 模型选择 + 配置保存
  - [x] AWAIT_MODEL 状态：编号列表展示 + 多选解析
  - [x] AWAIT_MANUAL_MODEL 状态：手动输入 model ID（Custom API fetch 失败时）
  - [x] Config 构建 + 合并写入 `~/.mutbot/config.json`
  - [x] `_activate()`：创建真实 provider → 设置 `_real_provider`
  - 状态：✅ 已完成

### 阶段二：GuideSession 集成 + 启动变更 [✅ 已完成]
- [x] **Task 2.1**: GuideSession 变更
  - [x] `create_agent()` 中检测 `config.get("providers")`
  - [x] 无 providers → 使用 SetupProvider
  - 状态：✅ 已完成

- [x] **Task 2.2**: 启动流程变更
  - [x] `__main__.py` 移除 CLI 向导自动调用
  - [x] `server.py` setup 模式跳过 LLM proxy 初始化
  - [x] `server.py` 实现 `_ensure_setup_session()`：自动创建 GuideSession + 重启恢复
  - [x] `AgentBridge.send_message()` 支持 `hidden` data 标记（跳过广播 user_message 和 thinking 状态）
  - [x] `AgentBridge.send_message()` 消息顺序修复：`put_nowait` 放在 `ensure_future` 之后
  - [x] `Agent.run()` 支持 `hidden` data 标记（跳过添加 user Message）
  - [x] `SessionManager.start()` initial_message 以 `hidden` 方式发送
  - [x] `/api/health` 添加 `setup_required` 字段
  - 状态：✅ 已完成

- [x] **Task 2.3**: `open_session` 事件推送
  - [x] `ConnectionManager` 增加 `_pending_events` 队列和 `queue_event()` 方法
  - [x] `connect()` 时自动 flush pending events 给新客户端
  - [x] `_ensure_setup_session()` 使用 `queue_event("open_session", ...)` 入队
  - [x] 前端 `App.tsx` 添加 `open_session` 事件 handler（通过 RPC 获取 session → `addTabForSession()`）
  - 状态：✅ 已完成

### 阶段三：验证 [✅ 已完成]
- [x] **Task 3.1**: 端到端测试
  - [x] 全新安装：无 config → 服务端自动创建 Guide session → 前端自动打开 Guide tab → 欢迎消息显示 → 聊天配置 → 同 session AI 对话
  - [x] Custom API 流程（模型发现 + 模型选择 + 配置保存 + 真实 LLM 代理）
  - [x] 消息顺序正确、hidden 消息不显示、思考状态正确
  - 状态：✅ 已完成

## 5. 测试验证

### 单元测试（55 tests — `test_setup_provider.py` + `test_setup_integration.py`）

**SetupProvider 状态机**（11 tests）：
- [x] WELCOME → AWAIT_CHOICE 转换 + 欢迎消息内容
- [x] 5 个 provider 选择（Anthropic/OpenAI/Custom×2/无效输入）
- [x] 各状态 cancel 回到 AWAIT_CHOICE（4 个状态）
- [x] COPILOT_POLLING 中断恢复

**API Key 流程**（3 tests）：
- [x] Anthropic key → 硬编码模型列表 → AWAIT_MODEL
- [x] OpenAI key → fetch 成功 → AWAIT_MODEL
- [x] OpenAI key → fetch 失败 → fallback 硬编码模型

**Custom API 流程**（4 tests）：
- [x] URL 输入验证（有效/无效）
- [x] Key + fetch 成功 → AWAIT_MODEL
- [x] Key + fetch 失败 → AWAIT_MANUAL_MODEL

**模型选择**（10 tests）：
- [x] 编号选择（单选/多选/混合编号名称）
- [x] "all" 全选、"a" 展开全部
- [x] 空输入/去重
- [x] 手动模型输入（AWAIT_MANUAL_MODEL）

**Sync→Async adapter**（4 tests）：
- [x] 基本迭代、空 generator、异常传播、StreamEvent 传递

**send() 代理**（2 tests）：
- [x] Async generator provider
- [x] Sync generator provider（如 CopilotProvider）

**配置**（9 tests）：
- [x] 5 种 provider 配置构建
- [x] 新建/合并写入 config.json
- [x] 模型优先级排序（family/empty/single）

**集成**（10 tests）：
- [x] ConnectionManager pending events 入队/多事件/flush/无 pending
- [x] AgentBridge hidden 消息不广播/正常消息广播/入队
- [x] _ensure_setup_session 首次创建/重启恢复/已有 initial_message

### 集成测试（手动验证）
- [x] 全新安装 → 自动创建 Guide session → `open_session` 事件 → 前端自动打开 tab → 欢迎消息显示 → 聊天配置 → 同 session AI 对话可用
- [x] Setup 完成后无需重启、无需切换 session
