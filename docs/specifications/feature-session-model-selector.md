# Session 模型显示与选择 设计规范

**状态**：✅ 已完成
**日期**：2026-02-28
**类型**：功能设计

## 1. 背景

当前 Agent Session 的信息栏（`agent-header`）显示 Session ID、连接数和 Token 用量，但不显示当前使用的模型。用户无法直观地看到 Session 正在使用哪个 LLM 模型，也无法在 Session 运行过程中切换模型。

需要在信息栏上增加：
1. 当前模型名称的显示
2. 点击后可选择切换模型的下拉菜单

## 2. 设计方案

### 2.1 后端：模型信息传递

**现有基础**：
- `AgentSession.model` 字段已存在（`session.py:63`），存储模型名
- `Config.get_all_models()` 已实现（`config.py:82`），可列出所有已配置模型
- `_session_dict()` 序列化 Session 时未包含 `model` 字段（`routes.py:554`）
- `session.update` RPC 方法已存在，但未支持 `model` 字段更新

**修改点**：

1. **`_session_dict()` 增加 model 字段**（`routes.py`）
   - 对 AgentSession 额外返回 `model` 字段
   - 前端即可通过 `session_created`/`session_updated` 事件获取模型名

2. **新增 `config.models` RPC 方法**（`routes.py`）
   - 返回所有已配置模型列表，供前端下拉菜单使用
   - 调用 `Config.get_all_models()`，返回 `[{name, model_id, provider_name}]`
   - 同时返回 `default_model` 名称

3. **`session.update` 支持 model 字段**（`routes.py`）
   - 允许前端通过 `session.update({session_id, model: "new-model"})` 切换模型
   - 更新 `AgentSession.model` 后，下一次对话轮次将使用新模型

4. **Agent 重建逻辑**
   - 模型切换后，需要重建 LLMClient
   - 在 `AgentBridge` 或 `SessionManager` 中添加热切换支持

### 2.2 前端：信息栏 UI

**当前信息栏布局**（`AgentPanel.tsx:355-368`）：
```
[●] Session abc12345  (2)  Context: 65% | Session: 2.3K
```

**新布局**：
```
[●] Session abc12345  (2)  [claude-opus ▾]  Context: 65% | Session: 2.3K
```

- 模型名称显示在连接数之后、Token 用量之前
- 点击模型名称展开下拉菜单，显示所有可用模型
- 当前选中模型带勾选标记
- 选择新模型后调用 `session.update` 切换

**组件设计**：
- 新建 `ModelSelector` 组件（inline dropdown）
- 模型列表通过 `config.models` RPC 获取（挂载时加载一次）
- 选中后调用 `session.update({session_id, model})` 并乐观更新 UI

### 2.3 模型切换时机

模型切换不中断当前正在进行的对话轮次，而是在下一轮次生效：
- 前端发送 `session.update` 更新 model 字段
- 后端更新 `AgentSession.model`，并重建 `LLMClient`
- 下一条消息使用新模型处理

### 2.4 已确认的设计决策

- **模型显示名**：直接使用 `get_all_models()` 返回的 `name`（即别名或 model_id），不做截断
- **Agent 运行中切换**：允许切换，下一轮次生效，当前轮次不受影响，前端不禁用下拉菜单
- **仅 AgentSession 显示**：模型选择器仅在 AgentSession 的信息栏中渲染，非 Agent 类型 Session 没有此信息栏
- **切换后保留上下文**：保留对话历史，仅更新 context_window 为新模型的值

## 4. 实施步骤清单

### 阶段一：后端支持 [✅ 已完成]
- [x] **Task 1.1**: `_session_dict()` 对 AgentSession 增加 `model` 字段输出
  - [x] 修改 `routes.py` 的 `_session_dict()` 函数
  - 状态：✅ 已完成

- [x] **Task 1.2**: 新增 `config.models` RPC 方法
  - [x] 在 `routes.py` 添加 `workspace_rpc.method("config.models")` handler
  - [x] 调用 `Config.get_all_models()` 返回模型列表 + default_model
  - 状态：✅ 已完成

- [x] **Task 1.3**: `session.update` 支持 model 字段
  - [x] 在 `handle_session_update` 中处理 `model` 参数
  - [x] 更新 `AgentSession.model` 并触发 LLMClient 重建
  - 状态：✅ 已完成

- [x] **Task 1.4**: LLMClient 热切换
  - [x] 在 `SessionManager._swap_llm_client()` 中实现模型切换后重建 LLMClient
  - [x] 对话历史保留，仅替换 agent.client
  - 状态：✅ 已完成

### 阶段二：前端实现 [✅ 已完成]
- [x] **Task 2.1**: 创建 `ModelSelector` 组件
  - [x] 实现 inline dropdown UI（`components/ModelSelector.tsx`）
  - [x] 挂载时调用 `config.models` 获取可用模型列表
  - [x] 选中时调用 `session.update` 切换模型
  - 状态：✅ 已完成

- [x] **Task 2.2**: 集成到 `AgentPanel` 信息栏
  - [x] 在 `agent-header` 中插入 `ModelSelector`（连接数与 Token 用量之间）
  - [x] 通过 `session_updated` 事件同步模型变更
  - [x] 初始化时通过 `session.get` 获取当前模型
  - 状态：✅ 已完成

- [x] **Task 2.3**: 样式适配
  - [x] dropdown 菜单样式与 VS Code Dark 主题一致
  - [x] 使用 CSS 变量保持主题统一
  - 状态：✅ 已完成

### 阶段三：测试验证 [待开始]
- [ ] **Task 3.1**: 后端测试
  - [ ] `config.models` RPC 返回正确模型列表
  - [ ] `session.update` 模型切换后 Agent 使用新模型
  - 状态：⏸️ 待开始

- [ ] **Task 3.2**: 前端集成测试
  - [ ] 模型选择器显示正确
  - [ ] 切换模型后 UI 更新
  - [ ] 信息栏布局不溢出
  - 状态：⏸️ 待开始

## 5. 测试验证

### 单元测试
- [ ] `config.models` RPC 返回结构正确
- [ ] `_session_dict()` 对 AgentSession 包含 model 字段
- [ ] `session.update` 支持 model 字段

### 集成测试
- [ ] 创建 AgentSession → 信息栏显示默认模型名
- [ ] 点击模型选择器 → 显示所有已配置模型
- [ ] 选择新模型 → session.update 调用成功 → 信息栏更新
- [ ] 发送消息 → 使用新模型响应
- [ ] 对话历史在模型切换后保留
