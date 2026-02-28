# 合并 Agent/Guide/Researcher 三种 Session 类型 设计规范

**状态**：✅ 已完成
**日期**：2026-02-28
**类型**：重构

## 1. 背景

当前 mutbot 有三种可对话的 AgentSession 子类：

| 类型 | 职责 | 工具 |
|------|------|------|
| **GuideSession** | 向导入口，识别需求后委托给专业 Session | SessionToolkit（创建其他 Session） |
| **ResearcherSession** | 研究员，Web 搜索与信息分析 | WebToolkit（search / fetch） |
| **AgentSession**（基类默认） | 代码检视与修改 | ModuleToolkit + LogToolkit + auto_discover |

问题：
- Guide 自身无法回答需要搜索的问题，必须创建 Researcher Session 来间接处理
- 用户体验割裂：简单的搜索问题也需要切换 Session
- Agent Session 的委托机制增加了不必要的间接层

## 2. 设计方案

### 2.1 核心思路

以 GuideSession 为主体，吸收 Researcher 的 Web 搜索能力，保留创建其他 Session 的能力：

- **保留** GuideSession 作为默认会话入口
- **保留** SessionToolkit（未来扩展专业 Session 类型）
- **删除** ResearcherSession（`mutbot/builtins/researcher.py`）
- **给 GuideSession 加上** WebToolkit（search / fetch）
- **更新** Guide system_prompt：移除 Researcher 委托描述，增加 Web 搜索能力描述

### 2.2 GuideSession 变更

**新 system_prompt**：
```
你是 MutBot 助手，帮助用户了解和使用 MutBot 的各项功能。

核心能力：
- 友好地介绍 MutBot 的功能和使用方式
- 回答用户的各类问题，提供信息和建议
- 通过 Web 搜索获取最新信息来回答问题
- 搜索前先思考合适的关键词和搜索策略
- 对搜索结果进行交叉验证，注意信息的时效性
- 给出结论时标注信息来源
```

**create_agent() 变更**：
- 保留 SessionToolkit
- 新增 WebToolkit
- 保留 SetupProvider 兜底逻辑

### 2.3 文件变更清单

| 文件 | 操作 |
|------|------|
| `mutbot/builtins/guide.py` | 更新 system_prompt + create_agent()，新增 WebToolkit |
| `mutbot/builtins/researcher.py` | **删除** |
| `mutbot/builtins/__init__.py` | 移除 researcher import |
| `mutbot/session.py` | **不变** |
| `mutbot/toolkits/session_toolkit.py` | **不变** |

## 3. 已确认决策

- **SessionToolkit 保留**：向导需要创建其他 Session 的能力，未来会扩展专业 Session 类型
- **前端不动**：动态发现机制，删除 Researcher 后自动不再出现

### 2.4 AgentSession 基类默认 Agent 简化

`build_default_agent()`（`session_impl.py`）同步简化：

- **移除** ModuleToolkit、LogToolkit 的显式注册，改为仅依赖 `ToolSet(auto_discover=True)` 自动发现
- **简化** 默认 system_prompt：`"You are a Python AI Agent. Use the available tools to help the user with their tasks."`

### 2.5 文件变更清单（最终）

| 文件 | 操作 |
|------|------|
| `mutbot/builtins/guide.py` | 更新 system_prompt + create_agent()，新增 WebToolkit |
| `mutbot/builtins/researcher.py` | **删除** |
| `mutbot/builtins/__init__.py` | 移除 researcher import |
| `mutbot/runtime/session_impl.py` | `build_default_agent()` 移除显式 Toolkit 注册，简化 system_prompt |
| `mutbot/toolkits/session_toolkit.py` | docstring 清理 |
| `mutbot/session.py` | **不变** |

## 4. 实施步骤清单

### 阶段一：合并 Session [✅ 已完成]
- [x] **Task 1.1**: 改写 GuideSession
  - [x] 更新 system_prompt（移除委托描述，增加 Web 搜索能力）
  - [x] create_agent() 中新增 WebToolkit
  - [x] 更新模块 docstring
  - 状态：✅ 已完成

- [x] **Task 1.2**: 删除 ResearcherSession
  - [x] 删除 `mutbot/builtins/researcher.py`
  - [x] 从 `mutbot/builtins/__init__.py` 移除 import
  - 状态：✅ 已完成

- [x] **Task 1.3**: 简化 AgentSession 基类默认 Agent
  - [x] `build_default_agent()` 移除 ModuleToolkit / LogToolkit 显式注册，改用 auto_discover
  - [x] 简化默认 system_prompt
  - 状态：✅ 已完成

### 阶段二：测试验证 [✅ 已完成]
- [x] **Task 2.1**: 更新相关测试
  - [x] 检查引用 ResearcherSession / SessionToolkit 的测试 — 无引用，无需修改
  - [x] 清理 session_toolkit.py docstring 中的 Researcher 示例
  - 状态：✅ 已完成

- [x] **Task 2.2**: 运行全量测试
  - [x] `pytest` 346 passed（11 failed 均为已有问题，与改动无关）
  - 状态：✅ 已完成

## 5. 测试验证

### 单元测试
- [x] GuideSession 创建 Agent 时包含 WebToolkit
- [x] GuideSession setup 模式仍可用
- [x] 无 ResearcherSession / SessionToolkit 引用残留

### 集成测试
- [x] 启动 mutbot 正常
- [x] 创建 Guide Session 正常对话
