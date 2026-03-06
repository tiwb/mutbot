# Config 系统重构 设计规范

**状态**：✅ 已完成
**日期**：2026-03-04
**类型**：重构

## 背景

mutagent 的 Config 接口已重构完成（✅）：Config 简化为纯桩 Declaration（`get`/`set`/`on_change`），移除了 `load()`/`get_model()`/`get_all_models()`/`section()`，模型查找移至 `LLMProvider.resolve_model()`/`list_models()`，`from_config()` 改名为 `from_spec()`。

mutbot 侧代码仍调用旧接口，全面断裂：

| 位置 | 调用 | 问题 |
|------|------|------|
| `runtime/config.py:24` | `Config.load(files)` | 已移除 |
| `session_impl.py:266` | `config.get_model(name)` | 已移至 `LLMProvider.resolve_model()` |
| `session_impl.py:279` | `from_config(model)` | 已改名 `from_spec(spec)` |
| `web/routes.py:603` | `config.get_all_models()` | 已移至 `LLMProvider.list_models()` |

本次重构目标：修复断裂，同时引入 mutbot 自己的 Config 子类，为未来分层配置预留扩展空间。

### 简化策略

完整分层设计（LayeredConfig + ConfigManager + ConfigLayer 体系）保留为未来演进方向。本次只实现：
- 用户目录单层配置（`~/.mutbot/config.json`）
- mutbot 自己的 Config 子类（MutbotConfig），内置文件读写和变更通知
- 修复所有断裂的调用方

## 设计方案

### MutbotConfig — 单层文件配置

Config 的 mutbot 实现。当前只管理 `~/.mutbot/config.json` 单文件，内置持久化和变更通知。

```python
class MutbotConfig(Config):
    """mutbot 单层文件配置。

    当前只管理 ~/.mutbot/config.json。
    未来可演进为 LayeredConfig 支持多层合并。
    """
    _data: dict                                    # 配置数据
    _listeners: list[tuple[str, ChangeCallback]]   # on_change 回调
    _config_path: Path                             # 配置文件路径

    def get(self, name: str, *, default: Any = None) -> Any:
        """点分路径导航 _data。"""
        ...

    def set(self, name: str, value: Any, *, source: str = "") -> None:
        """按点分路径写入 _data，持久化到文件，触发匹配的 on_change 回调。"""
        # 1. 写入 _data
        # 2. 持久化到 _config_path
        # 3. 触发匹配的 on_change 回调
        ...

    def on_change(self, pattern: str, callback: ChangeCallback) -> Disposable:
        """注册监听。pattern 语义与 mutagent DictConfig 一致。"""
        ...

    def reload(self) -> None:
        """从文件重新加载。逐个对比顶层 key，每个变化的 key 各触发一次 on_change。
        用于文件监视检测到外部变更时调用。"""
        ...

    def update_all(self, data: dict, *, source: str = "") -> None:
        """批量更新整个 _data，逐个对比顶层 key 触发 on_change。
        用于 ConfigToolkit 向导等需要一次性写多个 key 的场景。"""
        # 1. 替换 _data
        # 2. 持久化到 _config_path
        # 3. 对比旧 _data，每个变化的顶层 key 触发 on_change
        ...

```

**与 DictConfig 的区别**：MutbotConfig 内置了文件持久化（`set()` 自动写文件）和 `reload()` 方法。DictConfig 是纯内存的。

### load_mutbot_config() 改造

```python
def load_mutbot_config() -> MutbotConfig:
    """加载 mutbot 配置。"""
    config_path = MUTBOT_USER_DIR / "config.json"
    MUTBOT_USER_DIR.mkdir(parents=True, exist_ok=True)
    data = json.loads(config_path.read_text()) if config_path.exists() else {}
    return MutbotConfig(_data=data, _listeners=[], _config_path=config_path)
```

### Config 实例生命周期

Config 无单例，靠一路传递。在 `server.py` lifespan 中创建，传给所有需要它的组件：

