# 可演化块系统与多媒体内容

**状态**：⏸️ 暂缓
**日期**：2026-02-25
**类型**：功能设计
**来源**：从 `feature-web.md` 阶段五拆分

## 1. 背景

本规范从 `feature-web.md` 的阶段五拆分而来。当前 mutbot Web UI 已实现内置块渲染器（第一层），本规范描述后续的声明式块引擎（第二层）、自定义块沙箱（第三层）及多媒体内容的完整设计。

当前已实现的内置块渲染参见 `feature-web.md` 第 4.4 节。

## 2. 可演化的内容块系统

mutagent 的块类型（`mutagent:code`、`mutagent:tasks` 等）需要在 Web 端渲染。设计目标：**Agent 可以在运行时定义新的块类型和渲染方式**，无需重新构建前端。

### 2.1 块渲染的三层架构

```
第一层：内置块渲染器（React 组件，预构建）[✅ 已实现]
  → 已知块类型使用优化过的 React 组件渲染
  → code, tasks, status, thinking, ask, confirm, agents

第二层：声明式块（JSON Schema 驱动，无需 JS）
  → Agent 通过 define_module 注册新块类型的渲染 schema
  → 前端通用渲染器根据 schema 生成 UI（表格、列表、键值对、进度条等）
  → 类似低代码表单引擎

第三层：自定义块（HTML/CSS/JS，沙箱执行）
  → Agent 生成完整的 HTML+CSS+JS 渲染代码
  → 前端在 sandboxed iframe 中执行，通过 postMessage 通信
  → 最大灵活性，Agent 可创造任意可视化
```

### 2.2 块注册协议

Agent 通过 mutagent 的 `define_module` 机制注册新块类型：

```python
# Agent 在运行时定义一个新的块类型
define_module("mutbot.blocks.progress_bar", '''
block_type = "progress"
schema = {
    "type": "declarative",
    "layout": [
        {"field": "label", "render": "text", "style": "bold"},
        {"field": "value", "render": "progress_bar", "max_field": "total"},
        {"field": "status", "render": "badge", "color_map": {"done": "green", "running": "blue"}}
    ]
}
''')
```

前端收到 `block_start` 事件时：
1. 查找内置渲染器 → 找到则使用
2. 查找已注册的声明式 schema → 找到则用通用渲染器
3. 查找自定义 HTML 渲染器 → 找到则在 iframe 沙箱中执行
4. 都没有 → 降级为纯文本（代码块样式）

### 2.3 完整块类型规划

| 块类型 | 渲染层 | Web 渲染 |
|--------|--------|---------|
| `code` | 内置 | Shiki 高亮 + 复制按钮 |
| `tasks` | 内置 | 复选框列表 |
| `status` | 内置 | 状态卡片 |
| `thinking` | 内置 | 可折叠区域 |
| `ask` | 内置 | 选择列表 + 提交 |
| `confirm` | 内置 | 确认/取消按钮 |
| `agents` | 内置 | 实时状态仪表板 |
| `image` | 内置 | `<img>` 内联 |
| `chart` | 声明式/自定义 | ECharts/Plotly |
| `mermaid` | 声明式/自定义 | Mermaid.js → SVG |
| (Agent 自定义) | 声明式/自定义 | Agent 运行时定义 |

声明式块的通用渲染器只需一套代码，支持常见的展示模式（表格、列表、键值对、进度条、徽章、树形结构等），不依赖 Node.js 构建。自定义块的 iframe 沙箱也是纯浏览器能力，无需构建步骤。

## 3. 多媒体内容

| 能力 | 说明 |
|------|------|
| 图片显示 | `<img>` 内联 |
| 交互式图表 | ECharts/Plotly |
| 流程图/架构图 | Mermaid.js → SVG |
| 文件上传 | 拖拽上传作为 Agent 输入 |

## 4. 设计决策

| 决策 | 结论 |
|------|------|
| 声明式块 Schema | 先从 5-8 个核心原语开始（text、list、table、key-value、progress、badge、code、link），迭代扩展 |

## 5. 实施步骤清单

- [ ] **Task 1**: 声明式块引擎
  - [ ] 块 Schema 规范定义
  - [ ] 通用声明式渲染器
  - [ ] Agent 注册新块类型的协议
  - 状态：⏸️ 暂缓

- [ ] **Task 2**: 自定义块沙箱
  - [ ] iframe 沙箱执行环境
  - [ ] postMessage 通信协议
  - 状态：⏸️ 暂缓

- [ ] **Task 3**: 多媒体内容
  - [ ] 图片、图表、Mermaid、文件上传
  - 状态：⏸️ 暂缓
