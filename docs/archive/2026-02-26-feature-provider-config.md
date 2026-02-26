# Provider-Based 模型配置系统 设计规范

**状态**：✅ 已完成
**日期**：2026-02-26
**类型**：功能设计

## 1. 背景

### 1.1 问题

当前 `models` 配置格式要求为每个模型单独创建配置项，包含完整的 provider 信息：

```json
{
  "models": {
    "copilot-claude": {
      "provider": "mutbot.copilot.provider.CopilotProvider",
      "model_id": "claude-sonnet-4",
      "github_token": "ghu_xxx"
    },
    "copilot-gpt": {
      "provider": "mutbot.copilot.provider.CopilotProvider",
      "model_id": "gpt-4.1",
      "github_token": "ghu_xxx"
    },
    "anthropic-claude": {
      "provider": "AnthropicProvider",
      "base_url": "https://api.anthropic.com",
      "auth_token": "$ANTHROPIC_API_KEY",
      "model_id": "claude-sonnet-4"
    }
  }
}
```

**问题**：
1. 同一 provider 的多个模型需要重复 `provider`、`base_url`、`auth_token` 等字段
2. 添加新模型需要复制整个配置块，容易出错
3. 配置向导只能硬编码模型列表，不能动态发现 provider 支持的模型
4. `/llm` 路径没有可用的 API 说明和配置指南页面

### 1.2 目标

1. 改为 provider-based 配置：每个 provider 配一次，模型列表附在 provider 下
2. 支持两种模型声明方式：list（简洁）和 dict（名称映射，用于解决跨 provider 名称冲突）
3. 配置向导：对支持列出模型的 provider，动态查询并让用户选择
4. 模型解析按 provider 顺序搜索
5. `/llm` 路径提供 API 说明页面，包含端点文档、配置指南和模型列表

### 1.3 设计决策

- **不兼容旧格式**：旧的 `models` 配置项直接忽略，不做兼容处理
- **dict 形式仅按别名匹配**：dict 的意义是创建唯一别名来区分跨 provider 的同名模型，因此只匹配 key（别名），不匹配 value（model_id）
- **`/llm` 信息页不需要认证**：API 说明页不含敏感信息，保持 `/llm` 前缀在 auth skip list 中

## 2. 设计方案

### 2.1 新配置格式

用 `providers` 取代 `models`，每个 provider 条目包含连接信息和模型列表：

**list 形式**（模型名即 model_id，适用于无名称冲突场景）：
```json
{
  "default_model": "claude-sonnet-4",
  "providers": {
    "copilot": {
      "provider": "mutbot.copilot.provider.CopilotProvider",
      "github_token": "ghu_xxx",
      "models": ["claude-sonnet-4", "gpt-4.1"]
    },
    "anthropic": {
      "provider": "AnthropicProvider",
      "base_url": "https://api.anthropic.com",
      "auth_token": "$ANTHROPIC_API_KEY",
      "models": ["claude-sonnet-4", "claude-haiku-4.5", "claude-opus-4"]
    }
  }
}
```

**dict 形式**（key 为别名，value 为 model_id，用于解决名称冲突）：
```json
{
  "default_model": "copilot-claude",
  "providers": {
    "copilot": {
      "provider": "mutbot.copilot.provider.CopilotProvider",
      "github_token": "ghu_xxx",
      "models": {
        "copilot-claude": "claude-sonnet-4",
        "copilot-gpt": "gpt-4.1"
      }
    },
    "anthropic": {
      "provider": "AnthropicProvider",
      "base_url": "https://api.anthropic.com",
      "auth_token": "$ANTHROPIC_API_KEY",
      "models": {
        "anthropic-claude": "claude-sonnet-4"
      }
    }
  }
}
```

**可混合使用**：不同 provider 可各自选择 list 或 dict 形式。

### 2.2 模型解析逻辑

`Config.get_model(name)` 按 provider 在配置中的顺序逐个搜索：

1. 遍历 `providers` dict（Python 3.7+ dict 保持插入序）
2. 对每个 provider 的 `models`：
   - **list 形式**：`name` 匹配列表中的 model_id → 命中，`model_id = name`
   - **dict 形式**：`name` 匹配 key（别名）→ 命中，`model_id = models[name]`（**不匹配 value**）
