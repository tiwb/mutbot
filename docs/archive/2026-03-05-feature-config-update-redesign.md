# Config-update 重新设计 — 基于 View 的安全配置更新

**状态**：✅ 已完成
**日期**：2026-03-05
**类型**：功能设计

## 背景

当前 `Config-update` 工具存在安全隐患：

1. **Bot 可获知用户输入值** — 返回消息包含 `Configuration saved: {key} = {value}`，敏感信息（API Key、密码）直接暴露给 LLM
2. **单项配置** — 每次调用只能设置一个 key，多项配置需多次调用
3. **UI 能力有限** — 只有一个 text 输入框，无法使用 password、select 等组件类型

### 目标

- Bot 生成 View（类似 UI-show），声明多个配置项的 UI 组件
- 用户在 UI 上填写/修改配置值
- 提交后，前端/后端直接写入 Config，**不将用户输入值返回给 Bot**
- 仅告知 Bot 哪些配置项更新成功

## 设计方案

### 核心设计

**Bot 发送 View，系统处理值，Bot 只收到结果摘要。**

新的 `Config-update` 工具签名：

```python
async def update(self, view: dict) -> dict:
    """通过 UI 表单更新配置。

    Args:
        view: 声明式 View，组件的 id 即为 config key。
              组件类型复用 UI-show 组件系统（text、select、toggle 等）。

    Returns:
        {"updated": ["key1", "key2"], "cancelled": false}
        或 {"updated": [], "cancelled": true}
    """
```

**View 中组件 `id` 直接作为 config key**：

```json
{
  "title": "配置 Web 工具",
  "components": [
    {
      "type": "text", "id": "WebToolkit.jina_api_key",
      "label": "Jina API Key",
      "secret": true,
      "placeholder": "jina_xxx..."
    },
    {
      "type": "toggle", "id": "WebToolkit.enabled",
      "label": "启用 Web 工具",
      "value": true
    },
    {
      "type": "hint", "id": "__info",
      "text": "API Key 可从 https://jina.ai 获取"
    }
  ]
}

```

**规则**：
- `id` 以 `__` 开头的组件为纯展示，不写入 config（如 hint）
- 其余组件的 `id` 作为 config key，值作为 config value
- `secret: true` 的 text 组件在前端渲染为密码输入框

### 数据流

```
Bot 调用 Config-update(view={...})
  → 后端展示 UI（复用 ui.show()）
  → 用户填写表单
  → 用户点击提交
  → 后端收到 {component_id: value, ...}
  → 后端遍历结果：
      对每个 id 不以 __ 开头的组件：
        config.set(id, value)
  → 返回给 Bot：{"updated": ["WebToolkit.jina_api_key", "WebToolkit.enabled"]}
```

**Bot 永远不会看到用户填写的具体值。**

### 默认 actions

如果 Bot 的 view 中未提供 `actions`，自动补充默认按钮：

```json
"actions": [
  {"type": "cancel", "label": "取消"},
  {"type": "submit", "label": "保存", "primary": true}
]
```

Bot 也可以自定义 actions（如改 label），但 type 必须保持 submit/cancel。

### 空值处理

- 用户提交时，空字符串的 text 字段跳过（不写入 config），也不计入 updated 列表
- toggle 类组件始终有值（true/false），正常写入

### 与 UI-show 的对比

| 维度 | UI-show | Config-update（新） |
|------|---------|-------------------|
| 返回给 Bot | 所有组件值 `{id: value}` | 仅更新的 key 列表 `{"updated": [...]}` |
| 副作用 | 无，Bot 自行处理数据 | 自动写入 Config |
| 组件 id 含义 | 任意标识 | 即 config key（`_` 开头除外） |
| 适用场景 | 通用交互 | 配置修改 |

### 对现有调用方的影响

当前调用 `Config-update` 的地方：
- `web_jina_ext.py` — 错误提示中引导 bot 调用 `Config-update` 设置 Jina API Key
- Bot 自身基于 tool description 理解参数

**变更**：
- 旧签名 `update(key, default_value, description)` → 新签名 `update(view)`
- `_customize_schema()` 需更新 tool description，说明 view 格式和返回值语义（已决定不需要，docstring 足够）
- 引用 Config-update 的错误提示文案需更新

### 值回填策略

Bot 可在 view 组件中设置 `value` 字段（如帮用户预填建议值）。后端展示 UI 前的回填逻辑：

- **Bot 提供了 `value`**：使用 Bot 提供的值（Bot 主动帮用户填写建议值的场景）
- **Bot 未提供 `value`**：自动从 Config 中读取已有值回填，让用户看到当前配置

这样 Bot 既可以帮用户预填值，也不需要先查询 Config（避免 Bot 看到敏感值）。tool description 中需说明此规则。

### Config key 权限

不限制 Bot 可设置的 config key 范围。操作需用户在 UI 上确认提交，用户是最终把关者。

### tool description

不在 description 中列出所有可用 config key。description 只说明 view 格式和返回值语义。具体可用的 config key 由各 Toolkit 在错误提示或文档中告知 Bot。

## 关键参考

### 源码
- `mutbot/src/mutbot/builtins/config_toolkit.py:225-264` — 当前 Config-update 实现
- `mutbot/src/mutbot/ui/toolkit.py` — UIToolkitBase、UIToolkit（UI-show）
- `mutbot/src/mutbot/ui/context.py` — UIContext Declaration
- `mutbot/src/mutbot/builtins/web_jina_ext.py` — 引用 Config-update 的错误提示

### 相关规范
- `docs/specifications/feature-interactive-ui-tools.md` — 后端驱动 UI 框架（组件系统）
- `docs/specifications/feature-ui-show-tool.md` — UI-show 工具设计

## 实施步骤清单

### 阶段一：改造 Config-update 后端 [✅ 已完成]

- [x] **Task 1.1**: 改造 `ConfigToolkit.update()` 方法
  - [x] 新签名：`async def update(self, view: dict) -> dict`
  - [x] docstring 说明 view 格式（与 UI-show 相同）、id = config key、`__` 开头为展示组件、值回填规则、返回值语义
  - [x] 值回填逻辑：遍历 view components，对 `id` 不以 `__` 开头且 Bot 未提供 `value` 的组件，从 `self._config.get(id)` 回填
  - [x] 默认 actions：如果 view 无 `actions`，补充默认的取消/保存按钮
  - [x] 调用 `self.ui.show(view)` 展示 UI
  - [x] 处理返回数据：遍历结果，对 `id` 不以 `__` 开头的组件执行 `config.set(id, value)`；空字符串跳过
  - [x] 返回 `{"updated": [...], "cancelled": false/true}`，不包含用户输入值
  - 状态：✅ 已完成

### 阶段二：更新调用方 [✅ 已完成]

- [x] **Task 2.1**: 更新 `web_jina_ext.py` 的 `_CONFIG_HINT`
  - [x] 旧格式（key + description 参数）→ 新格式（view 参数）
  - 状态：✅ 已完成

### 阶段三：测试验证 [✅ 已完成]

- [x] **Task 3.1**: 验证构建和现有测试
  - [x] 确认无语法/import 错误
  - [x] 运行现有相关测试（22 passed）
  - 状态：✅ 已完成
