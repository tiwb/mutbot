# Setup Wizard 设计规范

**状态**：✅ 已完成
**日期**：2026-03-03
**类型**：重构

## 背景

Setup Wizard 是 mutbot 首次使用时引导用户配置 LLM provider 的交互流程。原实现基于对话式状态机（`SetupProvider`，796 行），通过多轮文本消息引导用户输入配置信息。

重构目标：基于交互式 UI 框架（`feature-interactive-ui-tools.md`），将配置流程从文本状态机改为后端驱动 UI 表单，提升用户体验和代码可维护性。

## 整体架构

```
┌─────────────────────────────────────────────────────────┐
│ 前端                                                      │
│  App.tsx ──handleHeaderAction──→ session.run_setup RPC    │
│  MessageList.tsx ──error bubble──→ onSetupLLM callback   │
│  AgentPanel.tsx ──ui_event──→ WebSocket                  │
│  ToolCallCard.tsx ──ViewRenderer──→ 渲染 UI 组件          │
├─────────────────────────────────────────────────────────┤
│ 后端                                                      │
│  routes.py ──session.run_setup──→ 查找/创建 GuideSession  │
│  agent_bridge.py ──request_tool──→ 注入 Setup-llm 工具    │
│  guide.py ──NullProvider──→ 自然触发 Setup-llm            │
│  setup_toolkit.py ──SetupToolkit──→ 配置向导 UI 流程      │
│                    ──_activate()──→ 替换 agent.llm        │
└─────────────────────────────────────────────────────────┘
```

## 设计方案

### 核心原则：只保留两条路径

```
路径 1（自然触发）：
  用户发消息 → NullProvider 返回 Setup-llm tool_use → agent 内循环 dispatch
  → SetupToolkit.llm() → 用户配置 → _activate() → 真实 LLM 处理消息

路径 2（菜单/按钮触发）：
  用户点击 "Setup LLM" → RPC session.run_setup
  → 找/建 Guide Session → bridge.request_tool("Setup-llm")
  → _input_stream 拦截 → dispatch → SetupToolkit.llm()
```

**API 错误时的引导**：配置错误（错误的 key、不兼容的模型等）会导致 API 错误。错误消息气泡中显示 "Setup LLM" 按钮，用户点击即可重新配置（路径 2）。选择权在用户，不自动切换 provider。

### NullProvider — 无配置占位（路径 1）

位于 `guide.py`，满足 Agent 构造的 LLM 参数要求：

```python
class NullProvider(LLMProvider):
    async def send(self, model, messages, tools, ...):
        yield StreamEvent(type="text_delta", text="欢迎使用 MutBot！...")
        yield StreamEvent(type="response_done", response=Response(
            message=Message(role="assistant", blocks=[
                TextBlock(text=guide_text),
                ToolUseBlock(id=..., name="Setup-llm", input={}),
            ]),
            stop_reason="tool_use",
        ))
```

只做一件事——返回引导文本 + `Setup-llm` tool_use block。Agent 自动 dispatch 该工具，进入配置流程。配置完成后由 `_activate()` 直接替换 `agent.llm`，同一 Agent 实例无缝切换到真实 LLM。

### request_tool — 运行时注入（路径 2）

`AgentBridge` 支持运行时注入工具调用，不中断当前 agent task：

```python
def request_tool(self, name, tool_input=None):
    self._ensure_setup_toolkit()
    self._pending_tool_calls.append((name, tool_input or {}))
    trigger = Message(role="user", blocks=[TextBlock(text="[配置更新]")])
    self._input_queue.put_nowait(trigger)
```

`_input_stream` yield 前检查 pending → `_execute_pending_tools()`（广播 turn_start → tool_exec_start → dispatch → tool_exec_end → turn_done）→ yield 触发消息。

`cancel()` 中清空 `_pending_tool_calls`，防止取消后循环。

### session.run_setup RPC（路径 2 后端入口）

专用 RPC，自动查找或创建 Guide Session：

```python
@workspace_rpc.method("session.run_setup")
async def handle_run_setup(params, ctx):
    # 1. 找已有 Guide Session（未停止的）
    # 2. 没有则创建 + 广播 session_created
    # 3. 确保 bridge 已启动
    # 4. bridge.request_tool("Setup-llm")
    # 5. 返回 { ok: true, session_id }
```

前端 `handleHeaderAction` 和 `onSetupLLM` 均调用此 RPC，不依赖 `activeSessionId`，返回的 `session_id` 用于打开/聚焦 tab。

---

## SetupToolkit 详细设计

### 类继承

```python
class SetupToolkit(UIToolkit):
    """LLM 配置向导，基于 UIToolkit 提供交互式表单。"""
```

继承 `UIToolkit`（→ `ToolSet`），利用 UI 框架的 `set_view()` / `wait_event()` / `show()` / `close()` 驱动前端表单。

### 配置向导主流程

```
Setup-llm 工具入口 (llm())
    │
    ├─ 无 provider → _add_provider_flow() → 添加第一个 provider
    │
    └─ 有 provider → _show_provider_list() 主循环
                         │
                         ├─ "Add" → _add_provider_flow()
                         ├─ "Edit" → _edit_provider()
                         ├─ "Delete" → _delete_provider()
                         └─ "Done" → 验证 + _activate()
```

### Provider 类型定义

