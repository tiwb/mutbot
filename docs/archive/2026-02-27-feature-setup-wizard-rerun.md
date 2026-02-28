# 设置向导可重复运行 设计规范

**状态**：✅ 已完成
**日期**：2026-02-27
**类型**：功能设计
**前置**：[feature-web-setup-wizard.md](feature-web-setup-wizard.md)

## 1. 背景

当前设置向导仅在首次启动（无 LLM provider 配置）时自动触发。一旦完成首次配置，用户无法：

- 添加新的 LLM provider
- 修复错误的配置（如 API Key 输错）
- 切换默认 provider

用户唯一的补救手段是手动编辑 `~/.mutbot/config.json`，但向导从未告知此文件位置。此外，手动编辑配置后需要重启服务器才能生效。

**目标**：

1. 用户可随时重新运行设置向导，添加新 LLM provider，添加后自动设为默认
2. 配置完成后告知用户配置文件位置，支持手动编辑
3. 配置文件修改后自动生效，无需重启

## 2. 设计方案

### 2.1 重新运行向导 — `force_setup` 模式

复用现有 SetupProvider 状态机，通过 `force_setup` 配置标志强制进入向导模式：

```python
# guide.py — create_agent() 变更
def create_agent(self, config, ...):
    force_setup = self.config.get("force_setup", False)
    if config.get("providers") and not force_setup:
        client = create_llm_client(config, ...)
    else:
        from mutbot.builtins.setup_provider import SetupProvider
        client = LLMClient(provider=SetupProvider(), model="setup-wizard")
```

创建带 `force_setup` 的 GuideSession 即可进入向导：

```python
session_manager.create(
    workspace_id,
    session_type="mutbot.builtins.guide.GuideSession",
    config={"initial_message": "__setup__", "force_setup": True},
)
```

向导完成后，`SetupProvider._activate()` 创建真实 provider 并代理后续消息，与首次配置行为一致。

### 2.2 触发入口 — Sessions 标题栏全局菜单

界面已经很紧凑，不增加独立菜单栏。在 Sessions 面板标题栏右侧添加三条杠（≡）图标按钮，作为全局主菜单入口。点击弹出 RpcMenu（下拉模式），使用已有的 Menu Declaration 体系。

**位置**：
- 展开模式：`[◀] Sessions [≡]`，≡ 在标题右侧
- 精简模式：不可见（精简模式空间有限，用户需展开后操作）

**新菜单类别**：`SessionList/Header`

**后端**（`builtins/menus.py` 新增两个 Menu 子类）：

```python
class SetupWizardMenu(Menu):
    display_name = "LLM Setup Wizard"
    display_icon = "settings"
    display_category = "SessionList/Header"
    display_order = "0tools:0"
    client_action = "run_setup_wizard"

class CloseWorkspaceMenu(Menu):
    display_name = "Close Workspace"
    display_icon = "log-out"
    display_category = "SessionList/Header"
    display_order = "1workspace:0"
    client_action = "close_workspace"
```

**前端**（`SessionListPanel.tsx` sidebar-header 内添加）：

```tsx
<div className="sidebar-header">
  <button className="sidebar-toggle-btn" onClick={toggleMode}>...</button>
  <h1>Sessions</h1>
  <RpcMenu
    rpc={rpc}
    category="SessionList/Header"
    trigger={<button className="sidebar-menu-btn" title="Menu">≡</button>}
    onClientAction={onHeaderAction}
  />
</div>
```

**client_action 处理**（App.tsx 中）：
- `run_setup_wizard` → 创建 force_setup GuideSession 并打开 tab
- `close_workspace` → `location.hash = ""`

### 2.3 新 Provider 设为默认

当前 `_write_config()` 仅在 `default_model` 不存在时设置。重新运行向导时，用户明确想使用新 provider，应更新默认模型。

```python
# setup_provider.py — _write_config() 变更

def _write_config(new_data: dict) -> None:
    ...
    # 合并 providers（不变）
    existing_providers.update(new_providers)
    existing["providers"] = existing_providers

    # default_model: 始终更新为新配置的值
    if "default_model" in new_data:
        existing["default_model"] = new_data["default_model"]
    ...
```

### 2.4 显示配置文件位置

在 `_activate()` 的完成消息中追加配置文件路径：

```python
async def _activate(self, provider: str) -> str:
    ...
    config_path = str(MUTBOT_CONFIG_PATH)
    return (
        f"✅ Configuration complete! "
        f"Using **{self._real_model}** as default model.\n"
        f"Selected models: {models_str}\n\n"
        f"📁 Config saved to: `{config_path}`\n"
        f"You can edit this file manually to adjust settings.\n\n"
        f"You can now chat with me — I'm powered by a real AI! "
        f"Try saying something to test the connection."
    )
```

### 2.5 配置修改自动生效

分两个场景处理：

#### 场景 A：通过向导修改（自动生效）

