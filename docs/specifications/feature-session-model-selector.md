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
- **模型列表需在 WebSocket 重连和配置变更时刷新**（见 §6 Bug 修复）

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

### 阶段三：Bug 修复 — 模型列表刷新 [✅ 已完成]
- [x] **Task 3.1**: ModelSelector 监听 `config_changed` 事件
  - [x] 提取 `fetchModels` 函数
  - [x] 添加 `rpc.on("config_changed", fetchModels)` 监听
  - 状态：✅ 已完成

- [x] **Task 3.2**: 后端 WebSocket 连接时推送 `config_changed`
  - [x] 在 WebSocket 连接建立后向客户端发送 `config_changed` 事件（`reason: "connect"`）
  - [x] 配置文件变更广播时附带 `reason: "file_changed"`
  - [x] `App.tsx` toast 仅在 `reason === "file_changed"` 时显示
  - 状态：✅ 已完成

### 阶段四：测试验证 [待开始]
- [ ] **Task 4.1**: 后端测试
  - [ ] `config.models` RPC 返回正确模型列表
  - [ ] `session.update` 模型切换后 Agent 使用新模型
  - 状态：⏸️ 待开始

- [ ] **Task 4.2**: 前端集成测试
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
- [ ] 服务器重启 → WebSocket 重连 → 模型列表自动刷新
- [ ] 运行时修改配置文件 → 模型列表自动更新

## 6. Bug 修复：服务器重启后模型列表为空

### 6.1 问题现象

服务器重启后，ModelSelector 下拉菜单中模型列表为空，无法选择模型。

### 6.2 根因分析

**调用链**：`ModelSelector useEffect([rpc])` → `rpc.call("config.models")` → `load_mutbot_config()` → `Config.get_all_models()`

后端 `config.models` RPC handler（`routes.py:398`）每次调用都从磁盘重新加载配置，不存在缓存问题。配置文件 `~/.mutbot/config.json` 内容正确。**问题在前端。**

**根本原因**：`ModelSelector` 仅在挂载时获取一次模型列表，WebSocket 重连后不会重新获取。

完整流程：
1. 服务器重启 → WebSocket 断开
2. `ReconnectingWebSocket` 自动重连（`websocket.ts:66-70`）
3. `onOpen` 触发 → `App.tsx` 重新获取 `session.list`（正确）
4. **但 `WorkspaceRpc` 对象引用不变** → `rpc` 状态不变
5. `ModelSelector` 的 `useEffect([rpc])` **不会重新执行**
6. 模型列表保持为重连前的值

两种场景导致列表为空：
- **场景 A**：初始连接时 `config.models` RPC 正常返回，服务器重启后 WebSocket 重连，`useEffect` 不再触发，但由于之前已成功获取，列表不为空 —— 此场景不受影响
- **场景 B**（实际触发）：初始 RPC 调用恰好在 WebSocket 尚未完全建立时发出（`useEffect` 在 `rpc` 设置后立即执行，但 WebSocket 连接可能还没 open），`ReconnectingWebSocket.send()` 静默丢弃消息（`readyState !== OPEN`），RPC 超时后被 `.catch(() => {})` 吞掉，模型列表为空。之后 WebSocket 就绪了，但 `useEffect` 不会再执行

**次要问题**：`config_changed` 事件未触发 ModelSelector 刷新（`App.tsx:287-289` 仅弹 toast），导致运行时修改配置文件后，已打开的 ModelSelector 也不会更新。

### 6.3 修复方案

**核心思路**：ModelSelector 需要在 WebSocket 重连和配置变更时重新获取模型列表。

**修改 1：ModelSelector 监听 `config_changed` 事件**（`ModelSelector.tsx`）

将模型获取逻辑提取为 `fetchModels` 函数，在 `config_changed` 事件时重新调用：

```tsx
const fetchModels = useCallback(() => {
  rpc.call<{ models: ModelInfo[]; default_model: string }>("config.models")
    .then((result) => {
      setModels(result.models);
      setDefaultModel(result.default_model);
    })
    .catch(() => {});
}, [rpc]);

// 挂载时加载
useEffect(() => { fetchModels(); }, [fetchModels]);

// 配置变更时刷新
useEffect(() => {
  return rpc.on("config_changed", fetchModels);
}, [rpc, fetchModels]);
```

**修改 2：后端 WebSocket 连接建立时推送 `config_changed` 事件**（`server.py` 或 `routes.py`）

在 WebSocket 连接建立（`onOpen`）后，向该客户端发送 `config_changed` 事件。这样 WebSocket 重连时，客户端自动刷新配置相关状态：

```python
# WebSocket 连接建立后的 handler 中
await ws.send_event("config_changed", {"reason": "connect"})
```

配置文件变更时的推送也需附带 reason：

```python
# _watch_config_changes() 检测到文件变更时
await broadcast_event("config_changed", {"reason": "file_changed"})
```

这样一来，WebSocket 重连 → 后端推送 `config_changed` → ModelSelector 监听到事件 → 重新获取模型列表。

**效果**：
- 服务器重启 → WebSocket 重连 → `config_changed` 事件 → 模型列表刷新 ✓
- 运行时修改配置文件 → 文件监视器触发 `config_changed` → 模型列表刷新 ✓
- 初始连接 → `config_changed` 事件 → 模型列表获取 ✓（作为 `useEffect` 挂载获取的补充）

## 7. 已确认的补充决策

- **`config_changed` 事件增加 `reason` 字段**：区分 `"connect"`（WebSocket 连接建立时推送）和 `"file_changed"`（配置文件变更时推送）。`App.tsx` 的 toast 仅在 `reason === "file_changed"` 时显示，`ModelSelector` 对两种 reason 都执行刷新。
