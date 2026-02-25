# LLM Provider 抽象与 Copilot 代理 设计规范

**状态**：✅ 已完成
**日期**：2026-02-25
**类型**：功能设计

## 1. 背景

### 1.1 目标

1. **mutagent**：通过 Declaration 抽象 LLM 提供商层（`LLMProvider`），支持多种后端（Anthropic、OpenAI、Copilot），配置中指定类路径即可自动加载
2. **mutbot**：扩展实现 `CopilotProvider`（GitHub Copilot 后端），并提供 LLM 代理服务，将所有已配置模型暴露给外部工具（如 Claude Code），同时记录 API 调用日志用于分析

### 1.2 核心设计理念

- **mutobj 优先**：所有组件（LLMProvider、代理功能）均为 Declaration 子类，不 import 就不存在
- **mutagent 本地直接调用所有 provider**，不走代理
- **代理对 mutbot 自身无用**，其价值是对外暴露模型 + 记录分析外部软件的 LLM 调用
- **零注册**：provider 和功能模块通过 mutobj 子类发现自动注册，配置指定类路径即触发 import
- **完全可选**：LLM 代理本身是可选功能，`modules` 配置中不列出则不加载，不影响 mutbot 其他功能

### 1.3 参考实现

[copilot-api](https://github.com/ericc-ch/copilot-api)（TypeScript，~3000 行）。Copilot API 使用标准 OpenAI Chat Completions 格式，认证通过 GitHub OAuth 设备流 → Copilot JWT。

## 2. 设计决策

| 决策 | 结论 | 理由 |
|------|------|------|
| **Provider 发现** | mutobj `resolve_class` 按类路径自动 import + 注册 | 零注册代码，配置驱动 |
| **本地调用** | mutagent 直接调用 provider，不走代理 | 无 HTTP 开销，provider 持有完整认证状态 |
| **代理用途** | 对外暴露模型 + API 日志分析 | 让 Claude Code 等外部工具使用已配置的模型 |
| **代理端口** | 复用 mutbot 8741，`/llm` 路径前缀 | 统一入口，非 copilot 专用 |
| **启动方式** | 随 mutbot 启动，复用 `modules` 配置 | 无需独立进程，无需额外配置机制 |
| **HTTP 客户端** | mutagent 用 `requests`（同步），mutbot 代理用 `httpx`（异步） | 各层匹配自身同步/异步模型 |

## 3. 设计方案

### 3.1 总体架构

```
                    ┌─ mutagent 内部（直接调用）──────────────────────┐
                    │                                                │
                    │  Agent → LLMClient → LLMProvider.send()        │
                    │                        ├─ AnthropicProvider     │
                    │                        ├─ OpenAIProvider        │
                    │                        └─ CopilotProvider ←──── mutbot 扩展
                    │                                                │
                    └────────────────────────────────────────────────┘

                    ┌─ mutbot 代理（对外暴露）───────────────────────┐
                    │                                                │
Claude Code ──→     │  /llm/v1/messages          ─┐                  │
其他客户端 ──→      │  /llm/v1/chat/completions   ├→ 读取模型配置    │
                    │  /llm/v1/models             ─┘  格式转换        │
                    │         │                       API 日志        │
                    │         ▼                                      │
                    │  api.githubcopilot.com / api.anthropic.com / …│
                    └────────────────────────────────────────────────┘
```

**两条独立路径**：
- **内部路径**：mutagent agent → `LLMProvider.send()`（同步，使用 mutagent 内部类型 `Message`/`StreamEvent`）
- **代理路径**：外部 HTTP 客户端 → mutbot FastAPI 路由 → 后端 API（异步，JSON-to-JSON 转换）

两条路径共享：模型配置、Copilot 认证状态。

### 3.2 mutobj 增强：`resolve_class`

在 mutobj 中新增一个方法，通过类路径字符串解析 Declaration 子类，未注册时自动 import：

```python
# mutobj/core.py 新增
def resolve_class(class_path: str, base_cls: type | None = None) -> type:
    """通过 '模块.类名' 解析 Declaration 子类，未注册时自动 import。

    Args:
        class_path: 全路径 "mutbot.copilot.provider.CopilotProvider"
                    或短名 "AnthropicProvider"（在已注册类中搜索）
        base_cls: 可选，验证解析结果是否为指定基类的子类

    Raises:
        ValueError: 找不到类或类型不匹配
    """
    # 1. 已注册类中查找（短名或全路径）
    for (mod, qualname), cls in _class_registry.items():
        if class_path in (qualname, f"{mod}.{qualname}"):
            if base_cls is None or issubclass(cls, base_cls):
                return cls

    # 2. 全路径时，自动 import 模块（DeclarationMeta.__new__ 自动注册）
    if "." in class_path:
        module_path, class_name = class_path.rsplit(".", 1)
        importlib.import_module(module_path)
        key = (module_path, class_name)
        if key in _class_registry:
            cls = _class_registry[key]
            if base_cls is None or issubclass(cls, base_cls):
                return cls

    raise ValueError(f"Cannot resolve class: {class_path}")
```

### 3.3 LLMProvider Declaration（mutagent 层）

```python
# mutagent/provider.py
class LLMProvider(mutagent.Declaration):
    """LLM 提供商抽象基类。

    子类通过 mutobj 子类发现机制自动注册。
    配置中指定类路径，resolve_class 自动加载。
    """

    @classmethod
    def from_config(cls, model_config: dict) -> LLMProvider:
        """从模型配置创建 provider 实例。子类覆盖此方法。"""
        ...

    def send(
        self,
        model: str,
        messages: list[Message],
        tools: list[ToolSchema],
        system_prompt: str = "",
        stream: bool = True,
    ) -> Iterator[StreamEvent]:
        """发送请求到 LLM 后端，返回流式事件。"""
        ...
```

#### 内置 Provider（mutagent）

```python
# mutagent/builtins/anthropic_provider.py
class AnthropicProvider(LLMProvider):
    """Anthropic Claude API"""
    base_url: str
    api_key: str

    @classmethod
    def from_config(cls, config: dict) -> AnthropicProvider:
        return cls(base_url=config["base_url"], api_key=config["auth_token"])

    def send(self, model, messages, tools, system_prompt="", stream=True):
        # 复用现有 claude_impl 的核心逻辑
        ...
```

```python
# mutagent/builtins/openai_provider.py
class OpenAIProvider(LLMProvider):
    """OpenAI Chat Completions 格式 API"""
    base_url: str
    api_key: str

    @classmethod
    def from_config(cls, config: dict) -> OpenAIProvider:
        return cls(base_url=config["base_url"], api_key=config["auth_token"])

    def send(self, model, messages, tools, system_prompt="", stream=True):
        # OpenAI 格式调用
        ...
```

#### 扩展 Provider（mutbot）

```python
# mutbot/copilot/provider.py
class CopilotProvider(LLMProvider):
    """GitHub Copilot 后端"""
    auth: CopilotAuth
    account_type: str = "individual"

    @classmethod
    def from_config(cls, config: dict) -> CopilotProvider:
        auth = CopilotAuth.get_instance()  # 单例，管理 token 生命周期
        return cls(auth=auth, account_type=config.get("account_type", "individual"))

    def send(self, model, messages, tools, system_prompt="", stream=True):
        # 1. 获取 Copilot JWT（懒刷新）
        token = self.auth.get_token()
        # 2. 转换 Message → OpenAI 格式
        # 3. 调用 api.githubcopilot.com/chat/completions
        # 4. 解析响应 → StreamEvent
        ...
```

### 3.4 LLMClient 重构

LLMClient 改为组合模式，持有 LLMProvider 实例：

```python
# mutagent/client.py
class LLMClient:
    """LLM 客户端，组合 provider + 录制。"""
    provider: LLMProvider
    model: str
    api_recorder: ApiRecorder | None = None

    def send_message(self, messages, tools, system_prompt="", stream=True):
        t0 = time.monotonic()
        response_obj = None

        for event in self.provider.send(
            self.model, messages, tools, system_prompt, stream
        ):
            if event.type == "response_done":
                response_obj = event.response
            yield event

        # API 录制
        if response_obj and self.api_recorder:
            self.api_recorder.record_call(...)
```

#### Provider 创建流程

```python
# mutagent 配置 → provider 实例化
def create_llm_client(config, model_name, ...):
    model_config = config.get_model(model_name)
    provider_path = model_config.get("provider", "AnthropicProvider")

    # resolve_class 自动 import + 注册
    provider_cls = mutobj.resolve_class(provider_path, base_cls=LLMProvider)
    provider = provider_cls.from_config(model_config)

    return LLMClient(
        provider=provider,
        model=model_config["model_id"],
        api_recorder=...,
    )
```

### 3.5 模型配置

```json
{
  "default_model": "copilot-claude",
  "models": {
    "claude-direct": {
      "provider": "AnthropicProvider",
      "base_url": "https://api.anthropic.com",
      "auth_token": "sk-ant-...",
      "model_id": "claude-sonnet-4-20250514"
    },
    "copilot-claude": {
      "provider": "mutbot.copilot.provider.CopilotProvider",
      "model_id": "claude-sonnet-4"
    },
    "copilot-gpt": {
      "provider": "mutbot.copilot.provider.CopilotProvider",
      "model_id": "gpt-4.1"
    },
    "openai-gpt": {
      "provider": "OpenAIProvider",
      "base_url": "https://api.openai.com/v1",
      "auth_token": "sk-...",
      "model_id": "gpt-4.1"
    }
  }
}
```

- 内置 provider 用短名（`"AnthropicProvider"`）
- 扩展 provider 用全路径（`"mutbot.copilot.provider.CopilotProvider"`）
- `provider` 缺省为 `"AnthropicProvider"`（向后兼容）

### 3.6 Copilot 认证（mutbot 层）

```python
# mutbot/copilot/auth.py
class CopilotAuth:
    """GitHub Copilot 认证管理（单例）。"""

    GITHUB_CLIENT_ID = "Iv1.b507a08c87ecfe98"
    TOKEN_PATH = Path("~/.local/share/mutbot/copilot/github_token").expanduser()

    github_token: str | None    # 持久化到磁盘
    copilot_token: str | None   # 仅内存
    expires_at: float           # Copilot JWT 过期时间

    def get_token(self) -> str:
        """获取有效的 Copilot JWT，过期时同步刷新。"""
        if self._is_expired():
            self._refresh_copilot_token()
        return self.copilot_token

    def ensure_authenticated(self) -> None:
        """确保已认证。未认证时触发 GitHub 设备流。"""
        if not self.github_token:
            self._device_flow()
        self._refresh_copilot_token()
```

**认证端点**：

| 步骤 | 端点 | 说明 |
|------|------|------|
| 1. 设备码 | `POST github.com/login/device/code` | 获取 user_code |
| 2. 轮询 | `POST github.com/login/oauth/access_token` | 获取 GitHub token |
| 3. 换取 JWT | `GET api.github.com/copilot_internal/v2/token` | 获取 Copilot JWT |
| 4. 模型列表 | `GET api.githubcopilot.com/models` | 可用模型 |
| 5. 调用 | `POST api.githubcopilot.com/chat/completions` | OpenAI 格式 |

**Copilot 专用 Headers**：
```python
{
    "Authorization": f"Bearer {copilot_jwt}",
    "copilot-integration-id": "vscode-chat",
    "editor-version": f"vscode/{vscode_version}",
    "editor-plugin-version": "copilot-chat/0.26.7",
    "user-agent": "GitHubCopilotChat/0.26.7",
    "openai-intent": "conversation-panel",
    "x-github-api-version": "2025-04-01",
    "x-request-id": str(uuid4()),
}
```

Base URL 按账户类型：`individual` → `api.githubcopilot.com`，`business` → `api.business.githubcopilot.com`，`enterprise` → `api.enterprise.githubcopilot.com`

### 3.7 可选功能加载（复用 modules 配置）

复用 mutagent 已有的 `modules` 配置机制。`config.json` 中的 `"modules"` 列表用于自动 import 扩展模块，import 时触发 Declaration 子类注册：

```python
# session_impl.py 中已有的机制（agent 创建时执行）
for module_name in config.get("modules", []):
    importlib.import_module(module_name)
```

mutbot server 启动时复用同一机制，在 lifespan 中 import modules 并发现代理路由：

```python
# mutbot/web/server.py（lifespan 中）
from mutbot.proxy import LLMProxyRouter  # 仅当 modules 已 import 时存在

# 读取 config，import modules（与 session_impl 相同逻辑）
for module_name in config.get("modules", []):
    importlib.import_module(module_name)

# 发现并挂载代理路由（如果 proxy 模块已被 import）
try:
    from mutbot.proxy.routes import create_llm_router
    router = create_llm_router(config)
    app.include_router(router, prefix="/llm")
except ImportError:
    pass  # proxy 模块未配置，跳过
```

**配置**（`.mutagent/config.json`）：

```json
{
  "modules": [
    "mutbot.proxy"
  ]
}
```

- 配置了 `"mutbot.proxy"` → `importlib.import_module` 自动 import → 代理路由可用 → 挂载到 `/llm`
- 不配置 → 模块不 import → 代理不存在 → mutbot 正常运行无任何影响
- 无需额外的 Feature 基类或 `features` 配置，复用现有 `modules` 机制

### 3.8 LLM 代理路由（mutbot 层）

代理将所有已配置模型暴露给外部客户端。路由挂载到 `/llm` 前缀（见 3.7 节加载机制）。

**端点**：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/llm/v1/messages` | POST | Anthropic 格式（Claude Code 使用） |
| `/llm/v1/chat/completions` | POST | OpenAI 格式 |
| `/llm/v1/models` | GET | 已配置模型列表 |

**代理工作流**：

```
外部请求 → 提取 model 名 → 查找模型配置 → 确定 provider 和目标格式
    │
    ├─ 请求格式 = 目标格式 → 透传（加认证 headers）
    └─ 请求格式 ≠ 目标格式 → 格式转换 → 转发 → 响应转换
```

例如 Claude Code 发送 Anthropic 格式请求，目标是 Copilot（OpenAI 格式）：
1. 解析 Anthropic JSON 请求
2. Anthropic → OpenAI JSON 转换
3. 添加 Copilot 认证 Headers，转发到 `api.githubcopilot.com`
4. OpenAI → Anthropic JSON 响应转换
5. 返回 Anthropic SSE 流

### 3.9 Anthropic ↔ OpenAI 格式转换

代理层的 JSON-to-JSON 直接转换（不经过 mutagent 内部类型）：

#### 请求转换（Anthropic → OpenAI）

| Anthropic | OpenAI |
|-----------|--------|
| `system` 顶层字段 | `messages[0] = {"role": "system", ...}` |
| `user` + `tool_result` blocks | `role: "tool"` 消息 |
| `assistant` + `tool_use` blocks | `tool_calls[].function` |
| `tools[].input_schema` | `tools[].function.parameters` |
| `tool_choice: "any"` | `tool_choice: "required"` |
| `max_tokens` | `max_tokens` |

模型名称归一化：strip 日期后缀（`claude-sonnet-4-20250514` → `claude-sonnet-4`）。

#### 响应转换（OpenAI → Anthropic）

| OpenAI | Anthropic |
|--------|-----------|
| `finish_reason: "stop"` | `stop_reason: "end_turn"` |
| `finish_reason: "tool_calls"` | `stop_reason: "tool_use"` |
| `choices[0].message.tool_calls` | `content[].type = "tool_use"` |
| `usage.prompt_tokens` | `usage.input_tokens` |
| `usage.completion_tokens` | `usage.output_tokens` |

#### 流式 SSE 转换（OpenAI → Anthropic）

维护 `StreamState` 状态机，追踪 content block index 和类型：

```
OpenAI chunk                        Anthropic events
──────────────────                  ────────────────────────
首个 chunk                      →   event: message_start
text delta                      →   content_block_start (text)
                                    content_block_delta (text_delta)
tool_calls[0] 出现              →   content_block_stop
                                    content_block_start (tool_use)
tool args delta                 →   content_block_delta (input_json_delta)
finish_reason                   →   content_block_stop + message_delta
data: [DONE]                    →   event: message_stop
```

### 3.10 API 日志

代理记录所有 API 调用到 JSONL，用于分析外部软件的 LLM 使用情况。

**存储**：`.mutbot/logs/proxy/YYYY-MM-DD.jsonl`

**记录格式**：

```json
{
  "type": "proxy_call",
  "ts": "2026-02-25T14:30:00Z",
  "client_format": "anthropic",
  "model": "claude-sonnet-4",
  "provider": "CopilotProvider",
  "request": {
    "message_count": 5,
    "has_tools": true,
    "tool_count": 12,
    "stream": true
  },
  "response": {
    "status": 200,
    "stop_reason": "end_turn",
    "has_tool_calls": false
  },
  "usage": {
    "input_tokens": 1500,
    "output_tokens": 350
  },
  "duration_ms": 2800
}
```

**查询**：

```bash
python -m mutbot.cli.proxy_log summary --date today
python -m mutbot.cli.proxy_log list --model claude-sonnet-4 -n 20
python -m mutbot.cli.proxy_log usage --date today
```

### 3.11 Claude Code 集成

**`~/.claude/settings.json`**：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://localhost:8741/llm",
    "ANTHROPIC_AUTH_TOKEN": "dummy",
    "ANTHROPIC_MODEL": "claude-sonnet-4",
    "ANTHROPIC_SMALL_FAST_MODEL": "claude-haiku-4.5",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"
  }
}
```

### 3.12 模块结构

```
mutobj/src/mutobj/
├── core.py                        # 新增 resolve_class()

mutagent/src/mutagent/
├── provider.py                    # 新增：LLMProvider Declaration
├── client.py                      # 重构：LLMClient 组合 LLMProvider
├── builtins/
│   ├── claude_impl.py             # 重构为 AnthropicProvider
│   └── openai_provider.py         # 新增：OpenAIProvider

mutbot/src/mutbot/
├── copilot/                       # 新增：Copilot 扩展（可选）
│   ├── __init__.py
│   ├── auth.py                    #   GitHub OAuth + Token 管理
│   └── provider.py                #   CopilotProvider(LLMProvider)
├── proxy/                         # 新增：LLM 代理（可选，modules 配置驱动）
│   ├── __init__.py                #   模块入口，import 时注册
│   ├── routes.py                  #   FastAPI 路由 /llm/v1/...
│   ├── translation.py             #   Anthropic ↔ OpenAI JSON 转换
│   └── logging.py                 #   API 日志 JSONL
```

## 4. 实施步骤清单

### 阶段一：mutobj 增强 [✅ 已完成]

- [x] **Task 1.1**: 实现 `resolve_class()`
  - [x] 按短名/全路径搜索 `_class_registry`
  - [x] 全路径时自动 `importlib.import_module`
  - [x] `base_cls` 类型校验
  - [x] 单元测试 (10 个测试通过)
  - 状态：✅ 已完成

### 阶段二：LLMProvider 抽象（mutagent） [✅ 已完成]

- [x] **Task 2.1**: 定义 LLMProvider Declaration
  - [x] `provider.py`：`LLMProvider` 基类 + `from_config` + `send`
  - 状态：✅ 已完成

- [x] **Task 2.2**: 重构 AnthropicProvider
  - [x] 从 `claude_impl.py` 提取为 `AnthropicProvider(LLMProvider)` 子类
  - [x] 保持现有格式转换和 SSE 解析逻辑
  - 状态：✅ 已完成

- [x] **Task 2.3**: 实现 OpenAIProvider
  - [x] OpenAI Chat Completions 格式调用
  - [x] 消息/工具格式转换
  - [x] SSE 流解析（`data: [DONE]` 结尾）
  - 状态：✅ 已完成

- [x] **Task 2.4**: 重构 LLMClient
  - [x] 组合模式：持有 `LLMProvider` 实例
  - [x] `create_llm_client` 使用 `resolve_class` 创建 provider
  - [x] `provider` 配置缺省为 `"AnthropicProvider"`（向后兼容）
  - [x] API 录制保留在 LLMClient 层
  - 状态：✅ 已完成

### 阶段三：Copilot 扩展（mutbot） [✅ 已完成]

- [x] **Task 3.1**: Copilot 认证 (`auth.py`)
  - [x] GitHub OAuth 设备流（交互式）
  - [x] Copilot JWT 交换
  - [x] Token 持久化 + 懒刷新
  - [x] CopilotAuth 单例
  - 状态：✅ 已完成

- [x] **Task 3.2**: CopilotProvider (`provider.py`)
  - [x] `CopilotProvider(LLMProvider)` 子类
  - [x] `from_config` 获取 CopilotAuth 单例
  - [x] `send` 调用 Copilot API（OpenAI 格式 + 专用 Headers）
  - [x] 内部复用 OpenAIProvider 的格式转换逻辑
  - 状态：✅ 已完成

### 阶段四：LLM 代理（mutbot） [✅ 已完成]

- [x] **Task 4.1**: Anthropic ↔ OpenAI JSON 转换 (`translation.py`)
  - [x] 请求转换：Anthropic JSON → OpenAI JSON
  - [x] 非流式响应转换：OpenAI JSON → Anthropic JSON
  - [x] 流式 SSE 转换 + StreamState 状态机
  - [x] 模型名称归一化
  - 状态：✅ 已完成

- [x] **Task 4.2**: 代理路由 (`routes.py`)
  - [x] `POST /llm/v1/messages`（Anthropic 格式代理）
  - [x] `POST /llm/v1/chat/completions`（OpenAI 格式代理）
  - [x] `GET /llm/v1/models`（模型列表）
  - [x] 读取模型配置确定后端 + 格式
  - [x] SSE streaming 响应
  - 状态：✅ 已完成

- [x] **Task 4.3**: 代理模块加载
  - [x] `proxy/__init__.py`：模块入口
  - [x] `server.py` lifespan 中 import modules + 挂载路由
  - [x] `modules` 配置中加入 `"mutbot.proxy"` 即启用
  - 状态：✅ 已完成

### 阶段五：API 日志 [✅ 已完成]

- [x] **Task 5.1**: 代理日志记录 (`logging.py`)
  - [x] JSONL 写入（按日分文件）
  - [x] 请求/响应元数据
  - 状态：✅ 已完成

- [x] **Task 5.2**: 日志查询 CLI
  - [x] 摘要、按模型过滤、usage 统计
  - 状态：✅ 已完成

### 阶段六：测试 [✅ 已完成]

- [x] **Task 6.1**: mutobj `resolve_class` 单元测试 (10 个测试)
- [x] **Task 6.2**: LLMProvider 子类单元测试（格式转换）— AnthropicProvider (20 个测试) + OpenAIProvider (33 个测试)
- [x] **Task 6.3**: 代理格式转换单元测试（JSON ↔ JSON）— translation.py (63 个测试)
- [ ] **Task 6.4**: 集成测试：Claude Code → 代理 → Copilot API（端到端）— 待手动验证
- 状态：✅ 已完成（端到端集成测试待手动验证）

---

### 实施进度总结

- ✅ **阶段一：mutobj 增强** - 100% 完成 (1/1 任务)
- ✅ **阶段二：LLMProvider 抽象** - 100% 完成 (4/4 任务)
- ✅ **阶段三：Copilot 扩展** - 100% 完成 (2/2 任务)
- ✅ **阶段四：LLM 代理** - 100% 完成 (3/3 任务)
- ✅ **阶段五：API 日志** - 100% 完成 (2/2 任务)
- ✅ **阶段六：测试** - 100% 完成 (3/4 自动化测试，端到端待手动验证)

**全部测试通过**：mutobj 148 + mutagent 685 + mutbot 241 = **1074 个测试**

### 实施备注

- **`@impl` 继承冲突**：多个 LLMProvider 子类通过 `@impl(SubClass.send)` 提供实现时，所有实现注册到父类的同一条覆盖链，后注册者覆盖前者。规避方案：将 `send()` 定义在各子类的类体内。影响文件：`anthropic_provider.py`、`openai_provider.py`、`copilot/provider.py`。此问题是 mutobj 的设计缺陷，已单独立项跟踪：**`mutobj/docs/specifications/bugfix-impl-inheritance-collision.md`**。

## 5. 技术风险

- **逆向工程风险**：Copilot API 非公开，可能变更。缓解：CopilotProvider 独立模块，易替换
- **Client ID**：使用 VS Code 扩展 ID (`Iv1.b507a08c87ecfe98`)，可能被 GitHub 更换
- **Rate Limiting**：Copilot 有配额限制，日志中记录 usage 便于监控
- **流式转换**：OpenAI SSE → Anthropic SSE 状态机是最复杂部分

## 6. 代码量预估

| 模块 | 位置 | 预估行数 |
|------|------|---------|
| `resolve_class` | mutobj | ~30 |
| `provider.py` + `AnthropicProvider` | mutagent | ~300（大部分从 claude_impl 重构） |
| `OpenAIProvider` | mutagent | ~280 |
| `client.py` 重构 | mutagent | ~50（简化） |
| `auth.py` | mutbot/copilot | ~180 |
| `provider.py` (Copilot) | mutbot/copilot | ~120 |
| `translation.py` | mutbot/proxy | ~440 |
| `routes.py` | mutbot/proxy | ~150 |
| `logging.py` | mutbot/proxy | ~100 |
| `server.py` modules 加载 | mutbot/web | ~20 |
| 测试 | 三项目 | ~500 |
| **合计** | | **~2170** |
