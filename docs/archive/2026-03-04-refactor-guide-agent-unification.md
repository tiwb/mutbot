# Guide 与 Agent 统一 设计规范

**状态**：✅ 已完成
**日期**：2026-03-04
**类型**：重构

## 背景

当前 mutbot 有两种 AgentSession 子类：

- **GuideSession**（`guide.py`）— 手动组装 `WebToolkit + SessionToolkit + SetupToolkit`，不使用 `auto_discover`，定位是"轻量助手"
- **AgentSession**（基类）— `build_default_agent()` 使用 `ToolSet(auto_discover=True)`，组装 `ModuleToolkit + LogToolkit + 所有可发现的 Toolkit`，定位是"全能 Agent"

问题：
1. **功能割裂** — Guide 无 Module/Log 工具，Agent 无 Web/Setup/Session 工具。用户需要手动切换 Session 才能使用不同能力
2. **NullProvider 逻辑与 Session 类型耦合** — 首次配置只在 GuideSession 生效
3. **配置工具局限** — SetupToolkit 只能配置 LLM provider，没有通用的配置管理能力（如 Jina API Key）
4. **会话消息缺乏上下文** — AI 看到的消息没有发送者名称和时间信息
5. **Setup 入口错位** — `SetupWizardMenu` 放在 `SessionList/Header` 全局菜单，但它是 Session 级操作

本规范整合 TASKS.md 中的四项关联任务：
- Guide 与 Agent 合并
- 会话消息包含发送者时间和名称
- 配置工具集（通用配置工具 + Setup-llm 合并）
- Jina 免费搜索额度引导配置

## 设计方案

### 核心思路：消除 GuideSession，增强 AgentSession

不再有 "Guide" 这个独立 Session 类型。AgentSession 提供一组精选的基础工具（网络、配置、UI），不使用 `auto_discover`。

```
当前：
  GuideSession  → WebToolkit + SessionToolkit + SetupToolkit（手动组装）
  AgentSession  → ModuleToolkit + LogToolkit + ...（auto_discover 全部）

目标：
  AgentSession  → WebToolkit + ConfigToolkit + UIToolkit（手动组装基础工具集）
                   不再使用 auto_discover
```

### 变更一：删除 GuideSession，统一到 AgentSession

**删除**：
- `mutbot/builtins/guide.py` — 整个文件（GuideSession + NullProvider）
- `mutbot/builtins/menus.py` 中的 `SetupWizardMenu`（从全局菜单移除）

**NullProvider 移动**：
- 移到 `mutbot/builtins/setup_toolkit.py`（→ 后续改名 `config_toolkit.py`），与配置逻辑内聚

**修改 `build_default_agent()`**（`session_impl.py`）：
- 不再使用 `ToolSet(auto_discover=True)`，改为手动组装基础工具集：
  ```python
  tool_set = ToolSet()
  tool_set.add(WebToolkit(config=config))
  tool_set.add(ConfigToolkit())
  tool_set.add(UIToolkit())
  ```
- 增加 NullProvider 检测：无 `providers` 配置时使用 NullProvider
- 默认系统提示词不限定语言，提示 LLM 使用用户所用的语言回答：
  ```
  You are MutBot assistant.
  - Help users with their tasks using your knowledge and available tools
  - Always respond in the user's language
  ```

**修改默认 Session 类型**：
- `WelcomePage.tsx` 中 `GuideSession` 引用改为 `AgentSession`
- `session.run_setup` RPC 改为在当前活跃的 AgentSession 上操作（不再查找/创建 Guide Session）
- `__init__.py` 中移除 `guide` 的 import

**不做兼容**：删除 GuideSession 后，已保存的旧 Guide Session 记录不做迁移处理。

### 变更二：AgentPanel Header 菜单

将 Session 级操作从全局菜单移到 AgentPanel 顶部信息栏，复用 `RpcMenu` 模式。

**后端**（`menus.py`）— 新增菜单分类 `AgentPanel/Header`：

```python
class ConfigLLMMenu(Menu):
    """AgentPanel 菜单 — LLM 配置"""
    display_name = "LLM Setup"
    display_icon = "settings"
    display_category = "AgentPanel/Header"
    display_order = "0config:0"

    async def execute(self, context: dict) -> MenuResult:
        session_id = context["session_id"]
        # 在当前 session 的 bridge 上触发 Config-llm 工具
        ...
```

菜单项通过 `context.session_id` 知道操作哪个 Session，后端通过 `bridge.request_tool()` 触发配置流程。

