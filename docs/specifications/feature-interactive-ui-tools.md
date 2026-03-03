# 后端驱动 UI 框架

**状态**：🔄 实施中
**日期**：2026-03-02
**类型**：功能设计

> **本规范涵盖 [feature-evolvable-blocks.md](feature-evolvable-blocks.md) 的全部范围，原文档弃用。**

## 背景

mutbot 当前的用户交互局限于纯文本对话。一些场景体验不佳：

- **首次 LLM 配置**：对话式状态机（SetupProvider），多轮文本来回，用户输入自由文本
- **复杂参数选择**：需要结构化输入，文本对话效率低
- **应用级设置**：没有专门的设置界面，全靠对话

NiceGUI 证明了一个思路：**后端 async 代码控制 UI，前端只做渲染**。Python 端声明 UI 组件，通过 WebSocket 推送到前端，用户事件回传后端处理。

本规范设计 mutbot 层面的**后端驱动 UI 基础设施**——一套通用框架，支撑工具交互、Markdown 内联、应用级界面等多种场景。

### 设计目标

1. **通用基础设施**：mutbot 全局可用——工具、Session、Markdown、设置界面均可使用
2. **后端 async 驱动**：Python async handler 控制 UI 生命周期，前端仅渲染
3. **持续双向交互**：支持多轮实时通信（如 OAuth 轮询、渐进式表单）
4. **统一组件系统**：工具、Markdown 扩展块、应用 UI 共享一套组件库和渲染引擎
5. **首个用例**：改造 LLM 首次配置流程

### 与 mutagent 的边界

mutagent 定位为稳固的 Agent 基础层，不面向终端用户。UI 框架完全在 mutbot 层，充分利用 Web 能力，不受 TUI 限制。现有 mutagent 的 ask/confirm 块机制（`block_handlers.py`、`pending_interaction`）后续统一为本框架的应用层接口。

## 架构总览

```
┌────────────────────────────────────────────────────────────┐
│                     集成层 (Integration)                     │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ 工具 (Tool)   │  │ Markdown     │  │ 应用级 UI        │  │
│  │ 任何工具均可   │  │ 内联 UI      │  │ (Session/设置/   │  │
│  │ 使用交互式 UI │  │              │  │  菜单/向导)      │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘  │
├─────────┼─────────────────┼───────────────────┼────────────┤
│                    UIContext (核心 API)                      │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ set_view()  推送视图                                  │   │
│  │ wait_event() 等待事件                                 │   │
│  │ show()      便捷方法                                  │   │
│  │ close()     关闭                                      │   │
│  └──────────────────────┬──────────────────────────────┘   │
├─────────────────────────┼──────────────────────────────────┤
│                  组件系统 (Components)                       │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ 声明式 JSON Schema → React 通用渲染器                  │   │
│  │ text | select | toggle | hint | badge | spinner ... │   │
│  └─────────────────────────────────────────────────────┘   │
├────────────────────────────────────────────────────────────┤
│                  传输层 (WebSocket)                          │
│  ui_view / ui_event / ui_close                             │
└────────────────────────────────────────────────────────────┘
```

### 核心概念

| 概念 | 说明 |
|------|------|
| **UIContext** | 后端 handler 与前端 UI 面的通信通道（核心 API） |
| **View Schema** | 声明式 JSON，描述 UI 的完整状态 |
| **组件** | View 的构成单元，可交互或只读 |
| **渲染面** | 前端提供的 UI 渲染位置（ToolCallCard、Session 面板等） |

**关键设计决策：前端提供渲染位置，后端连接并推送视图。** 不是后端指定一个字符串 target，而是前端已有的 UI 结构（ToolCallCard、Session 面板、Dialog 容器）各自作为渲染面，后端通过 UIContext 连接到对应的渲染面。

## 组件系统

### View Schema

所有 UI 由声明式 JSON 描述。一个 **View** 包含组件列表和操作按钮：

```json
{
  "title": "配置 LLM 提供者",
  "components": [
    {"type": "select", "id": "provider", "label": "选择提供者", "options": [...]},
    {"type": "text", "id": "api_key", "label": "API Key", "secret": true,
     "visible_when": {"provider": ["anthropic", "openai", "custom"]}}
  ],
  "actions": [
    {"type": "submit", "label": "确认", "primary": true},
    {"type": "cancel", "label": "取消"}
  ]
}
```

