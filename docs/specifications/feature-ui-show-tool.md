# UI-show 通用工具设计

**状态**：✅ 已完成
**日期**：2026-03-04
**类型**：功能设计

## 背景

`feature-interactive-ui-tools.md` 阶段一已完成，后端驱动 UI 框架全链路打通。当前有两类工具使用 UI：

1. **SetupToolkit**（硬编码流程）：通过 `.add()` 手动注册，`Setup-llm` 工具内部硬编码多步 UI 流程。LLM 只需决定调用工具，UI 流程由 Python 代码控制。工作正常。

2. **UIToolkit.show**（通用工具）：通过 auto-discover 注册为 `UI-show`。设计意图是让 LLM **自主构建 View Schema** 并展示交互式 UI。但当前工具描述不足——docstring 仅 "便捷方法：直接展示 UI 并等待提交。"，LLM 无从知道 View Schema 的结构（session b0e51eaf 中 LLM 传入了纯文本字符串）。

> mutagent 层面的问题（async late-bound bug、Toolkit 发现机制改进）已拆分到 `mutagent/docs/specifications/bugfix-toolkit-discovery.md`。

### 核心问题

**如何让 LLM 最大程度发挥 UI 框架的灵活性？** 即：LLM 能根据对话上下文动态决定展示什么 UI，而不依赖预定义的 Python 工具。

## 设计方案

### 核心思路：LLM 作为 UI 设计者

SetupToolkit 模式中，Python 开发者预定义 UI 流程，LLM 只是触发者。UI-show 反转这个关系——**LLM 自主设计 UI**：

- 对话中需要结构化输入时，LLM 构造 View Schema 调用 UI-show
- 用户在 UI 中填写并提交，结果返回给 LLM 继续对话
- LLM 根据返回结果可再次调用 UI-show（多步流程）

这使 LLM 从"文本对话"升级到"对话 + 交互式 UI"，能力边界由组件库决定而非硬编码工具。只暴露 `show`，交互完成即结束——不暴露 `set_view` / `close` 等底层 API。

### 使用场景

| 场景 | 说明 | 示例 |
|------|------|------|
| **结构化数据采集** | 需要多个字段的输入，文本对话效率低 | 收集项目配置、用户偏好、表单填写 |
| **选择与确认** | 让用户从选项中选择，比文本描述更直观 | 模型选择、方案对比、操作确认 |
| **多步向导** | LLM 根据前一步结果动态决定下一步 | 数据导入向导、诊断流程 |
| **信息展示 + 操作** | 展示结构化信息并附带操作按钮 | 搜索结果卡片、状态面板、配置摘要 |
| **参数微调** | 用 toggle/select 比自然语言精确 | 生成参数调节、过滤条件设定 |

### 工具描述设计：渐进式披露

#### 问题：描述过长 vs 信息不足

- **过长**：把所有组件类型、属性、示例塞进工具描述 → 占大量 system prompt tokens，每轮对话都带着
- **信息不足**：当前 docstring 一句话 → LLM 不知道怎么用

#### 方案：精简描述 + 可扩展组件

工具描述采用**最小必要信息**策略（~200 tokens）：

1. **功能说明**（2 行）：做什么、返回什么
2. **View 结构**（3 行）：`components` + `actions` 的基本结构
3. **常用组件**（3-4 种）：覆盖绝大多数场景的核心组件
4. **一个紧凑示例**：展示结构模式

**不在描述中列举所有组件类型**。组件系统是可扩展的——前端可以随时新增组件类型，工具描述不应硬编码枚举。描述中只列出最常用的几种，LLM 通过结构模式（`type` + `id` + 类型属性）举一反三。

#### 工具描述草稿

```
展示交互式 UI 并等待用户提交。返回 {组件id: 值} 字典。

view 结构：
- title: 标题（可选）
- components: 组件列表，每个组件有 type、id 和类型属性
- actions: 按钮列表，如 {"type": "submit", "label": "确认", "primary": true}

常用组件：
- text: 文本输入。属性：label, placeholder, secret, multiline
- select: 选择器。属性：label, options: [{value, label}]
- hint: 只读提示文字。属性：text（支持 Markdown）
- toggle: 布尔开关。属性：label

示例：
{"title": "选择模型", "components": [{"type": "select", "id": "model", "label": "模型", "options": [{"value": "gpt-4", "label": "GPT-4"}, {"value": "claude", "label": "Claude"}]}, {"type": "toggle", "id": "stream", "label": "流式输出"}], "actions": [{"type": "submit", "label": "确认", "primary": true}]}
```

这段描述约 200 tokens，覆盖了 90% 的使用场景。LLM 看到 `type` + `id` 的模式后，自然能推断其他组件类型的用法。

#### 为什么不需要完整枚举

1. **组件系统可扩展**：前端新增组件类型后，不需要更新工具描述。LLM 通过模式举一反三
2. **绝大多数场景简单**：一个 select + submit 或 text + submit 就够了
3. **system prompt 可补充**：如果特定 session 需要复杂 UI，可以在 system prompt 中注入更多组件文档
4. **对话上下文学习**：LLM 看到过一次 UI-show 的成功调用后，后续调用自然更准确

### 类继承结构：UIToolkitBase 拆分

将当前 UIToolkit 拆分为基础设施层和工具层：

```
Toolkit
  └── UIToolkitBase (_discoverable=False)
        │   ui property (lazy UIContext 创建)
        │   session property
        │   _resolve_broadcast()
        │
        ├── UIToolkit
        │     show(view) → 暴露为 UI-show（LLM 通用工具）
        │     _customize_schema() → 注入丰富的工具描述
        │
        └── SetupToolkit
              llm() → 暴露为 Setup-llm（硬编码流程）
              内部直接调 self.ui.show()
```