**前端**（`AgentPanel.tsx`）— header 区域增加 `RpcMenu`：

```tsx
<div className="agent-header">
  <span className={`status-dot ${connected ? "connected" : ""}`} />
  <span>Session {sessionId.slice(0, 8)}</span>
  {/* ... existing elements ... */}
  <RpcMenu
    rpc={rpc}
    category="AgentPanel/Header"
    context={{ session_id: sessionId }}
    trigger={<button className="agent-menu-btn" title="Menu">⋮</button>}
    onResult={handleMenuResult}
  />
</div>
```

用竖三点 `⋮` 按钮触发，风格与 SessionList 的汉堡菜单保持一致。

**可扩展性**：此机制为 AgentSession 提供了通用的菜单扩展点。后续可以通过定义新的 `Menu` 子类（`display_category = "AgentPanel/Header"`）轻松添加更多 Session 级菜单项。

### 变更三：会话消息包含发送者和时间

让 LLM 看到的消息格式与用户在前端看到的保持一致，包括发送者名称和完整日期时间。

**消息元信息注入**：在 `AgentContext.prepare_messages()` 中，为每条消息添加元信息前缀行：

- **用户消息**：`[User · 2026-03-04 15:30]`（`User` 后续可替换为用户自定义名称）
- **助理消息**：`[claude-sonnet-4 · 2026-03-04 15:30]`（使用 `msg.model` 字段）
- **系统消息**：不加前缀（role 已区分）

格式示例（LLM 视角）：

```
[User · 2026-03-04 15:30]
帮我搜索一下最近的 Python 3.13 新特性

[claude-sonnet-4 · 2026-03-04 15:31]
我来帮你搜索...
```

**实现要点**：

1. **数据来源** — Message 已有 `sender` 和 `timestamp` 字段。`sender` 在 `AgentBridge` 接收用户消息时赋值（用户名），assistant 消息的 `model` 在 `agent_impl.py:79` 已赋值
2. **注入位置** — `AgentContext.prepare_messages()` 中生成新的 Message 副本（不修改原始消息），在首个 TextBlock 前插入元信息行
3. **时间格式** — `YYYY-MM-DD HH:MM`，使用消息自身的 `timestamp`。LLM 从最后一条消息的时间自然得知当前时间
4. **可配置** — 通过 `Config` 配置项 `message_metadata`（默认 `true`）控制是否注入。Config 级别可全局开关，Session 级别覆盖留作后续

**不做**：
- 不修改 Message 数据结构
- 不在系统提示词中注入当前时间（LLM 从消息时间戳推断）
- 不修改前端显示（前端已有独立的 sender/timestamp 渲染）

### 变更四：通用配置工具（ConfigToolkit）

将 `SetupToolkit` 重命名并扩展为 `ConfigToolkit`，提供 LLM 配置和通用配置修改能力。

```python
class ConfigToolkit(UIToolkitBase):
    """配置管理工具 — LLM 配置向导 + 通用配置修改。"""

    _tool_methods = ["llm", "update"]
```

**工具方法**：

| 工具名 | 说明 |
|--------|------|
| `Config-llm` | 原 `Setup-llm`，LLM provider 配置向导（保持现有全部功能） |
| `Config-update` | 通用配置修改：AI 提供配置键和建议默认值，用户手动确认后写入 |

**不提供 Config-show** — Agent 不能读取配置值（安全考虑）。各工具在报错时会告知 Agent 需要配置什么以及如何配置，Agent 不需要主动查询配置状态。

**Config-update 设计**：

```python
async def update(self, key: str, default_value: str = "", description: str = "") -> str:
    """修改配置项。AI 提供配置键、建议默认值和说明，用户确认后写入。

    Args:
        key: 配置路径（如 "WebToolkit.jina_api_key"）
        default_value: 建议的默认值，用户可修改
        description: 配置项说明，帮助用户理解
    """
```

交互流程：
1. AI 调用 `Config-update`，提供 key、default_value、description
2. UI 展示：配置项说明 + 输入框（预填 default_value）+ 确认/取消按钮
3. 用户修改值并确认 → 写入 `~/.mutbot/config.json`
4. 用户取消 → 返回"用户取消配置"

**关键原则**：AI 只提供建议值，最终值由用户手动输入和确认。不做自动配置。

**NullProvider 触发调整**：NullProvider 返回的 tool_use 改为 `Config-llm`（原 `Setup-llm`）。

### 变更五：Jina 配额引导