每个组件：
- `type` — 组件类型标识
- `id` — 视图内唯一标识（用于取值、事件源、React reconciliation key）
- 类型特定属性（`label`、`options`、`placeholder`、`value` 等）
- `visible_when` — 条件可见性，前端本地处理（可选）

### 初始组件集

| 组件 | type | 说明 |
|------|------|------|
| 文本输入 | `text` | 单行/多行，支持 `secret` 遮罩 |
| 选择器 | `select` | 单选下拉/卡片选择 |
| 按钮组 | `button_group` | 多按钮单选 |
| 按钮 | `button` | 独立按钮，可绑定 action 事件 |
| 开关 | `toggle` | 布尔开关 |
| 提示文本 | `hint` | 只读说明文字（支持 Markdown） |
| 状态徽章 | `badge` | 状态标签（success/info/warning/error） |
| 加载指示 | `spinner` | 加载中动画 + 文字 |
| 可复制文本 | `copyable` | 文本 + 复制按钮 |
| 链接 | `link` | 可点击链接 |

后续按需扩展（表格、滑块、文件选择、代码编辑器等）。

### 三种交互模式

同一组件在不同上下文中行为不同：

| 模式 | 事件路由 | 状态管理 | 场景 |
|------|---------|---------|------|
| **Connected** | 事件 → 后端 handler | 后端驱动 | 工具 UI、应用级 UI |
| **Local** | 无路由，前端本地 | 前端自管理 | Markdown 内联 UI |
| **Read-only** | 无交互 | 显示快照 | 历史消息、已完成交互 |

前端组件渲染相同，区别仅在事件处理。

## 前端渲染引擎

### 利用 React Reconciliation 实现无抖动更新

后端 `set_view()` 发送完整 View JSON。前端**不是**丢弃旧 DOM 重建——而是利用 React 的 reconciliation（diffing）实现平滑更新：

```
set_view(View JSON)
  → ViewRenderer 组件接收新 props
  → React 按 key={component.id} 做 reconciliation
  → 同 id 组件：更新 props，保留 DOM 节点和本地状态
  → 新 id 组件：挂载
  → 消失的 id：卸载
  → 结果：无抖动，输入框保持焦点和已输入内容
```

**核心实现思路**：

```tsx
// 通用视图渲染器
function ViewRenderer({ view, mode, onEvent }: ViewRendererProps) {
  return (
    <div className="ui-view">
      {view.title && <div className="ui-view-title">{view.title}</div>}
      <div className="ui-view-components">
        {view.components
          .filter(comp => evaluateVisibility(comp, formValues))
          .map(comp => (
            <UIComponent
              key={comp.id}       // ← React reconciliation key
              schema={comp}
              mode={mode}
              value={formValues[comp.id]}
              onChange={handleChange}
            />
          ))}
      </div>
      {view.actions && (
        <ActionBar actions={view.actions} onAction={handleAction} />
      )}
    </div>
  );
}
```

**关键点**：
- `key={comp.id}` 确保 React 按组件 ID 做 diff，而非按列表位置
- 组件内部用 `useState` 管理交互状态（输入值、展开/折叠等）
- `set_view` 更新 props 但不重新挂载组件 → 输入焦点和中间状态保留
- 后端显式传 `value` 时覆盖本地状态，不传则保留用户输入

### 组件状态保持策略

`set_view()` 全量替换时的状态保持规则：

1. **同 id 同 type** → React 复用组件实例，保留本地状态
2. **后端传了 `value`** → 覆盖本地状态（后端强制设值）
3. **后端未传 `value`** → 保留用户已输入的值（不打断用户）
4. **id 消失** → 组件卸载，状态丢弃
5. **新 id 出现** → 新组件挂载，初始值取 `value` 或 `defaultValue`

这样 `set_view` 可以更新部分组件（如状态徽章从 "等待" 变 "成功"），不影响用户正在填写的输入框。

### 表单状态管理

```tsx
function ViewRenderer({ view, mode, onEvent }) {
  // 表单值集中管理
  const [formValues, setFormValues] = useState<Record<string, unknown>>({});

  // view 变更时：合并新默认值，但不覆盖已有用户输入
  useEffect(() => {
    setFormValues(prev => {
      const next = { ...prev };
      for (const comp of view.components) {
        if (comp.value !== undefined) {
          next[comp.id] = comp.value;          // 后端显式设值
        } else if (!(comp.id in next)) {
          next[comp.id] = comp.defaultValue;    // 首次出现，用默认值
        }
        // 已有用户输入 + 后端未传 value → 保留
      }
      return next;
    });
  }, [view]);

  function handleSubmit() {
    onEvent({ type: "submit", data: formValues });
  }

  function handleAction(actionId: string) {
    onEvent({ type: "action", data: { action: actionId, ...formValues } });
  }
  // ...
}
```