3. 首个命中的 provider 胜出
4. 返回合并后的 flat dict：provider 级字段（去掉 `models`）+ 解析出的 `model_id`

**返回值格式**（保持与现有 `_create_llm_client` 兼容）：
```python
{
    "provider": "AnthropicProvider",
    "base_url": "https://api.anthropic.com",
    "auth_token": "$ANTHROPIC_API_KEY",
    "model_id": "claude-sonnet-4",
}
```

**解析示例**：

| 请求 name | Provider | models 形式 | 匹配？ | 返回 model_id |
|-----------|----------|------------|--------|--------------|
| `"claude-sonnet-4"` | copilot | `["claude-sonnet-4", "gpt-4.1"]` | list 匹配 | `claude-sonnet-4` |
| `"copilot-claude"` | copilot | `{"copilot-claude": "claude-sonnet-4"}` | dict key 匹配 | `claude-sonnet-4` |
| `"claude-sonnet-4"` | copilot | `{"copilot-claude": "claude-sonnet-4"}` | **不匹配**（dict 不按 value 搜索） | — |

**`default_model` 使用相同的解析逻辑**：list 形式写 model_id，dict 形式写别名。

### 2.3 `Config.get_all_models()` 新方法

新增方法返回所有可用模型的完整信息，供 `/llm/v1/models` 和 `/llm` 信息页使用：

```python
def get_all_models(self) -> list[dict]:
    """列出所有已配置的模型。

    Returns:
        每个模型一个 dict，包含：
        - name: 模型显示名（list 形式 = model_id，dict 形式 = alias key）
        - model_id: 实际发送给 API 的 model_id
        - provider: provider 类路径
        - provider_name: provider 配置 key（如 "copilot"、"anthropic"）
    """
```

### 2.4 配置向导改造

#### 2.4.1 动态模型发现

对支持 `/v1/models` 端点的 provider（OpenAI、其他 OpenAI 兼容），向导在认证后调用该端点列出可用模型，让用户多选启用：

```
Select a provider:
  [1] GitHub Copilot (free with GitHub account)
  [2] Anthropic (Claude)
  [3] OpenAI
  [4] Other OpenAI-compatible API

> 3

Detected OPENAI_API_KEY from environment.
API key: sk-proj-...***abc (from $OPENAI_API_KEY)
Use this key? [Y/n] y

Fetching available models...

Available models:
  [1] gpt-4.1
  [2] gpt-4.1-mini
  [3] gpt-4.1-nano
  [4] o3
  [5] o4-mini
  Also available: gpt-4o, gpt-4o-mini, ...

Select models (numbers or names, comma-separated):
> 1,2

Config written to ~/.mutbot/config.json
  Provider: openai
  Models: gpt-4.1, gpt-4.1-mini
  Default: gpt-4.1
```

**展示规则**：
- 编号列表最多显示 **10 个**模型（按推荐度排序）
- 超出 10 个的模型在 `Also available: ...` 行列出（过多时加省略号）
- 用户输入支持**编号**和**模型名称**混合，逗号分隔

#### 2.4.2 各 provider 的模型发现能力

| Provider | 模型发现方式 | 说明 |
|----------|------------|------|
| GitHub Copilot | 硬编码列表 | Copilot API 不提供 models 端点，使用已知模型列表 |
| Anthropic | 硬编码列表 | Anthropic API 无公开 models 端点 |
| OpenAI | `GET /v1/models` API | 动态查询，按前缀过滤 chat 模型（`gpt-*`、`o1*`、`o3*`、`o4*`、`chatgpt-*`） |
| Other | `GET {base_url}/models` 尝试 | 成功则列出，失败则让用户手动输入 model_id |

#### 2.4.3 写入新格式

向导生成 `providers` 格式配置（list 形式）：