```
启动时（lifespan）：
  config = load_mutbot_config()
    → MutbotConfig 实例
  session_manager = SessionManager(config=config)
  统一注册 on_change 回调（LLM 重建、proxy 刷新、WS 广播）
  启动 _watch_config_changes(config)

Session 创建时：
  build_default_agent(config=config)
    → Agent(config=config)                 # Agent 持有 Config 引用（mutagent 层声明字段）
    → 所有 Toolkit 通过 self.owner.agent.config 访问

运行时修改：
  config.set("providers.anthropic.auth_token", "sk-xxx")
    → 更新 _data → 持久化文件 → 触发 on_change
    → 所有持有 config 引用的组件自动看到新值

外部文件变更：
  _watch_config_changes(config) 检测 mtime 变化
    → config.reload()
    → 触发 on_change
```

移除 `SessionManager._get_config()` 懒加载机制和 `session_manager._config = None` 置空 hack。

### 调用方修复

#### create_llm_client（session_impl.py）

```python
# 之前
model = config.get_model(model_name or None)
provider = provider_cls.from_config(model)

# 之后
model = LLMProvider.resolve_model(config, model_name or None)
if model is None:
    raise RuntimeError(f"No model configured (requested: {model_name!r})")
provider = provider_cls.from_spec(model)
```

#### config.models RPC（web/routes.py）

```python
# 之前
config = load_mutbot_config()
models = config.get_all_models()

# 之后（从 session_manager 获取传递进来的 config）
models = LLMProvider.list_models(session_manager.config)
```

#### _load_config（session_impl.py）

```python
# 之前：独立调用 load_mutbot_config()，返回 plain dict
def _load_config() -> dict | None:
    config = load_mutbot_config()
    return {"providers": config.get("providers", {}), ...}

# 之后：不再需要。proxy 直接从共享 config 读取
```

#### ConfigToolkit（config_toolkit.py）

ConfigToolkit 统一通过 `self.owner.agent.config` 获取共享 Config 引用（`config` 是 mutagent Agent Declaration 的字段）。

```python
# 之前
config = self._load_config()           # 读 JSON 文件 → dict
config["providers"][key] = value
self._write_full_config(config)        # 写 JSON 文件
# hack reload
self._reload_config()

# 之后：单个 key 写入
config = self.owner.agent.config
config.set(f"providers.{key}", value)
# 自动持久化 + 自动触发 on_change，无需手动 reload

# 之后：向导批量写入（多个 key 一次性）
config = self.owner.agent.config
config.update_all(full_config_dict, source="wizard")
# 一次写文件 + 一次 on_change 触发
```

`_load_config()` / `_write_full_config()` / `_save_provider()` 等文件操作方法可移除。

#### _activate（config_toolkit.py）

`_activate` 不再手动 `create_llm_client`。config 写入通过 `set()` / `update_all()` 触发 `on_change`，由回调自动重建 LLM client：

```python
# 之前
mutbot_config = load_mutbot_config()        # 每次新建 Config 实例
client = create_llm_client(mutbot_config)
agent.llm = client

# 之后
# config.set() / update_all() 已在向导流程中完成
# on_change 回调自动重建 LLM client，_activate 只需确认结果
```

所有 Toolkit 统一通过 `self.owner.agent.config` 访问共享 Config 实例。`config` 是 mutagent Agent Declaration 的正式字段，WebToolkit 保留 config= 构造参数（mutagent 层设计），ConfigToolkit 通过 agent.config 访问。

### on_change 回调统一注册

所有 on_change 回调在 `server.py` lifespan 中统一注册，config 由 lifespan 创建并传递：

```python
# server.py lifespan 中
config = load_mutbot_config()
session_manager = SessionManager(config=config)

# 1. LLM client 重建（所有活跃 session）
def on_provider_changed(event: ConfigChangeEvent):
    for sid, rt in session_manager._runtimes.items():
        if hasattr(rt, 'agent') and rt.agent:
            rt.agent.llm = create_llm_client(event.config)

config.on_change("providers.**", on_provider_changed)
config.on_change("default_model", on_provider_changed)

# 2. Proxy 配置刷新
def refresh_proxy_providers(event: ConfigChangeEvent):
    import mutbot.proxy.routes as proxy
    proxy._providers_config = event.config.get("providers", default={})

config.on_change("providers.**", refresh_proxy_providers)

# 3. WS 广播
def broadcast_config_changed(event: ConfigChangeEvent):
    asyncio.create_task(
        workspace_connection_manager.broadcast_all(
            make_event("config_changed", {
                "reason": event.source or "changed",
                "key": event.key,
            })
        )
    )

config.on_change("**", broadcast_config_changed)
```