### 组件实现方案

当前 mutbot 前端是纯手写 React 组件 + 全局 CSS Variables（VS Code 风格）。UI 框架的组件遵循同一风格：

- **自行实现**：text、select、toggle、button_group、hint、badge 等——交互逻辑简单，手写保证与现有风格一致
- **按需引入**：如将来需要复杂交互（日期选择、拖拽排序、富文本），引入 Radix UI 等 headless 组件库做基础
- **样式**：统一使用 CSS Variables（`--bg`、`--accent`、`--border` 等），与 VS Code 主题对齐

```css
/* 示例：UI 组件样式 */
.ui-select {
  background: var(--bg-input);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 4px 8px;
}
.ui-select:focus {
  border-color: var(--accent);
  outline: none;
}
.ui-badge.success { color: var(--success); }
.ui-badge.error   { color: var(--error); }
```

## 后端编程模型

### UIContext API

UIContext 是 handler 与前端通信的核心接口。采用 Declaration 模式——接口声明与实现分离：

```python
class UIContext(Declaration):
    """后端 handler 与前端 UI 渲染面的通信通道。
    接口声明在此，实现细节（WebSocket 推送、Future 管理等）在 @impl 中。
    """

    def set_view(self, view: dict) -> None:
        """推送完整视图到前端。前端通过 React reconciliation 平滑更新。"""
        ...

    async def wait_event(
        self,
        *,
        type: str | None = None,
        source: str | None = None,
    ) -> UIEvent:
        """等待用户事件。可按类型、来源过滤。
        外部取消时抛 CancelledError。"""
        ...

    async def show(self, view: dict) -> dict:
        """便捷方法：set_view + wait_event(type="submit")。返回表单数据。"""
        ...

    def close(self, final_view: dict | None = None) -> None:
        """关闭 UI。可指定最终视图（变为 Read-only 快照）。"""
        ...
```

不同渲染面对应不同的 `@impl`（ToolUIContext 实现、SessionUIContext 实现等），handler 代码只面向 UIContext 声明接口。

UIContext 不处理超时。超时是工具层面的关注点（普通工具和交互式工具都可能需要超时），不应耦合到 UI 通道上。

### 事件模型

前端 → 后端事件：

| 事件类型 | 触发 | data |
|---------|------|------|
| `submit` | 点击提交按钮 | `{所有组件当前值}` |
| `cancel` | 点击取消按钮 | `{}` |
| `action` | 点击自定义按钮 | `{"action": "btn_id", ...组件值}` |
| `change` | 组件值变更（需 `notify_change: true`） | `{"source": "组件id", "value": ...}` |

### 编程模式一：顺序流程（向导/工具）

适合步骤明确、线性的交互（如配置向导）：

```python
async def config_flow(ctx: UIContext):
    # Step 1
    r1 = await ctx.show(provider_selection_view)
    # Step 2（根据 Step 1 结果动态生成）
    r2 = await ctx.show(build_credentials_view(r1["provider"]))
    # Step 3
    r3 = await ctx.show(model_selection_view(r2))
    return {**r1, **r2, **r3}
```

### 编程模式二：事件循环（持久 UI）

适合长期存在、响应多种操作的 UI（如设置面板）：

```python
async def settings_handler(ctx: UIContext):
    config = load_config()
    ctx.set_view(build_settings_view(config))

    while True:
        event = await ctx.wait_event()
        match (event.type, event.data.get("action")):
            case ("action", "save"):
                save_config(event.data)
                ctx.set_view(saved_success_view())
                await asyncio.sleep(1)
                ctx.set_view(build_settings_view(load_config()))
            case ("action", "test_connection"):
                ctx.set_view(testing_view())
                ok = await test_connection(event.data)
                ctx.set_view(test_result_view(ok))
            case ("cancel", _):
                ctx.close()
                return
```

### 编程模式三：按钮各有 handler（类方法路由）

适合复杂 UI，每个按钮的处理逻辑独立：