```json
{
  "default_model": "gpt-4.1",
  "providers": {
    "openai": {
      "provider": "OpenAIProvider",
      "base_url": "https://api.openai.com/v1",
      "auth_token": "$OPENAI_API_KEY",
      "models": ["gpt-4.1", "gpt-4.1-mini"]
    }
  }
}
```

`_write_config()` 合并逻辑：已有 `providers` 保留，新 provider 追加/覆盖同名 provider。

### 2.5 `/llm` 信息页

在 `/llm` 路径返回 HTML 页面（服务端渲染，`HTMLResponse`），包含：

1. **API 端点文档**：
   - `POST /llm/v1/messages` — Anthropic Messages 格式
   - `POST /llm/v1/chat/completions` — OpenAI Chat Completions 格式
   - `GET /llm/v1/models` — 列出可用模型

2. **配置说明**：
   - 配置文件位置（`~/.mutbot/config.json`）
   - `providers` 格式说明和 list / dict 两种示例
   - 环境变量引用（`$VAR`）说明

3. **当前已配置模型列表**（动态）：
   - 每个模型的名称、model_id、所属 provider

不需要认证，`/llm` 前缀保持在 auth skip list 中。

### 2.6 Proxy routes 适配

`create_llm_router()` 和 `_find_model_config()` 改为基于新的 provider 配置格式：
- `create_llm_router()` 接收包含 `providers` 的 config dict
- `list_models` 端点使用 `get_all_models()` 等效逻辑
- `_find_model_config()` 使用与 `Config.get_model()` 相同的 provider 顺序搜索逻辑
- `_get_backend_info()` 从 provider 级配置获取 base_url、headers

## 4. 实施步骤清单

### 阶段一：mutagent Config 核心改造 [✅ 已完成]

- [x] **Task 1.1**: `Config.get_model()` 重写为 provider-based 解析
  - [x] 遍历 `providers` dict，按序搜索
  - [x] list 形式：匹配 model_id
  - [x] dict 形式：仅匹配 key（别名），不匹配 value
  - [x] 返回合并后的 flat dict（provider 字段 + model_id）
  - 状态：✅ 已完成

- [x] **Task 1.2**: 新增 `Config.get_all_models()` 方法
  - [x] Declaration 桩方法 + config_impl 实现
  - [x] 遍历所有 provider，展开所有模型
  - [x] 返回 `list[dict]`（name, model_id, provider, provider_name）
  - 状态：✅ 已完成

- [x] **Task 1.3**: `_resolve_default_model()` 适配新格式
  - [x] 从 `providers` 中取第一个 provider 的第一个 model
  - [x] list 形式取首个 model_id，dict 形式取首个 key
  - 状态：✅ 已完成

### 阶段二：配置向导改造 [✅ 已完成]

- [x] **Task 2.1**: 向导输出改为 `providers` 格式
  - [x] Copilot 路径：生成 `providers.copilot`
  - [x] Anthropic 路径：生成 `providers.anthropic`
  - [x] OpenAI 路径：生成 `providers.openai`
  - [x] Other 路径：生成 `providers.custom`
  - 状态：✅ 已完成

- [x] **Task 2.2**: OpenAI 动态模型发现
  - [x] 调用 `GET /v1/models` 获取模型列表
  - [x] 按前缀过滤 chat 类模型（`gpt-*`、`o1*`、`o3*`、`o4*`、`chatgpt-*`）
  - [x] 编号列表最多 10 个，超出部分 `Also available` 行展示
  - [x] 支持编号和模型名称混合输入
  - [x] 失败时 fallback 到硬编码列表
  - 状态：✅ 已完成

- [x] **Task 2.3**: Other provider 动态模型发现
  - [x] 尝试 `GET {base_url}/models`
  - [x] 成功则列出并让用户选择（同 2.2 交互方式）
  - [x] 失败则让用户手动输入 model_id
  - 状态：✅ 已完成

- [x] **Task 2.4**: `_write_config()` 适配 providers 格式
  - [x] 合并逻辑：已有 providers 保留，同名 provider 覆盖
  - [x] `default_model` 仅在未设置时写入
  - 状态：✅ 已完成

### 阶段三：Proxy routes 适配 [✅ 已完成]