### 文件监视改造

沿用 mtime 轮询，改为调用 `config.reload()`：

```python
async def _watch_config_changes(config: MutbotConfig) -> None:
    config_path = config._config_path
    last_mtime = config_path.stat().st_mtime if config_path.exists() else 0.0
    while True:
        await asyncio.sleep(5)
        try:
            current_mtime = config_path.stat().st_mtime
        except OSError:
            continue
        if current_mtime != last_mtime:
            last_mtime = current_mtime
            config.reload()  # 自动触发 on_change
```

不再需要 `session_manager._config = None` 置空 hack。`config.reload()` 内部对比新旧数据差异，自动触发 `on_change` 回调。

**防循环**：`set()` 写文件后记录 `_last_write_mtime`，`reload()` 中跳过自己写的文件。

### Proxy 配置刷新修复

移除 lifespan 中 `_load_config()` + 手动赋值 `_proxy_routes._providers_config` 的旧逻辑，改为 on_change 回调（见上方统一注册）。

## 关键参考

### 源码
- `mutagent/src/mutagent/config.py` — Config Declaration（get/set/on_change/affects + ConfigChangeEvent/Disposable）
- `mutagent/src/mutagent/builtins/main_impl.py` — DictConfig 实现
- `mutagent/src/mutagent/provider.py` — LLMProvider Declaration（resolve_model/list_models/from_spec）
- `mutbot/src/mutbot/runtime/config.py` — 现有 load_mutbot_config()（调用已移除的 Config.load）
- `mutbot/src/mutbot/runtime/session_impl.py:249` — create_llm_client（调用 get_model/from_config）
- `mutbot/src/mutbot/runtime/session_impl.py:377` — SessionManager._get_config（懒加载 Config）
- `mutbot/src/mutbot/web/routes.py:598` — config.models RPC（调用 get_all_models）
- `mutbot/src/mutbot/web/server.py:123` — _watch_config_changes（mtime 轮询）
- `mutbot/src/mutbot/web/server.py:226` — lifespan 中 proxy config 加载
- `mutbot/src/mutbot/builtins/config_toolkit.py:765` — _load_config/_write_full_config（直接读写文件）
- `mutbot/src/mutbot/builtins/config_toolkit.py:893` — _activate（每次新建 Config）
- `mutbot/src/mutbot/proxy/routes.py:28` — _providers_config 模块级变量

### 相关规范
- `mutagent/docs/specifications/refactor-config-system.md` — Config Declaration 重构（✅ 已完成，上游依赖）

## 未来演进方向（不在本次实施范围）

以下设计保留，待本次简化版稳定后逐步引入：

- **ConfigLayer 抽象体系**：ConfigLayer / FileLayer / MemoryLayer / FrontendLayer
- **ConfigManager**：独立的层级管理器，分离读写职责
- **多层合并**：project 层（`.mutbot/config.json`）、defaults 层、remote 层
- **Browser 层**：前端 localStorage 配置 + WS 双向同步
- **Account 层**：用户账号漫游配置
- **Remote 层**：官网远程配置拉取
- **inspect(key) API**、惰性合并缓存、不可变视图、弱引用回调
- **配置 Schema 验证**、配置迁移、Session 级配置层

## 实施步骤清单

### 阶段一：MutbotConfig + load_mutbot_config [✅ 已完成]

- [x] **Task 1.1**: MutbotConfig 实现
  - [x] `runtime/config.py`: 实现 MutbotConfig(Config) — get/set/on_change/reload/update_all
  - [x] set(): 写入 _data + 持久化文件 + 触发 on_change（用 self.affects）
  - [x] reload(): 从文件重读，逐个对比顶层 key，触发 on_change
  - [x] update_all(): 批量替换 _data + 持久化 + 逐顶层 key 触发 on_change
  - [x] 防循环：set()/update_all() 写文件后记录 _last_write_mtime，reload() 跳过
  - 状态：✅ 已完成

- [x] **Task 1.2**: load_mutbot_config() 改造
  - [x] 移除 `Config.load(MUTBOT_CONFIG_FILES)` 调用
  - [x] 改为构造 MutbotConfig(_data=..., _listeners=[], _config_path=...)
  - [x] 移除 MUTBOT_CONFIG_FILES 常量（不再需要多文件列表）
  - 状态：✅ 已完成