```python
class SettingsSession(AppSession):
    """应用设置"""
    display_name = "设置"
    display_icon = "settings"

@impl(SettingsSession.handle)
async def settings_handle(self: SettingsSession, ctx: UIContext):
    ctx.set_view(self._build_view())
    while True:
        event = await ctx.wait_event()
        handler = getattr(self, f"_on_{event.data.get('action', event.type)}", None)
        if handler:
            result = await handler(ctx, event)
            if result == "close":
                return

async def _on_save(self, ctx: UIContext, event: UIEvent):
    save_config(event.data)

async def _on_reset(self, ctx: UIContext, event: UIEvent):
    ctx.set_view(self._build_view(load_defaults()))

async def _on_cancel(self, ctx: UIContext, event: UIEvent):
    ctx.close()
    return "close"
```

这三种模式都基于同一套 UIContext API，只是组织代码的方式不同。

## 集成：工具（Tool）

**所有工具都可以使用交互式 UI**。mutbot 提供 `UIToolkit` 作为 Toolkit 子类，提供便捷的 UI 访问接口。

### 绑定链

对象之间通过公开接口形成完整的绑定链：

```
Toolkit.owner ──→ ToolSet.agent ──→ Agent ──→ Session
     │                  │               │         │
     │                  │               │         └─ 会话元数据、配置
     │                  │               └─ LLM、Context、消息
     │                  └─ 工具注册表、dispatch
     └─ 工具方法实现
```

- `Toolkit.owner`：Toolkit 发现时由 ToolSet 设置（公开接口）
- `ToolSet.agent`：已有
- `Agent.session`：mutbot 中 Agent 与 Session 绑定

### UIToolkit

mutbot 层提供 `UIToolkit(Toolkit)` 子类，明确"可交互工具"的概念。**UIContext 在 UIToolkit 中按需创建**，而非由 dispatch 注入——非 UI 工具零开销，dispatch 不需要了解 UI：

```python
class UIToolkit(Toolkit):
    """带 UI 能力的 Toolkit 基类（mutbot 专用）。
    通过绑定链访问上下文，按需创建 UIContext。
    """

    @property
    def ui(self) -> UIContext:
        """当前工具执行的 UIContext。首次访问时按需创建。
        UIContext 绑定到当前 tool_call 对应的 ToolCallCard。
        """
        owner = self.owner  # ToolSet
        if not getattr(owner, '_active_ui', None):
            tool_call = owner._current_tool_call
            owner._active_ui = UIContext(
                context_id=tool_call.id,
                broadcast=...,
            )
        return owner._active_ui

    @property
    def session(self) -> Session:
        """当前 Session。"""
        return self.owner.agent.session

    async def show(self, view: dict) -> dict:
        """便捷方法：直接展示 UI 并等待提交。"""
        return await self.ui.show(view)
```

**关键：UIContext lazy 创建**：
- 非 UI 工具：不访问 `self.ui` → 不创建 UIContext → 零开销
- UI 工具：首次 `self.ui` 访问 → 创建并缓存 UIContext → 后续调用复用
- 普通 Toolkit 也可以通过绑定链手动访问（`self.owner._active_ui`），UIToolkit 只是便捷封装

### ToolSet dispatch 变更

dispatch 只需跟踪当前 tool_call，不需要了解 UI：

```python
# ToolSet dispatch（最小变更）
async def dispatch(self, tool_call):
    self._current_tool_call = tool_call
    try:
        await _original_dispatch(self, tool_call)
    finally:
        # 通用清理：如果工具执行期间创建了 UIContext，关闭它
        active_ui = getattr(self, '_active_ui', None)
        if active_ui:
            active_ui.close()
            self._active_ui = None
        self._current_tool_call = None
```

dispatch 不导入 UIContext，不创建 UIContext，只做 finally 清理。UI 的创建完全在 UIToolkit 内部。

### 工具代码示例

```python
class SetupToolkit(UIToolkit):
    async def configure_llm(self):
        result = await self.show({
            "title": "选择 LLM 提供者",
            "components": [
                {"type": "select", "id": "provider", "label": "提供者",
                 "options": [
                     {"value": "anthropic", "label": "Anthropic (Claude)"},
                     {"value": "openai", "label": "OpenAI (GPT)"},
                     {"value": "copilot", "label": "GitHub Copilot"},
                     {"value": "custom", "label": "自定义 (OpenAI 兼容)"}
                 ]}
            ],
            "actions": [{"type": "submit", "label": "下一步", "primary": True}]
        })
        return result
```

### 前端渲染位置

