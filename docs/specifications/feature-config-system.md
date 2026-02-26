# 配置系统改进 设计规范

**状态**：🔄 进行中
**日期**：2026-02-25
**类型**：功能设计

## 1. 背景

### 1.1 问题

1. **Copilot 认证后无配置**：`python -m mutbot.copilot.auth` 完成 OAuth 认证后，用户需手动创建 `.mutagent/config.json` 并编写模型配置，否则代理无法使用
2. **首次启动无引导**：mutbot 首次启动时，如果没有任何模型配置，用户不知道如何开始
3. **配置搜索路径写死**：mutagent `Config.load()` 固定搜索 `~/` 和 `./` 两个前缀，上层应用（如 mutbot）无法添加自己的搜索路径
4. **敏感值硬编码**：`auth_token` 等敏感信息直接写在 JSON 中，无法引用环境变量

### 1.2 目标

1. mutbot 首次启动时，通过终端向导引导用户选择 LLM 提供商并完成配置
2. mutagent `Config.load()` 接受完整的配置文件路径列表，调用方决定搜索哪些文件
3. GitHub token 直接存在模型配置的字段中（与 `auth_token` 同层），多模型共享时用环境变量引用
4. 配置值支持 `$ENV_VAR` 语法引用环境变量

## 2. 设计方案

### 2.1 Config.load() 接受配置文件列表

`Config.load()` 接受显式的文件路径列表，支持 `~` 和相对路径自动展开：

```python
# mutagent/config.py
@classmethod
def load(cls, config_files: list[str | Path]) -> Config:
    """从配置文件列表构建 Config 对象。

    文件按列表顺序加载，靠后的优先级更高。
    不存在的文件自动跳过。

    路径展开规则：
    - "~" 前缀展开为用户目录（Path.home()）
    - 相对路径相对于 cwd 展开
    - 绝对路径不变

    Args:
        config_files: 配置文件路径列表（低优先级 → 高优先级）。
    """
```

**mutagent 独立使用**：
```python
config = Config.load(["~/.mutagent/config.json", ".mutagent/config.json"])
```

**mutbot 使用**：
```python
config = Config.load([
    "~/.mutbot/config.json",       # mutbot 用户级
    ".mutbot/config.json",         # 项目级
])
```

### 2.2 mutbot 配置层级

| 优先级 | 路径 | 说明 |
|--------|------|------|
| 低 | `~/.mutbot/config.json` | mutbot 用户级（向导写入此处） |
| 高 | `.mutbot/config.json` | 项目级覆盖 |

```python
# mutbot/runtime/config.py
from mutagent.config import Config

MUTBOT_CONFIG_FILES = [
    "~/.mutbot/config.json",      # mutbot 用户级
    ".mutbot/config.json",        # 项目级（最高）
]

def load_mutbot_config() -> Config:
    """加载 mutbot 配置（两层合并）。"""
    return Config.load(MUTBOT_CONFIG_FILES)
```

### 2.3 首次启动配置向导

mutbot 启动时检测配置中是否包含模型。如果无模型配置，在终端启动交互式向导：

```
No LLM models configured. Let's set one up.

Select a provider:
  [1] GitHub Copilot (free with GitHub account)
  [2] Anthropic (Claude)
  [3] OpenAI
  [4] Other OpenAI-compatible API

> 1

Starting GitHub Copilot authentication...
  Open: https://github.com/login/device
  Enter code: ABCD-1234

Waiting for authorization... done!

Config written to ~/.mutbot/config.json
  Models: copilot-claude (claude-sonnet-4), copilot-gpt (gpt-4.1)
  Default: copilot-claude

Starting MutBot server...
```

**各提供商向导流程**：

| 提供商 | 需要输入 | 自动检测 |
|--------|----------|----------|
| GitHub Copilot | 无（OAuth 设备流） | — |
| Anthropic | API key（从 `$ANTHROPIC_API_KEY` 自动填充） | 环境变量 |
| OpenAI | API key（从 `$OPENAI_API_KEY` 自动填充） | 环境变量 |
| Other | base_url, API key, model_id | — |

**环境变量自动填充**：Anthropic / OpenAI 路径在向导中先检测对应环境变量，找到则自动填入并让用户确认，未找到再要求手动输入：

```
Select a provider:
  [1] GitHub Copilot (free with GitHub account)
  [2] Anthropic (Claude)
  [3] OpenAI
  [4] Other OpenAI-compatible API

> 2

Detected ANTHROPIC_API_KEY from environment.
API key: sk-ant-...***Xk2 (from $ANTHROPIC_API_KEY)
Use this key? [Y/n] y

Select model:
  [1] claude-sonnet-4 (recommended)
  [2] claude-haiku-4.5
  [3] claude-opus-4

> 1

Config written to ~/.mutbot/config.json
  Model: anthropic-claude (claude-sonnet-4)
```