**方案：工具自身在出错时返回配置指引，不污染系统提示词**

**修改位置**：`mutagent/builtins/web_jina.py` — `JinaSearchImpl.search()` 和 `JinaFetchImpl.fetch()`

当 Jina 返回 401/429 或响应中包含配额提示时，在工具返回结果中附加完整的配置指引：

```
[错误] Jina 免费额度已用完。

请引导用户配置 Jina API Key：
1. 获取 Key：https://jina.ai/api-key（请提供可点击链接给用户）
2. 使用 Config-update 工具写入配置：
   - key: "WebToolkit.jina_api_key"
   - description: "Jina API Key，用于 Web 搜索和网页读取"
```

**关键原则**：
- 配置方法由 Jina 工具自身提供，不写在系统提示词中
- 仅在出错时注入提示，正常使用时不增加额外 token 开销
- 提示中包含完整的操作步骤，AI 无需预置知识即可引导用户

## 实施步骤清单

### 阶段一：后端核心重构 [✅ 已完成]

- [x] **Task 1.1**: SetupToolkit → ConfigToolkit 重命名与扩展
  - [x] 将 `setup_toolkit.py` 重命名为 `config_toolkit.py`
  - [x] 类名 `SetupToolkit` → `ConfigToolkit`
  - [x] 将 `NullProvider` 从 `guide.py` 移入 `config_toolkit.py`
  - [x] NullProvider 的 tool_use 名从 `Setup-llm` 改为 `Config-llm`
  - [x] 添加 `_tool_methods = ["llm", "update"]`
  - [x] 实现 `Config-update` 工具方法（key + default_value + description → UI 表单 → 写入配置）
  - [x] 更新 `__init__.py` 中的 import
  - 状态：✅ 已完成

- [x] **Task 1.2**: 重写 `build_default_agent()`
  - [x] 移除 `ToolSet(auto_discover=True)`，改为 `ToolSet()`
  - [x] 手动组装工具集：`WebToolkit` + `ConfigToolkit` + `UIToolkit`
  - [x] 增加 NullProvider 检测（无 `providers` 时使用 NullProvider）
  - [x] 更新默认系统提示词
  - [x] 确保 `import mutagent.builtins.web_local` 注册 LocalFetchImpl
  - 状态：✅ 已完成

- [x] **Task 1.3**: 删除 GuideSession
  - [x] 删除 `mutbot/builtins/guide.py` 文件
  - [x] `mutbot/builtins/__init__.py` 中移除 `import guide`
  - [x] `routes.py` 中所有 `GuideSession` 引用改为 `AgentSession`
  - 状态：✅ 已完成

- [x] **Task 1.4**: 重构 `session.run_setup` RPC
  - [x] 改为接受 `session_id` 参数（在指定 session 上触发 `Config-llm`）
  - [x] 不再查找/创建 GuideSession
  - [x] 通过 `bridge.request_tool("Config-llm")` 触发
  - 状态：✅ 已完成

### 阶段二：AgentPanel Header 菜单 [✅ 已完成]

- [x] **Task 2.1**: 后端 — 新增 `AgentPanel/Header` 菜单
  - [x] `menus.py` 中删除 `SetupWizardMenu`
  - [x] 新增 `ConfigLLMMenu`（`display_category = "AgentPanel/Header"`，`client_action = "run_setup"`）
  - 状态：✅ 已完成

- [x] **Task 2.2**: 前端 — AgentPanel header 添加 RpcMenu
  - [x] `AgentPanel.tsx` header 区域添加 `<RpcMenu category="AgentPanel/Header">`
  - [x] 竖三点 `⋮` 触发按钮，添加对应 CSS 样式
  - [x] `onClientAction` 处理 `run_setup`，调用 `session.run_setup` RPC 带 `session_id`
  - 状态：✅ 已完成

- [x] **Task 2.3**: 前端 — 清理旧 Setup 入口
  - [x] `App.tsx` 中 `handleHeaderAction` 移除 `run_setup_wizard` 分支
  - [x] `AgentPanel.tsx` 中 `onSetupLLM` 改为走 `session.run_setup` RPC 带 `session_id`
  - 状态：✅ 已完成

### 阶段三：消息元信息注入 [✅ 已完成]