工具的 UIContext 自然绑定到对应的 **ToolCallCard**。当 UIContext 推送 view 时：

```
tool_exec_start
  → 前端创建 ToolCallCard（现有逻辑）
  → ToolCallCard 持有 tool_call_id

ui_view {tool_call_id: "xxx", view: {...}}
  → 前端找到对应 ToolCallCard
  → ToolCallCard 内渲染 ViewRenderer
  → 状态指示从 "●" 变为交互 UI

tool_exec_end
  → UI 关闭，ToolCallCard 显示最终结果（现有逻辑）
```

ToolCallCard 扩展：

```tsx
function ToolCallCard({ data }: Props) {
  const isRunning = data.result === undefined;
  const [uiView, setUiView] = useState<ViewSchema | null>(null);

  // 订阅 UI view 更新（通过 tool_call_id 匹配）
  useEffect(() => {
    return subscribeUIView(data.toolCallId, setUiView);
  }, [data.toolCallId]);

  return (
    <div className={`tool-card ${...}`}>
      <div className="tool-card-header" onClick={...}>
        {/* 现有 header */}
      </div>
      {uiView ? (
        // 有 UI view → 渲染交互界面
        <ViewRenderer
          view={uiView}
          mode="connected"
          onEvent={(e) => sendUIEvent(data.toolCallId, e)}
        />
      ) : expanded ? (
        // 无 UI view → 现有的 args/result 展示
        <div className="tool-card-body">{/* ... */}</div>
      ) : null}
    </div>
  );
}
```

### 示例：持续交互（Copilot OAuth）

```python
async def configure_copilot(self):
    # 1. 加载中
    self.ui.set_view({"components": [
        {"type": "spinner", "text": "正在获取设备码..."}
    ]})

    device = await self._request_device_code()

    # 2. 显示验证码和链接（前端立即更新）
    self.ui.set_view({"components": [
        {"type": "hint", "text": "请在浏览器中打开链接并输入验证码："},
        {"type": "copyable", "text": device["user_code"]},
        {"type": "link", "url": device["verification_uri"],
         "label": "打开 GitHub 验证页面"},
        {"type": "badge", "id": "status", "text": "等待验证...",
         "variant": "info"}
    ]})

    # 3. 后台轮询（前端持续显示步骤 2 的 UI）
    token = None
    for _ in range(60):
        await asyncio.sleep(device.get("interval", 5))
        token = await self._poll_github_token(device["device_code"])
        if token:
            break

    if not token:
        raise TimeoutError("GitHub 认证超时")

    # 4. 认证成功，切换到模型选择
    models = await self._fetch_copilot_models(token)
    result = await self.show({
        "components": [
            {"type": "badge", "text": "认证成功", "variant": "success"},
            {"type": "select", "id": "model", "label": "选择模型",
             "options": [{"value": m, "label": m} for m in models]}
        ],
        "actions": [
            {"type": "submit", "label": "完成配置", "primary": True}
        ]
    })

    return {"token": token, "model": result["model"]}
```

## 集成：应用 Session（AppSession）

AppSession 是 mutbot **Session 的子类**，通过 Session 面板体系渲染，但主界面是后端驱动的 UI 而非聊天对话。

### Session 体系扩展

```
Session (Declaration, abstract)
├── AgentSession (abstract)
│   └─ 具体 Agent Session 子类（聊天对话）
├── AppSession (abstract, 新增)
│   ├─ SetupSession    — 首次配置向导
│   ├─ SettingsSession — 应用设置
│   └─ ...             — 其他 UI 驱动的 Session
├── TerminalSession
└── DocumentSession
```

**Session 的 abstract 标记**：Session 上增加 `_abstract` 标记。标记为 abstract 的类（如 AgentSession、AppSession）不能被前端直接创建，仅其具体子类可以。前端调用 `session.types` 时过滤掉 abstract 类。

### AppSession 基类

```python
class AppSession(Session):
    """UI 驱动的 Session 基类。
    子类继承并实现 handle()，通过 UIContext 控制界面。
    """
    _abstract = True  # 不可被前端直接创建

    async def handle(self, ctx: UIContext) -> None:
        """子类实现此方法。ctx 连接到 Session 面板的渲染面。"""
        ...
```

### 具体 Session 子类