写入配置时，如果 key 来自环境变量，使用环境变量引用而非明文值：

```json
{
  "models": {
    "anthropic-claude": {
      "provider": "AnthropicProvider",
      "base_url": "https://api.anthropic.com",
      "auth_token": "$ANTHROPIC_API_KEY",
      "model_id": "claude-sonnet-4"
    }
  }
}
```

**实现位置**：`mutbot/cli/setup.py`（独立模块），在 `mutbot/__main__.py` 启动前调用：

```python
# mutbot/__main__.py
config = load_mutbot_config()
if not config.get("models"):
    from mutbot.cli.setup import run_setup_wizard
    run_setup_wizard()
    config = load_mutbot_config()  # 重新加载
```

### 2.4 Copilot token 存在模型配置中

GitHub token 直接作为模型配置的字段（`github_token`），与 `auth_token` 同层，保持模型配置自包含：

```json
{
  "default_model": "copilot-claude",
  "models": {
    "copilot-claude": {
      "provider": "mutbot.copilot.provider.CopilotProvider",
      "model_id": "claude-sonnet-4",
      "github_token": "ghu_xxxxxxxxxxxx"
    },
    "copilot-gpt": {
      "provider": "mutbot.copilot.provider.CopilotProvider",
      "model_id": "gpt-4.1",
      "github_token": "ghu_xxxxxxxxxxxx"
    }
  }
}
```

**多模型共享 token 时**，用环境变量引用（依赖 2.5 节环境变量展开）：

```json
{
  "models": {
    "copilot-claude": {
      "provider": "mutbot.copilot.provider.CopilotProvider",
      "model_id": "claude-sonnet-4",
      "github_token": "$GITHUB_COPILOT_TOKEN"
    },
    "copilot-gpt": {
      "provider": "mutbot.copilot.provider.CopilotProvider",
      "model_id": "gpt-4.1",
      "github_token": "$GITHUB_COPILOT_TOKEN"
    }
  }
}
```

**CopilotAuth 改造**：
- 删除 `TOKEN_DIR` / `TOKEN_PATH` / `_load_github_token()` / `_save_github_token()`
- `CopilotProvider.from_config(model_config)` 直接从 `model_config["github_token"]` 获取 token
- 认证写入走向导的 `_write_config()`

### 2.5 环境变量引用

配置值支持 `$ENV_VAR` 和 `${ENV_VAR}` 语法：

```json
{
  "models": {
    "openai": {
      "provider": "OpenAIProvider",
      "base_url": "https://api.openai.com/v1",
      "auth_token": "$OPENAI_API_KEY",
      "model_id": "gpt-4.1"
    }
  }
}
```

**实现位置**：mutagent `Config.get()` 返回值上做环境变量展开（通用需求，mutagent 独立使用同样受益）。

```python
import re

def _expand_env(value: Any) -> Any:
    """递归展开配置值中的环境变量引用。"""
    if isinstance(value, str):
        return re.sub(
            r'\$\{([^}]+)\}|\$([A-Za-z_][A-Za-z0-9_]*)',
            lambda m: os.environ.get(m.group(1) or m.group(2), m.group(0)),
            value,
        )
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value
```

**规则**：
- `$VAR` 或 `${VAR}`：替换为环境变量值
- 环境变量不存在时保留原文（不报错，便于调试）
- 仅对 `str` 值展开，不影响 `int`/`bool` 等类型
- 展开在 `get()` 返回时执行（惰性），不修改 `_layers` 中的原始数据

### 2.6 get_model() 放宽 auth_token 校验

当前 `Config.get_model()` 在 `auth_token` 为空时 `raise SystemExit`。CopilotProvider 用 `github_token` 而非 `auth_token`，需放宽校验：

- 移除 `get_model()` 中的 `auth_token` 必填校验
- 各 `Provider.from_config()` 自行校验所需字段（AnthropicProvider 校验 `auth_token`，CopilotProvider 校验 `github_token`）

## 4. 实施步骤清单

### 阶段一：mutagent Config 改造 [✅ 已完成]

- [x] **Task 1.1**: `Config.load()` 改为接受配置文件列表
  - [x] 新签名：`load(cls, config_files: list[str | Path] | str | Path) -> Config`
  - [x] `str | Path` 时自动展开为 `[~/{path}, ./{path}]`（向后兼容）
  - [x] `list` 时按序加载，`~` 前缀展开为 home，相对路径相对 cwd，靠后优先级高
  - 状态：✅ 已完成