- [x] **Task 3.1**: `create_llm_router()` 和 `_find_model_config()` 适配
  - [x] 接收 providers 格式 config
  - [x] provider 顺序搜索（与 get_model 逻辑一致）
  - 状态：✅ 已完成

- [x] **Task 3.2**: `list_models` 端点适配
  - [x] 展开所有 provider 的模型列表
  - 状态：✅ 已完成

- [x] **Task 3.3**: `_get_backend_info()` 适配
  - [x] 从 provider 级配置获取 base_url、headers
  - 状态：✅ 已完成

### 阶段四：`/llm` 信息页 [✅ 已完成]

- [x] **Task 4.1**: 实现 `/llm` HTML 信息页路由
  - [x] API 端点文档（Messages / Chat Completions / Models）
  - [x] 配置文件格式说明（providers + list/dict 示例）
  - [x] 当前已配置模型列表（动态渲染）
  - [x] 服务端渲染 HTML，HTMLResponse 返回
  - 状态：✅ 已完成

### 阶段五：mutbot 集成适配 [✅ 已完成]

- [x] **Task 5.1**: `session_impl._load_config()` 适配
  - [x] 返回 providers 格式给 proxy
  - 状态：✅ 已完成

- [x] **Task 5.2**: `create_llm_client()` / `build_default_agent()` 验证
  - [x] 确认 `Config.get_model()` 返回的 flat dict 格式不变
  - [x] 下游 `_create_llm_client()` 无需改动
  - 状态：✅ 已完成

### 阶段六：测试 [✅ 已完成]

- [x] **Task 6.1**: `get_model()` provider-based 解析测试
  - [x] list 形式：直接匹配 model_id
  - [x] dict 形式：仅 key 匹配，value 不匹配
  - [x] provider 顺序优先级（同名模型，先配置的 provider 胜出）
  - [x] 模型不存在时 SystemExit
  - [x] 无 providers 时 SystemExit
  - 状态：✅ 已完成

- [x] **Task 6.2**: `get_all_models()` 测试
  - [x] 多 provider 展开
  - [x] list 和 dict 混合
  - [x] 空配置返回空列表
  - 状态：✅ 已完成

- [x] **Task 6.3**: `_resolve_default_model()` 测试
  - [x] 有 default_model 配置时直接使用
  - [x] 无 default_model 时取首个 provider 的首个模型（list 和 dict）
  - 状态：✅ 已完成

- [x] **Task 6.4**: 配置向导输出格式测试
  - [x] providers 格式正确性
  - [x] 合并逻辑（新 provider 追加，同名覆盖）
  - 状态：✅ 已完成

- [x] **Task 6.5**: 已有 e2e 测试适配
  - [x] test_e2e.py 更新为 providers 格式
  - 状态：✅ 已完成

---

### 实施进度总结
- ✅ **阶段一：mutagent Config 核心改造** - 100% 完成 (3/3任务)
- ✅ **阶段二：配置向导改造** - 100% 完成 (4/4任务)
- ✅ **阶段三：Proxy routes 适配** - 100% 完成 (3/3任务)
- ✅ **阶段四：/llm 信息页** - 100% 完成 (1/1任务)
- ✅ **阶段五：mutbot 集成适配** - 100% 完成 (2/2任务)
- ✅ **阶段六：测试** - 100% 完成 (5/5任务)

**核心功能完成度：100%** (18/18任务)
**单元测试覆盖：mutagent 705 通过 + mutbot 250 通过 = 955 全部通过**

## 5. 测试验证

### 单元测试
- [x] Config.get_model() provider-based 解析 (9 tests)
- [x] Config.get_all_models() (4 tests)
- [x] _resolve_default_model() 新格式（含在 get_model 测试中）
- [x] 配置向导 providers 输出 (3 tests)
- [x] Provider auth_token 校验 (4 tests)
- 执行结果：mutagent 705/705 通过，mutbot 250/250 通过

### 集成测试
- [ ] 端到端：向导生成配置 → get_model() 解析 → proxy 代理调用（需手动验证）
- [ ] /llm 信息页内容正确性（需启动服务器验证）
- 执行结果：待手动验证