```python
# mutbot/builtins/setup_session.py
class SetupSession(AppSession):
    """首次 LLM 配置向导"""
    display_name = "配置向导"
    display_icon = "settings"

@impl(SetupSession.handle)
async def setup_handle(self: SetupSession, ctx: UIContext):
    # Step 1: 选择提供者
    r1 = await ctx.show(provider_selection_view)
    # Step 2: 填写凭据
    r2 = await ctx.show(build_credentials_view(r1["provider"]))
    # Step 3: 选择模型
    models = await fetch_models(r2)
    r3 = await ctx.show(model_selection_view(models))
    # 保存配置
    save_config({**r1, **r2, **r3})
    ctx.close({"components": [
        {"type": "badge", "text": "配置完成", "variant": "success"}
    ]})
```

### 发现与创建

- 所有 Session 子类通过现有 `discover_subclasses(Session)` 统一发现
- 前端 `session.types` 过滤 `_abstract=True` 的类，只展示可创建的具体子类
- AppSession 子类和 AgentSession 子类在 Session 列表中并列
- 用户点击 AppSession 子类时，Session 面板渲染 ViewRenderer（而非消息列表）

### 用例

- **首次配置向导**：检测到无 LLM 配置 → 自动创建 SetupSession
- **设置界面**：从菜单打开 → 创建 SettingsSession
- **交互式教程**：引导用户了解功能
- **仪表板**：数据可视化、系统状态

## 集成：Markdown 内联 UI

> 与之前的 `mutagent:*` 块系统无关，全新设计。

### 概念

Markdown 中嵌入 UI 组件，使用与后端驱动 UI 相同的组件系统，但运行在 **Local 模式**（无后端）。

LLM 在 Markdown 中输出 UI 组件 → 前端渲染可交互的组件（select 可选、toggle 可切换）→ 用户操作后状态存在前端本地 → 结果在用户发送消息时传递给 LLM。

### 嵌入方式

具体的 Markdown 扩展语法待设计（将在单独章节或后续规范中确定）。核心要求：

- 能嵌入 View Schema JSON
- 前端 Markdown 渲染器识别后调用 `ViewRenderer`
- 默认 Local 模式（前端自管理状态）

### 结果采集

用户操作 Markdown 内联 UI 后，状态如何传递给 LLM：

1. **消息附带**：用户发送下一条消息时，自动附带当前所有内联 UI 的状态
   ```json
   {"role": "user", "text": "就用这个吧",
    "ui_state": {"lang": "python"}}
   ```

2. **工具获取**：LLM 调用工具主动查询内联 UI 的当前值

两种方式可并存。

### Markdown 块中也可以有后端

Markdown 内联 UI 可以关联一个 UIContext（Connected 模式）。用例：在聊天中嵌入一个由后端实时更新的状态面板。

## 集成：应用级 UI（框架设计）

### 菜单系统

现有 RPC 菜单系统可逐步迁移。菜单项触发 AppSession 子类：

```python
# 框架层面支持，不在本次实施范围
menu_item = {"label": "设置", "action": "open_session", "session_class": "SettingsSession"}
```

### 对话框

独立的 Dialog 渲染面，用于不需要创建 Session 的轻量交互（如确认操作）。后续设计。

## 传输层

### WebSocket 消息

复用现有 WebSocket 连接，新增消息类型：

**后端 → 前端：**

| 消息类型 | 说明 | 关键字段 |
|---------|------|---------|
| `ui_view` | 推送视图 | `context_id`, `view` |
| `ui_close` | 关闭 UI | `context_id`, `final_view?` |

**前端 → 后端：**

| 消息类型 | 说明 | 关键字段 |
|---------|------|---------|
| `ui_event` | 用户事件 | `context_id`, `event_type`, `data` |

`context_id` 标识 UIContext 实例。对于工具，context_id 可以直接使用 tool_call_id；对于 AppSession，使用 session_id。

## 取消机制

UIContext 本身不处理超时。超时是工具层面的关注点——普通工具和交互式工具都可能需要超时，这应该在 ToolSet.dispatch 或工具自身的业务逻辑中处理，与 UI 框架无关。

UIContext 只处理取消：

- 用户点击 UI 中的取消按钮 → `cancel` 事件 → handler 自行处理（业务逻辑级取消）
- 用户点击 Agent 停止按钮 → handler 的 `wait_event()` 抛 `CancelledError`（外部强制取消）
- 工具自身超时逻辑 → 调用 `ctx.close()` → UI 关闭

## 实施概要

实施顺序按确定性递减：先走通最明确的路径，再扩展。