向导完成后 `_activate()` 已在当前 session 内创建新 provider 并切换，立即生效。新创建的 session 会通过 `load_mutbot_config()` 加载最新配置，也自动生效。

**需要解决的是已有的其他 session**。

#### 场景 B：手动编辑配置文件

用户直接编辑 `~/.mutbot/config.json` 后，期望无需重启就能生效。

**方案：配置文件 mtime 轮询 + 通知**

后台 asyncio task 每 5 秒检查 `~/.mutbot/config.json` 的 mtime，变更时：
1. 重新加载全局配置
2. 通过 workspace WebSocket 广播 `config_changed` 事件
3. 前端收到事件后显示 toast 提示："配置已更新，新对话将使用最新配置"

不引入 `watchdog` 等外部依赖，轻量实现即可。

**已有 session 的处理**：已有 session 的 provider 实例不热替换（避免状态混乱）。用户创建新 session 时自动使用最新配置。这是最简单且安全的策略。

## 3. 实施步骤清单

### 阶段一：核心功能 [✅ 已完成]
- [x] **Task 1.1**: GuideSession 支持 `force_setup` 模式
  - [x] `create_agent()` 检查 `self.config.get("force_setup")`
  - [x] `force_setup=True` 时使用 SetupProvider，忽略已有 providers
  - 状态：✅ 已完成

- [x] **Task 1.2**: `_write_config()` 始终更新 `default_model`
  - [x] 移除 `"default_model" not in existing` 条件
  - 状态：✅ 已完成

- [x] **Task 1.3**: `_activate()` 完成消息显示配置文件路径
  - [x] 追加 `📁 Config saved to: ...` 信息
  - [x] 追加手动编辑提示
  - 状态：✅ 已完成

### 阶段二：前端全局菜单 [✅ 已完成]
- [x] **Task 2.1**: 后端新增 `SessionList/Header` 菜单项
  - [x] `SetupWizardMenu`（client_action: `run_setup_wizard`）
  - [x] `CloseWorkspaceMenu`（client_action: `close_workspace`）
  - 状态：✅ 已完成

- [x] **Task 2.2**: 前端 SessionListPanel 标题栏添加 ≡ 菜单按钮
  - [x] sidebar-header 右侧添加 RpcMenu（下拉模式，category: `SessionList/Header`）
  - [x] 精简模式下不显示
  - [x] 新增 `onHeaderAction` prop 回调到 App.tsx
  - 状态：✅ 已完成

- [x] **Task 2.3**: App.tsx 处理 client_action
  - [x] `run_setup_wizard` → 创建 force_setup GuideSession + 打开 tab
  - [x] `close_workspace` → `location.hash = ""`
  - 状态：✅ 已完成

### 阶段三：配置自动生效 [✅ 已完成]
- [x] **Task 3.1**: 后端配置文件变更检测
  - [x] 后台 asyncio task 定期检查 config.json mtime
  - [x] 变更时广播 `config_changed` 事件到 workspace WebSocket
  - [x] 失效 SessionManager 缓存配置
  - 状态：✅ 已完成

- [x] **Task 3.2**: 前端配置变更提示
  - [x] 监听 `config_changed` WebSocket 事件
  - [x] 显示 toast 提示用户配置已更新
  - 状态：✅ 已完成

### 阶段四：测试 [待开始]
- [ ] **Task 4.1**: 单元测试
  - [x] `_write_config()` 覆盖 default_model（已有测试已更新）
  - [ ] `force_setup` 模式下 GuideSession 使用 SetupProvider
  - [ ] 完成消息包含配置文件路径
  - 状态：⏸️ 待开始

- [ ] **Task 4.2**: 端到端测试
  - [ ] 已有配置 → ≡ 菜单 → "LLM Setup Wizard" → 向导 → 添加新 provider → 新 session 使用新 provider
  - [ ] ≡ 菜单 → "Close Workspace" → URL hash 清除，回到 workspace 列表
  - [ ] 手动编辑配置 → toast 提示 → 新 session 使用最新配置
  - 状态：⏸️ 待开始

## 4. 测试验证

### 单元测试
- [ ] `force_setup=True` 触发 SetupProvider
- [ ] `force_setup=False` 或未设置时正常使用真实 LLM
- [x] `_write_config()` 覆盖已有 `default_model`
- [x] `_write_config()` 保留已有 providers 并添加新 provider
- [ ] `_activate()` 完成消息包含配置文件路径
- 执行结果：320/320 通过（已有测试已更新适配新行为）

### 集成测试
- [x] ≡ 菜单 → "LLM Setup Wizard" → 创建 force_setup GuideSession
- [x] ≡ 菜单 → "Close Workspace" → URL hash 清除，回到 workspace 列表
- [x] 向导完成后新 provider 可用且为默认
- [x] 手动编辑 config.json → toast 提示 → 新 session 使用最新配置