```python
_PROVIDER_DEFS = {
    "copilot": {
        "label": "GitHub Copilot",
        "protocol": "copilot",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "protocol": "anthropic",
        "default_url": "https://api.anthropic.com",
        "provider_cls": "AnthropicProvider",
        "default_models": ["claude-sonnet-4", "claude-haiku-4.5", "claude-opus-4"],
    },
    "openai": {
        "label": "OpenAI (GPT)",
        "protocol": "openai",
        "default_url": "https://api.openai.com/v1",
        "provider_cls": "OpenAIProvider",
        "default_models": ["gpt-4.1", "gpt-4.1-mini", "o3"],
    },
}
```

- **Copilot**：OAuth device flow → GitHub token → 模型获取
- **Anthropic/OpenAI**：API Base URL + API Key → 模型获取；URL 可编辑，支持自定义端点

### Provider 配置流程

**API Provider（Anthropic/OpenAI）** — `_configure_api_provider()`：

```
选择 provider 类型 (select, auto_submit)
    → 表单：API Base URL / API Key (secret) / Provider Name
    → _fetch_models(base_url, api_key, protocol)
    → 选择模型 (multi-select, scrollable)
    → _save_provider()
```

**Copilot** — `_configure_copilot()`：

```
获取 device code → 显示 user_code + 验证 URL
    → 轮询等待 OAuth 授权 (spinner)
    → _fetch_copilot_models(token)
    → 选择模型 (multi-select)
    → _save_provider()
```

### 模型发现与排序

`_prioritize_models(models_with_ts)` 按 family 分组、按新鲜度排序：

1. 提取 family（去除 `-mini`、`-turbo`、`-preview` 等后缀）
2. 按主前缀分组（`gpt`、`claude`、`o1` 等）
3. 每个前缀保留最新 2 个 family（`_FEATURED_FAMILIES_PER_PREFIX`）
4. Featured families 在前，其余在后，均按新鲜度降序

### 配置文件格式

写入 `~/.mutbot/config.json`：

```json
{
  "providers": {
    "copilot": {
      "provider": "mutbot.copilot.provider.CopilotProvider",
      "github_token": "ghu_...",
      "models": ["claude-sonnet-4", "gpt-4.1"]
    },
    "my-claude": {
      "provider": "AnthropicProvider",
      "base_url": "https://api.anthropic.com",
      "auth_token": "sk-ant-...",
      "models": ["claude-sonnet-4", "claude-haiku-4.5"]
    }
  },
  "default_model": "claude-sonnet-4"
}
```

- `_write_config(new_data)` 合并写入（保留已有 providers，按 key 覆盖）
- `_save_provider(key, config)` 单个 provider 保存
- `_write_full_config(config)` 全量覆盖（删除操作使用）

### _activate() — 切换到真实 LLM

```python
def _activate(self, config):
    mutbot_config = load_mutbot_config()
    client = create_llm_client(mutbot_config, ...)
    agent = self.owner.agent        # 通过 ToolSet 绑定链获取 Agent
    agent.llm = client              # 直接替换 NullProvider → 真实 LLM
```

关键设计：同一 Agent 实例，只替换 `llm` 属性。Setup 完成后 Agent 继续 `step()`，此时已是真实 LLM，自然处理用户原始消息。

---

## GuideSession 配置

```python
class GuideSession(AgentSession):
    def create_agent(self, config, ...):
        if config.get("providers"):
            client = create_llm_client(config, ...)   # 真实 LLM
        else:
            client = LLMClient(provider=NullProvider(), model="setup-wizard")

        tool_set.add(SetupToolkit())   # 始终添加，允许随时重新配置
```

- 无配置时用 NullProvider 占位，有配置时用真实 LLM
- SetupToolkit 始终注册（不仅限首次配置，用户可随时通过菜单重新配置）

## 前端集成

### 错误消息 Setup LLM 按钮

`MessageList.tsx` 的 error case 中添加 "Setup LLM" 按钮：

```typescript
case "error":
  return (
    <div className="message-bubble assistant error">
      ...
      {onSetupLLM && (
        <button className="error-setup-btn" onClick={onSetupLLM}>Setup LLM</button>
      )}
    </div>
  );
```

`onSetupLLM` 回调由 `AgentPanel` 传入，调用 `session.run_setup` RPC。

### 首次启动引导

`handle_app_workspace_create` 中检测无 LLM 配置时，自动创建 Guide Session 并通过 `queue_event("open_session")` 让前端自动打开 Guide tab。

## 已知问题

- **useEffect 的 view 引用稳定性**：ViewRenderer 的 `useEffect([view])` 依赖 view 对象引用。如果后续发现表单值异常重置，可改用 `JSON.stringify(view)` 做深比较。
- **被 stop 的工具前端仍显示运行状态**：后续工具迭代统一处理。

## 关键参考

### 源码

- `src/mutbot/builtins/guide.py` — GuideSession + NullProvider
- `src/mutbot/builtins/setup_toolkit.py` — SetupToolkit CRUD + 模型发现 + Copilot OAuth
- `src/mutbot/web/agent_bridge.py` — request_tool / _execute_pending_tools / _input_stream / cancel
- `src/mutbot/web/routes.py` — session.run_setup / session.run_tool RPC
- `src/mutbot/runtime/session_impl.py` — get_bridge() / start() / list_by_workspace()
- `frontend/src/App.tsx` — handleHeaderAction (run_setup_wizard)
- `frontend/src/panels/AgentPanel.tsx` — handleUIEvent / onSetupLLM
- `frontend/src/components/ToolCallCard.tsx` — ViewRenderer / UIComponent
- `frontend/src/components/MessageList.tsx` — onSetupLLM / error bubble

### 相关规范

- `docs/specifications/feature-interactive-ui-tools.md` — UIToolkit / UIContext 设计
- `docs/specifications/feature-session-list-management.md` — Session 生命周期