**阶段一：组件系统 + 工具集成**（最确定：前端位置 = ToolCallCard，后端入口 = tool dispatch）
- 组件系统（View Schema + React ViewRenderer，初始 ~10 种组件）
- UIContext 核心实现
- WebSocket `ui_view` / `ui_event` / `ui_close` 协议
- ToolCallCard 扩展（渲染 ViewRenderer）
- Toolkit.owner 绑定 + UIToolkit（lazy UIContext 创建）
- 首个用例验证：LLM 配置流程改造

**阶段二：AppSession**（确定：Session 面板体系提供渲染位置）
- AppSession 基类（Session 子类）
- Session `_abstract` 标记 + `session.types` 过滤
- Session 面板扩展（AppSession 渲染 ViewRenderer）
- 首个 AppSession 子类（如 SetupSession 首次配置向导）

**阶段三：Markdown + 扩展**
- Markdown 内联 UI 渲染与结果采集
- 替换现有 ask/confirm 块机制
- Dialog 渲染面
- 菜单系统迁移（按需）

## 实施步骤清单

### 阶段一：组件系统 + 工具集成 [✅ 已完成]

> 目标：从后端 UIContext 到前端 ViewRenderer 全链路打通，以 ToolCallCard 为渲染面，LLM 配置流程为验证用例。

- [x] **Task 1**: mutagent 基础变更 — Toolkit.owner + dispatch 扩展
  - [x] 1.1 `Toolkit` 类新增 `owner` 属性（公开接口），类型为 `ToolSet`
  - [x] 1.2 `tool_set_impl.py` 中 ToolSet 发现/添加 Toolkit 时设置 `instance.owner = self`
  - [x] 1.3 `dispatch()` 新增 `_current_tool_call` 跟踪：进入时设置，finally 中清除
  - [x] 1.4 `dispatch()` finally 中通用清理：检查 `_active_ui`，有则调用 `close()` 并置 None
  - [x] 1.5 补充单元测试：`owner` 绑定、`_current_tool_call` 生命周期
  - 状态：✅ 已完成

- [x] **Task 2**: UIContext Declaration + UIEvent 数据结构
  - [x] 2.1 `mutbot/src/mutbot/ui/context.py` — UIContext(Declaration) 接口声明
  - [x] 2.2 `mutbot/src/mutbot/ui/events.py` — UIEvent 数据类
  - [x] 2.3 `mutbot/src/mutbot/ui/__init__.py` — 模块导出
  - 状态：✅ 已完成

- [x] **Task 3**: UIContext WebSocket 实现
  - [x] 3.1 `mutbot/src/mutbot/ui/context_impl.py` — `@impl` 实现（set_view, wait_event, show, close）
  - [x] 3.2 UIContext 实例管理：全局注册表 `{context_id: UIContext}`
  - [x] 3.3 `deliver_event(context_id, event)` 函数
  - 状态：✅ 已完成

- [x] **Task 4**: UIToolkit — mutbot 层 Toolkit 子类
  - [x] 4.1 `mutbot/src/mutbot/ui/toolkit.py` — UIToolkit(Toolkit) + lazy ui property
  - [x] 4.2 broadcast 通过 ToolSet._broadcast_fn + _session_id 注入（SessionManager.start 中设置）
  - 状态：✅ 已完成

- [x] **Task 5**: WebSocket 传输层
  - [x] 5.1-5.2 消息格式：ui_view / ui_close / ui_event
  - [x] 5.3 `routes.py` websocket_session handler 新增 `ui_event` 消息处理
  - [x] 5.4 `AgentPanel.tsx` 新增 `ui_view` / `ui_close` 事件处理，转发到 ToolCallCard
  - 状态：✅ 已完成

- [x] **Task 6+7**: 前端 ViewRenderer + UI 组件库（合并到 ToolCallCard.tsx）
  - [x] ViewRenderer 核心：formValues 状态、evaluateVisibility、ActionBar
  - [x] UIComponent 分发器 + 10 种组件（text, select, button_group, button, toggle, hint, badge, spinner, copyable, link）
  - [x] TypeScript 类型：ViewSchema, ComponentSchema, ActionSchema, UIEventPayload
  - [x] CSS 样式（CSS Variables 体系，~250 行）
  - 状态：✅ 已完成