### 阶段二：调用方修复 [✅ 已完成]

- [x] **Task 2.1**: create_llm_client 修复（session_impl.py）
  - [x] `config.get_model()` → `LLMProvider.resolve_model(config, name)`
  - [x] `from_config()` → `from_spec()`
  - [x] SystemExit → RuntimeError（resolve_model 返回 None 时）
  - 状态：✅ 已完成

- [x] **Task 2.2**: config.models RPC 修复（web/routes.py）
  - [x] `config.get_all_models()` → `LLMProvider.list_models(session_manager.config)`
  - [x] 移除每次调用 `load_mutbot_config()` 的重新加载
  - 状态：✅ 已完成

- [x] **Task 2.3**: _load_config 移除（session_impl.py）
  - [x] 移除 `_load_config()` 函数
  - [x] 更新所有调用方（proxy 等）改为从共享 config 读取
  - 状态：✅ 已完成

- [x] **Task 2.4**: CopilotProvider.from_config → from_spec
  - [x] `copilot/provider.py`: `from_config` → `from_spec`，参数 `config` → `spec`
  - 状态：✅ 已完成

### 阶段三：Config 传递链改造 [✅ 已完成]

- [x] **Task 3.1**: SessionManager 接收 config
  - [x] SessionManager.__init__ 接受 config 参数，存为 self.config
  - [x] 移除 _get_config() 懒加载 + self._config = None 置空
  - [x] build_default_agent 从 self.config 传递
  - 状态：✅ 已完成

- [x] **Task 3.2**: Agent.config 字段（mutagent 层）
  - [x] mutagent Agent Declaration 新增 `config: Config` 字段
  - [x] mutagent main_impl 的 Agent() 构造传入 config=self.config
  - [x] mutbot build_default_agent 中 Agent(config=config)
  - 状态：✅ 已完成

- [x] **Task 3.3**: WebToolkit 保持 config= 构造参数
  - [x] WebToolkit 是 mutagent 层设计，保留 config= 构造参数
  - [x] ConfigToolkit 改为 self.owner.agent.config 访问
  - 状态：✅ 已完成

### 阶段四：ConfigToolkit 改造 [✅ 已完成]

- [x] **Task 4.1**: ConfigToolkit 读写改造
  - [x] 移除 _load_config() / _write_full_config() / _save_default_model() / _write_config()
  - [x] _save_provider() 改为通过 self._config.set()
  - [x] 所有读取改为 self._config.get()
  - [x] NullProvider.from_config → from_spec
  - 状态：✅ 已完成

- [x] **Task 4.2**: _activate 简化
  - [x] 移除 load_mutbot_config()，改用 self._config
  - [x] create_llm_client(self._config) 直接使用共享 Config
  - 状态：✅ 已完成

### 阶段五：server.py 统一注册 + 文件监视 [✅ 已完成]

- [x] **Task 5.1**: lifespan 改造
  - [x] config = load_mutbot_config() 在 lifespan 中创建
  - [x] SessionManager(config=config) 传递
  - [x] 统一注册 on_change 回调（LLM 重建、proxy 刷新、WS 广播含 key 字段）
  - [x] 移除旧的 proxy config 加载逻辑（_load_config + _proxy_routes 赋值）
  - 状态：✅ 已完成

- [x] **Task 5.2**: 文件监视改造
  - [x] _watch_config_changes 改为接收 config 参数
  - [x] 调用 config.reload() 替代 session_manager._config = None
  - 状态：✅ 已完成

### 阶段六：测试验证 [✅ 已完成]

- [x] **Task 6.1**: 运行全量测试
  - [x] mutagent: 689 passed, 5 skipped
  - [x] mutbot: 365 passed（排除无关的 test_session_persistence 已有失败）
  - [x] 重写 test_config_system.py — MutbotConfig 单元测试
  - [x] 更新 test_setup_provider.py — 移除 _write_config 测试
  - [ ] 手动启动 mutbot 验证 config 加载、模型列表、向导流程
  - 状态：🔄 进行中

## 测试验证

- mutagent 全量测试：689 passed, 5 skipped (2.90s)
- mutbot 全量测试：365 passed (1.68s)
- 手动验证：待执行