- [x] **Task 3.1**: mutagent — `prepare_messages()` 注入发送者和时间
  - [x] `context_impl.py` 中实现 `_inject_metadata()` 和 `_format_timestamp()`
  - [x] 为 user 消息注入 `[{sender} · {YYYY-MM-DD HH:MM}]` 前缀行
  - [x] 为 assistant 消息注入 `[{model} · {YYYY-MM-DD HH:MM}]` 前缀行
  - [x] 系统消息不处理
  - [x] 生成 Message 副本，不修改原始消息
  - [x] 无 sender 时 user 默认 `"User"`，无 model 时 assistant 默认 `"Assistant"`
  - 状态：✅ 已完成

- [x] **Task 3.2**: 可配置开关
  - [x] `AgentContext` 增加 `message_metadata: bool = True` 属性
  - [x] `prepare_messages()` 根据此属性决定是否注入
  - [x] `build_default_agent()` 中从 Config 读取 `message_metadata` 配置项传入 AgentContext
  - 状态：✅ 已完成

- [x] **Task 3.3**: mutbot — 用户消息设置 sender 和 timestamp
  - [x] `AgentBridge.send_message()` 已有 `sender="User"` 和 `timestamp=time.time()`，无需修改
  - 状态：✅ 已完成（已有实现满足要求）

### 阶段四：Jina 配额引导 [✅ 已完成]

- [x] **Task 4.1**: 更新 Jina 错误提示
  - [x] `web_jina.py` 中 `_jina_search` 的 401/429 返回信息改为包含 `Config-update` 工具引导
  - [x] `web_jina.py` 中 `_jina_fetch` 的 401/429 返回信息同步修改
  - [x] 提示内容包含：可点击链接、Config-update 工具调用方式
  - 状态：✅ 已完成

### 阶段五：清理与测试 [✅ 已完成]

- [x] **Task 5.1**: 更新前端默认 Session 类型
  - [x] `WelcomePage.tsx` 中 `GuideSession` 改为 `mutbot.session.AgentSession`
  - 状态：✅ 已完成

- [x] **Task 5.2**: 更新测试
  - [x] 更新 `test_setup_provider.py`、`test_config_system.py`、`test_setup_integration.py` 中的引用
  - [x] 358 个测试全部通过（1 个已有失败 `test_session_persistence` 与本次无关）
  - 状态：✅ 已完成

- [x] **Task 5.3**: 构建验证
  - [x] 后端 `pip install -e ".[dev]"` 无报错
  - [x] 前端 `npm run build` 无报错
  - 状态：✅ 已完成

## 测试验证

- 后端：358/359 passed（1 个已有失败与本次改动无关）
- 前端：build 成功
- import 验证：`ConfigToolkit`、`NullProvider`、`UIToolkit`、`AgentSession` 均可正常导入

## 关键参考

### 源码
- `mutbot/src/mutbot/builtins/guide.py` — GuideSession + NullProvider（将被删除）
- `mutbot/src/mutbot/session.py` — AgentSession 基类
- `mutbot/src/mutbot/runtime/session_impl.py:296` — `build_default_agent()`（将重写工具组装逻辑）
- `mutbot/src/mutbot/builtins/setup_toolkit.py` — SetupToolkit（将重命名为 ConfigToolkit）
- `mutbot/src/mutbot/builtins/menus.py` — Menu 系统（将移除 SetupWizardMenu，新增 AgentPanel/Header 菜单）
- `mutagent/src/mutagent/messages.py` — Message 数据结构（已有 sender/timestamp/model 字段）
- `mutagent/src/mutagent/context.py` — AgentContext（prepare_messages 将注入元信息）
- `mutagent/src/mutagent/builtins/context_impl.py` — prepare_messages 默认实现
- `mutagent/src/mutagent/toolkits/web_toolkit.py` — WebToolkit 声明
- `mutagent/src/mutagent/builtins/web_jina.py` — Jina 集成（401/429 处理）
- `mutbot/src/mutbot/web/agent_bridge.py` — AgentBridge（消息输入流、request_tool）
- `mutbot/frontend/src/panels/AgentPanel.tsx` — AgentPanel header（将增加 RpcMenu）
- `mutbot/frontend/src/panels/SessionListPanel.tsx` — RpcMenu 使用模式参考
- `mutbot/frontend/src/components/WelcomePage.tsx` — 默认 Session 类型引用
- `mutbot/frontend/src/App.tsx:419` — handleHeaderAction（run_setup_wizard 处理）

### 相关规范
- `docs/specifications/refactor-setup-wizard.md` — Setup Wizard 重构（✅ 已完成）
- `docs/specifications/feature-interactive-ui-tools.md` — UIToolkit 设计
- `docs/specifications/feature-session-list-management.md` — Session 管理