**UIToolkitBase**：基础设施，`_discoverable = False`，不暴露任何工具。提供 `ui` property、`session` property 等公开接口供子类和 Python 代码使用。

**UIToolkit**：继承 UIToolkitBase，只定义 `show()` 方法——这是暴露给 LLM 的唯一通用工具。

**SetupToolkit**：改为继承 UIToolkitBase（而非 UIToolkit）。它有自己的领域工具（`llm()` 等），内部通过 `self.ui.show()` 调用 UIContext，不需要 UIToolkit 的 `show` 工具方法。

这样 UIToolkitBase 上可以安全地添加更多公开 utility 方法（如未来的 `update_view()`、`close_ui()` 等），不会被 auto-discover 暴露为 LLM 工具。

### 工具描述注入

通过 `_customize_schema` 钩子注入工具描述和 input_schema：

```python
class UIToolkit(UIToolkitBase):
    def _customize_schema(self, method_name: str, schema: ToolSchema) -> ToolSchema:
        if method_name == "show":
            schema = schema.model_copy(update={
                "description": _UI_SHOW_DESCRIPTION,
                "input_schema": _UI_SHOW_INPUT_SCHEMA,
            })
        return schema
```

#### input_schema

```json
{
  "type": "object",
  "properties": {
    "view": {
      "type": "object",
      "description": "View Schema",
      "properties": {
        "title": {"type": "string"},
        "components": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["type", "id"]
          }
        },
        "actions": {
          "type": "array",
          "items": {"type": "object"}
        }
      },
      "required": ["components"]
    }
  },
  "required": ["view"]
}
```

input_schema 同样不在 `type` 上用 `enum` 限制，保持开放。

### 组件参考（非工具描述的一部分，供开发者参考）

当前已实现的组件：

| 组件 | type | 核心属性 | 返回值 |
|------|------|---------|--------|
| 文本输入 | `text` | `label`, `placeholder`, `secret`, `multiline` | 字符串 |
| 选择器 | `select` | `label`, `options: [{value, label}]` | 选中的 value |
| 按钮组 | `button_group` | `label`, `options: [{value, label}]` | 选中的 value |
| 开关 | `toggle` | `label` | boolean |
| 提示文本 | `hint` | `text`（支持 Markdown） | — (只读) |
| 状态徽章 | `badge` | `text`, `variant: success/info/warning/error` | — (只读) |
| 可复制文本 | `copyable` | `text` | — (只读) |
| 链接 | `link` | `url`, `label` | — (只读) |

前端新增组件时，只需在 ViewRenderer 中添加对应的 React 组件。后端和工具描述无需变更。

## 设计决策

| 决策 | 结论 |
|------|------|
| 工具名称 | `UI-show`，与 `Setup-llm`、`Web-search` 风格一致 |
| 暴露范围 | 只暴露 `show`，不暴露 `set_view` / `close` |
| 描述策略 | 渐进式披露：~200 tokens 精简描述 + 常用组件，不枚举所有类型 |
| Schema 开放性 | `components[].type` 不用 enum 限制，支持前端扩展 |
| 类继承结构 | 拆分为 UIToolkitBase（基础设施，不暴露）+ UIToolkit（通用工具）。SetupToolkit 改继承 UIToolkitBase |

## 实施步骤清单

### 阶段一：UIToolkitBase 拆分 [✅ 已完成]

- [x] **Task 1**: 拆分 `toolkit.py` — UIToolkitBase + UIToolkit
  - [x] 1.1 当前 UIToolkit 重命名为 UIToolkitBase，设置 `_discoverable = False`
  - [x] 1.2 新建 UIToolkit(UIToolkitBase)，定义 `show()` 和 `_customize_schema()`
  - [x] 1.3 更新 `mutbot/ui/__init__.py` 导出（新增 UIToolkitBase）
  - 状态：✅ 已完成

- [x] **Task 2**: SetupToolkit 改继承 UIToolkitBase
  - [x] 2.1 `setup_toolkit.py` 中 `from mutbot.ui.toolkit import UIToolkitBase`
  - [x] 2.2 `class SetupToolkit(UIToolkitBase):`
  - 状态：✅ 已完成

- [x] **Task 3**: 工具描述注入
  - [x] 3.1 UIToolkit._customize_schema 实现：注入 `_UI_SHOW_DESCRIPTION` 和 `_UI_SHOW_INPUT_SCHEMA`
  - [x] 3.2 编写工具描述常量（~200 tokens 精简版）
  - [x] 3.3 编写 input_schema 常量（开放式，不限制 component type enum）
  - 状态：✅ 已完成

- [x] **Task 4**: 测试与验证
  - [x] 4.1 现有 UIContext/UIToolkit 测试通过（11 passed）
  - [x] 4.2 全量测试：372 passed（已有 2 个无关 session persistence 测试失败，非本次改动）
  - [x] 4.3 前端构建：`npm run build` 通过
  - 状态：✅ 已完成

## 关键参考

### 源码

- `mutbot/src/mutbot/ui/toolkit.py` — 当前 UIToolkit，将拆分为 UIToolkitBase + UIToolkit
- `mutbot/src/mutbot/ui/context.py:53` — UIContext.show 声明
- `mutbot/src/mutbot/ui/context_impl.py:106` — UIContext.show 实现（set_view + wait_event）
- `mutbot/src/mutbot/builtins/setup_toolkit.py:153` — SetupToolkit，改为继承 UIToolkitBase

### 相关规范

- `mutbot/docs/specifications/feature-interactive-ui-tools.md` — 后端驱动 UI 框架总规范（阶段一已完成）
- `mutagent/docs/specifications/bugfix-toolkit-discovery.md` — Toolkit 发现机制 bug 修复（async + 继承 + 可控性）