- [x] **Task 1.2**: `Config.get()` 增加环境变量展开
  - [x] `_expand_env()` 递归展开函数
  - [x] 在 `get()` 返回值上应用
  - [x] 不修改 `_layers` 原始数据
  - 状态：✅ 已完成

- [x] **Task 1.3**: `get_model()` 移除 auth_token 必填校验
  - [x] 由各 Provider.from_config() 自行校验
  - 状态：✅ 已完成

### 阶段二：mutbot 配置加载改造 [✅ 已完成]

- [x] **Task 2.1**: 新建 `mutbot/runtime/config.py`
  - [x] `MUTBOT_USER_DIR` / `MUTBOT_CONFIG_FILES` 常量
  - [x] `load_mutbot_config()` 函数：传入三层配置文件列表
  - 状态：✅ 已完成

- [x] **Task 2.2**: 改造现有配置加载点
  - [x] `_load_config()` → `load_mutbot_config()`
  - [x] `SessionManager._get_config()` → `load_mutbot_config()`
  - 状态：✅ 已完成

### 阶段三：首次启动向导 + Copilot 认证改造 [✅ 已完成]

- [x] **Task 3.1**: `mutbot/cli/setup.py` 配置向导
  - [x] 提供商选择菜单（Copilot / Anthropic / OpenAI / Other）
  - [x] Copilot 路径：调用 OAuth 设备流，token 写入模型配置
  - [x] Anthropic 路径：检测 `$ANTHROPIC_API_KEY`，有则自动填充并确认，无则要求输入
  - [x] OpenAI 路径：检测 `$OPENAI_API_KEY`，同上
  - [x] Other 路径：输入 base_url、API key、model_id
  - [x] 来自环境变量的 key 写入配置时使用 `$VAR` 引用而非明文
  - [x] 写入 `~/.mutbot/config.json`
  - 状态：✅ 已完成

- [x] **Task 3.2**: `mutbot/__main__.py` 集成向导
  - [x] 启动前检查 `config.get("models")`
  - [x] 无模型时调用 `run_setup_wizard()`
  - 状态：✅ 已完成

- [x] **Task 3.3**: `auth.py` + `CopilotProvider` 适配
  - [x] 删除独立 token 文件逻辑
  - [x] `CopilotProvider.from_config()` 从 `model_config["github_token"]` 获取 token
  - 状态：✅ 已完成

### 阶段四：测试 [✅ 已完成]

- [x] **Task 4.1**: `Config.load()` 文件列表单元测试
  - [x] 旧签名兼容（str → 自动展开）
  - [x] list[Path] 多文件合并优先级
  - [x] 不存在的文件自动跳过
  - [x] `~` 路径展开
  - 状态：✅ 已完成

- [x] **Task 4.2**: 环境变量展开单元测试
  - [x] `$VAR` 和 `${VAR}` 语法
  - [x] 嵌套 dict/list 递归展开
  - [x] 未定义变量保留原文
  - [x] 不修改原始 _layers 数据
  - [x] 混合文本中的变量展开
  - 状态：✅ 已完成

- [x] **Task 4.3**: 配置向导单元测试
  - [x] `_write_config()` 创建新配置
  - [x] 已有配置时合并不覆盖 default_model
  - 状态：✅ 已完成

- [x] **Task 4.4**: `load_mutbot_config()` 单元测试
  - [x] 三层合并优先级
  - 状态：✅ 已完成

- [x] **Task 4.5**: Provider auth_token 校验测试
  - [x] AnthropicProvider / OpenAIProvider 校验 auth_token
  - 状态：✅ 已完成

---

### 实施进度总结
- ✅ **阶段一：mutagent Config 改造** - 100% 完成 (3/3任务)
- ✅ **阶段二：mutbot 配置加载改造** - 100% 完成 (2/2任务)
- ✅ **阶段三：首次启动向导 + Copilot 认证改造** - 100% 完成 (3/3任务)
- ✅ **阶段四：测试** - 100% 完成 (5/5任务)

**核心功能完成度：100%** (13/13任务)
**单元测试覆盖：mutagent 45 通过 + mutbot 249 通过 = 294 全部通过**

## 5. 测试验证

### 单元测试
- [x] Config.load() 文件列表 (5 tests)
- [x] 环境变量展开 (9 tests)
- [x] 配置向导 _write_config() (2 tests)
- [x] load_mutbot_config() 多层合并 (2 tests)
- [x] Provider auth_token 校验 (4 tests)
- 执行结果：mutagent 45/45 通过，mutbot 249/249 通过

### 集成测试
- [ ] 端到端：首次启动向导 → 配置写入 → 代理调用（需手动验证，涉及 OAuth 交互）
- 执行结果：待手动验证