- [x] **Task 8**: ToolCallCard 扩展
  - [x] 8.1 ToolGroupData 新增 uiView / uiFinalView 字段
  - [x] 8.2 有 UI view 时渲染 ViewRenderer（connected/readonly 模式）
  - [x] 8.3 handleUIEvent → WebSocket sendUIEvent
  - [x] 8.4 MessageList 传递 onUIEvent
  - 状态：✅ 已完成

- [x] **Task 9**: LLM 配置流程改造（验证用例）
  - [x] 9.1 `setup_toolkit.py` — SetupToolkit(UIToolkit) + configure_llm 工具
  - [x] 9.2 Copilot OAuth 交互流程（device code → 验证码 + 链接 → 轮询）
  - [x] 9.3 SetupProvider 改造：首次 send() 生成 tool_use 响应触发 SetupToolkit
  - [x] 9.4 GuideSession 在 setup 模式添加 SetupToolkit
  - 状态：✅ 已完成

- [x] **Task 10**: 测试与收尾
  - [x] 10.1-10.3 UIContext 单元测试（11 个测试：set_view, wait_event, show, close, deliver_event, UIToolkit）
  - [x] 10.4 前端构建验证：`npm run build` 通过
  - [x] 10.5 mutagent 测试：685 passed（含 8 个新测试）
  - [x] 10.6 mutbot 测试：370 passed（含 11 个新 UIContext 测试 + 重写 SetupProvider 测试）
  - 状态：✅ 已完成

### 阶段二、三（后续实施，暂不展开）

阶段二（AppSession）和阶段三（Markdown + 扩展）在阶段一完成并验证后再展开详细步骤。

## 设计决策

| 决策 | 结论 |
|------|------|
| UIContext 类设计 | Declaration 模式。接口声明与实现分离，不同渲染面（Tool、Session）对应不同 `@impl` |
| 前端状态管理 | ViewRenderer 内 `useState` 集中管理 `formValues`。`visible_when` 在 ViewRenderer 层处理 |
| Markdown 扩展块语法 | 全新设计，待阶段三确定。不沿用之前的 `mutagent:*` 块方案 |
| UIContext 超时 | 不在 UIContext 层处理。超时是 Tool 层关注点 |
| UI 访问方式 | 通过绑定链：`Toolkit.owner` → ToolSet → Agent → Session。UIToolkit 封装便捷接口 |
| UIContext 创建 | Lazy 创建：UIToolkit.ui 首次访问时创建，非 UI 工具零开销。dispatch 不注入 |
| AppSession 设计 | Session 子类，走 Session 统一发现。子类直接实现 `handle()` |
| Session abstract 标记 | `_abstract = True` 阻止前端直接创建。AgentSession、AppSession 均为 abstract 基类 |
| Toolkit 绑定 | `Toolkit.owner` → ToolSet（公开接口）。ToolSet 发现 Toolkit 时设置 |

当前无待定问题。

## 关键参考

### 源码

- `mutagent/src/mutagent/tools.py` — ToolSet / Toolkit 基类，工具注册与分发
- `mutagent/src/mutagent/builtins/tool_set_impl.py:337` — `dispatch()` 实现，async 执行，原地更新 ToolUseBlock
- `mutagent/src/mutagent/builtins/block_handlers.py` — 现有 ask/confirm 块（待替换）
- `mutbot/src/mutbot/builtins/setup_provider.py` — 当前 LLM 配置状态机，含 Copilot OAuth（`_do_copilot_auth` / `_poll_github_token`）
- `mutbot/src/mutbot/web/agent_bridge.py` — WebSocket 事件转发，`broadcast_fn`，`tool_exec_start/end` 处理
- `mutbot/src/mutbot/web/rpc.py` — RPC 框架，WebSocket 消息路由
- `mutbot/src/mutbot/web/serializers.py` — 事件序列化
- `mutbot/frontend/src/panels/AgentPanel.tsx` — 前端事件 switch（需新增 `ui_view`/`ui_close`）
- `mutbot/frontend/src/components/ToolCallCard.tsx` — 工具卡片（扩展渲染 ViewRenderer）
- `mutbot/frontend/src/index.css` — CSS Variables 体系（`--bg`, `--accent`, `--border` 等）
- `mutbot/frontend/package.json` — React 19, Vite 6, flexlayout-react, 无 UI 组件库

### 外部参考

- **NiceGUI**：Server-authoritative element tree, asyncio.Future 等待用户事件, Outbox 批量推送
- **Phoenix LiveView**：Server-rendered HTML + WebSocket diffing, 同一编程模型的先例
