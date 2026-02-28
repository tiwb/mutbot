# 聊天界面增强 设计规范

**状态**：✅ 已完成
**日期**：2026-02-28
**类型**：功能设计

## 1. 背景

当前 mutbot 聊天界面存在以下体验问题：

1. **Context 显示不完整**：有百分比时只显示百分比（如 `90%`），没百分比时才显示 token 大小（如 `45K`）。希望始终显示大小，有百分比时附加显示。
2. **缺少时间信息**：消息没有时间戳，用户无法得知消息发送时间和 agent 工作耗时。
3. **缺少发送者标识**：消息仅靠左右对齐和背景色区分角色，没有头像和名称，不符合主流聊天软件体验。
4. **多条用户消息的 turn 归属**：用户可在 agent 忙碌时连续发送多条消息，当前每条消息被 agent 作为独立 turn 处理，需要在显示层面将同一工作周期内的用户消息归为一组。

## 2. 设计方案

### 2.1 Context 显示优化

**当前行为**（`AgentPanel.tsx` TokenUsageDisplay）：
- `contextPercent != null` → 显示 `90%`
- `contextPercent == null` → 显示 `45K`

**目标行为**：
- 始终显示 token 大小
- 有百分比时附加显示百分比，百分比带颜色

示例：
- 无百分比：`Context: 45.2K`
- 有百分比：`Context: 45.2K (90%)`（90% 带红色）

**修改范围**：仅修改 `TokenUsageDisplay` 组件渲染逻辑。

### 2.2 消息时间戳与工作耗时

#### 2.2.1 整体方案

- **后端计算**：时间戳和耗时全部由服务端记录，前端只负责显示
- **持久化**：时间元数据保存在 session 持久化数据中，历史恢复时也能显示
- **显示位置**：在聊天气泡外侧（元数据区域），不嵌入聊天内容

#### 2.2.2 后端：时间元数据

在 `AgentBridge` 中跟踪每轮对话的时间：

1. `send_message()` 被调用时记录 `turn_start_time`（仅首条，忙碌时追加消息不重置）
2. 广播 `user_message` 事件时附带 `timestamp`（ISO 格式，本地时区）
3. `turn_done` 事件触发时计算 `duration_seconds`，始终携带
4. 广播增强的 `turn_done` 事件：`{ type: "turn_done", timestamp, duration_seconds, turn_id, model }`

持久化方案：在 session JSON 中新增 `turn_timestamps` 数组，与 LLM `messages` 数组分离：

```json
{
  "messages": [...],
  "turn_timestamps": [
    {
      "user_timestamps": ["2026-02-28T14:30:00+08:00", "2026-02-28T14:30:05+08:00"],
      "agent_timestamp": "2026-02-28T14:30:35+08:00",
      "duration_seconds": 35,
      "model": "claude-sonnet-4-6"
    }
  ]
}
```

- `user_timestamps`：数组，支持一个 turn 中多条用户消息各自的发送时间
- `model`：该轮对话使用的模型名称
- 每个条目对应一轮对话（一或多条用户消息 → agent 完成响应）
- `session.messages` RPC 响应中同时返回 `turn_timestamps`

#### 2.2.3 前端：ChatMessage 扩展

为 `ChatMessage` 类型新增可选的时间元数据字段：

```typescript
type ChatMessage =
  | { id: string; role: "user"; type: "text"; content: string; timestamp?: string; turnId?: string }
  | { id: string; role: "assistant"; type: "text"; content: string; timestamp?: string; durationSeconds?: number; model?: string }
  | { id: string; role: "assistant"; type: "tool_group"; data: ToolGroupData }
  | { id: string; role: "assistant"; type: "error"; content: string };
```

- **user 消息**：每条都有 `timestamp`，收到 `user_message` 事件或恢复历史时填充
- **assistant text 消息**：在 `turn_done` 事件时，回填最后一条 assistant text 消息的 `timestamp`、`durationSeconds`、`model`

#### 2.2.4 前端：时间显示规则

**响应式布局**：meta 信息与气泡同行或换行，取决于气泡宽度。

气泡和 meta 放在同一个 `flex-wrap: wrap` 容器中：
- **气泡内容短**时，meta 与气泡同行，底部对齐（`align-items: flex-end`）
- **气泡内容长**时，meta 自动换行到气泡下方，信息始终为一行

```
短消息（同行，底部对齐）：
  ┌──────────┐
  │◁ Hi there│  ✻ 35s · 14:30
  └──────────┘

长消息（换行到下方，一行显示）：
  ┌──────────────────────────────────────┐
  │◁ This is a very long message that    │
  │   takes up a lot of horizontal space  │
  └──────────────────────────────────────┘
                              ✻ 35s · 14:30
```

**meta 内容格式**（始终一行）：

| 角色 | 条件 | 格式 |
|------|------|------|
| User | 当天 | `14:30` |
| User | 非当天（同年） | `2/28 14:30` |
| User | 去年或更早 | `2025/12/31 14:30` |
| Agent | 耗时 < 10s | `14:30`（仅时间，不显示耗时） |
| Agent | 耗时 ≥ 10s，当天 | `✻ 35s · 14:30` |
| Agent | 耗时 ≥ 10s，非当天 | `✻ 35s · 2/28 14:30` |
| Agent | 耗时 ≥ 60s | `✻ 1m 23s · 14:30` |

### 2.3 头像与名称显示

#### 2.3.1 布局结构

采用主流聊天软件的两栏布局：

```
┌──────────────────────────────────────────────────┐
│  [Avatar]  claude-sonnet-4-6                     │
│            ┌─────────────────────┐               │
│            │◁ Message content    │  ✻ 35s · 14:30│
│            └─────────────────────┘               │
│                                                  │
│                              ┌───────────┐ [Ava] │
│                    14:31     │ Content ▷ │       │
│                              └───────────┘       │
└──────────────────────────────────────────────────┘
```

- **Assistant 消息**（左侧）：`[头像] [名称 + 气泡]`，气泡左侧有三角箭头指向头像
- **User 消息**（右侧）：`[气泡] [头像]`，气泡右侧有三角箭头指向头像，**不显示名称**
- 头像列固定宽度，名称 + 气泡列自适应

#### 2.3.2 头像样式

- **形状**：圆形遮罩（`border-radius: 50%`），尺寸 32px
- **有图片时**：显示图片（`<img>` 填充圆形）
- **无图片时**：带颜色的边框 + 名称首字母缩写
  - 边框颜色：基于名称哈希生成，确保每个 agent 类型有稳定的颜色
  - 字母缩写：取名称首字母
  - 背景：透明
  - 字体：与界面一致，居中显示

#### 2.3.3 数据来源

**Agent 名称**：
- 显示该轮对话使用的**模型名称**（如 `claude-sonnet-4-6`）
- 每轮 turn 的 model 由后端记录在 `turn_timestamps` 中，并通过 `turn_done` 事件传递
- 前端在 `turn_done` 时回填到对应的 assistant text 消息
- 无 model 信息时回退到 session 的 `display_name`（如 "Guide"）

**Agent 头像**：`session.config.avatar`（可选），无则用 model 名首字母缩写

**用户信息**：
- 系统当前无用户身份系统
- 默认显示字母缩写 "U"（User），不显示名称
- 后续可扩展为从配置中读取用户名和头像

**数据传递**：
- `session.messages` RPC 响应中新增 `agent_display: { name, avatar? }` 字段
- `turn_done` 事件中携带 `model` 字段
- 前端优先使用 per-message `model` 作为名称，回退到 `agentDisplay.name`

#### 2.3.4 气泡箭头

使用 CSS 伪元素绘制三角形箭头：
- Assistant 气泡：左侧箭头（`::before` 边框色 + `::after` 背景色），颜色匹配气泡
- User 气泡：右侧箭头（`::after`），颜色匹配气泡背景

#### 2.3.5 连续消息合并

同角色连续消息仅首条显示头像和名称，后续消息保留头像列缩进但不显示头像，消息间距缩小。

### 2.4 多条用户消息的 Turn 归组

#### 2.4.1 现状

用户可在 agent 忙碌时连续发送消息：
- 消息通过 `asyncio.Queue` FIFO 排队
- Agent 在工具执行 checkpoint 处通过 `check_pending()` 检测新消息
- 检测到后提前结束当前 turn，处理下一条消息
- 每条用户消息各自触发一次 `turn_done`

#### 2.4.2 Turn 归组方案

**后端**：引入 `turn_id` 概念

- `AgentBridge` 维护 `_current_turn_id: str`、`_agent_status: str`
- 第一条用户消息（agent idle 时收到）生成新 `turn_id`
- 后续消息（agent 忙碌时收到）复用当前 `turn_id`
- `user_message` 广播事件中附带 `turn_id`
- 仅当 `input_queue` 为空时（turn 所有用户消息都处理完毕），才记录到 `turn_timestamps`

**持久化**：`turn_timestamps` 中每个条目的 `user_timestamps` 为数组，对应该 turn 内的多条用户消息。

**前端**：
- 同一 `turn_id` 的用户消息在视觉上归为一组（共享头像列，消息间距缩小）
- 每条用户消息各自独立显示内容和时间戳
- turn 内最后一条用户消息后面跟 agent 响应

## 3. 待定问题

（已全部确认）
- 头像尺寸：32px
- 连续同角色消息：仅首条显示头像和名称，后续保留缩进
- 多消息归组头像：同上规则

## 4. 实施步骤清单

### 阶段一：Context 显示优化 [✅ 已完成]
- [x] **Task 1.1**: 修改 TokenUsageDisplay 显示逻辑
  - [x] 修改渲染：始终显示 `formatTokenCount(usage.contextUsed)`
  - [x] 有百分比时追加 `(XX%)`，百分比部分应用颜色
  - 状态：✅ 已完成

### 阶段二：消息布局重构 — 头像与名称 [✅ 已完成]
- [x] **Task 2.1**: 后端提供 agent 显示信息
  - [x] `session.messages` RPC 响应新增 `agent_display: { name, avatar? }`
  - [x] 从 Session 类的 `display_name` 和 `config.avatar` 获取
  - 状态：✅ 已完成

- [x] **Task 2.2**: 前端 ChatMessage 布局重构
  - [x] 消息行改为两栏 flex 布局：`[avatar-col] [content-col]`
  - [x] User 消息反向：`[content-col] [avatar-col]`
  - [x] 连续同角色消息的合并规则：仅首条显示头像和名称
  - 状态：✅ 已完成

- [x] **Task 2.3**: Avatar 组件
  - [x] 创建 `Avatar` 组件：圆形遮罩，支持 image / initials 两种模式
  - [x] Initials 模式：基于名称哈希的边框颜色 + 首字母
  - [x] Image 模式：`<img>` + 圆形 `overflow: hidden`
  - 状态：✅ 已完成

- [x] **Task 2.4**: 气泡箭头 CSS
  - [x] Assistant 气泡左侧三角箭头（`::before` + `::after` 伪元素）
  - [x] User 气泡右侧三角箭头
  - [x] 箭头颜色匹配气泡背景/边框
  - 状态：✅ 已完成

### 阶段三：后端时间元数据与 Turn 归组 [✅ 已完成]
- [x] **Task 3.1**: AgentBridge 记录时间与 turn_id
  - [x] 新增字段：`_turn_start_time`、`_current_turn_id`、`_turn_user_timestamps`、`_agent_status`
  - [x] `send_message()` 中：idle 时创建新 turn_id 并记录 start_time，busy 时复用
  - [x] 每条 `user_message` 广播附带 `timestamp` 和 `turn_id`
  - [x] `turn_done` 时始终携带 `timestamp`、`duration_seconds`、`turn_id`、`model`
  - [x] 仅当 input_queue 为空时（turn 真正结束）才计入 turn_timestamps
  - 状态：✅ 已完成

- [x] **Task 3.2**: 持久化 turn_timestamps
  - [x] AgentBridge 维护 `turn_timestamps: list[dict]`
  - [x] turn 完成时追加条目（含 `user_timestamps` 数组和 `model`）
  - [x] 初始化时从 session 元数据恢复
  - [x] `_persist()` 中保存 turn_timestamps 到 session JSON
  - 状态：✅ 已完成

- [x] **Task 3.3**: session.messages RPC 返回 turn_timestamps
  - [x] 响应中新增 `turn_timestamps` 和 `agent_display` 字段
  - 状态：✅ 已完成

### 阶段四：前端时间显示 [✅ 已完成]
- [x] **Task 4.1**: ChatMessage 类型扩展
  - [x] user text 消息新增 `timestamp?: string`、`turnId?: string`
  - [x] assistant text 消息新增 `timestamp?: string`、`durationSeconds?: number`、`model?: string`
  - 状态：✅ 已完成

- [x] **Task 4.2**: AgentPanel 事件处理更新
  - [x] `user_message` 事件：提取 `timestamp`、`turn_id` 写入 ChatMessage
  - [x] `turn_done` 事件：提取 `timestamp`、`duration_seconds`、`model`，回填最后一条 assistant text 消息
  - [x] 历史恢复：从 `turn_timestamps` 匹配用户/助手消息的时间元数据（含 model）
  - [x] Agent 名称优先使用 per-message model，回退到 agentDisplay.name
  - 状态：✅ 已完成

- [x] **Task 4.3**: MessageList 渲染时间元数据（响应式布局）
  - [x] 实现 `formatMessageTime()` 函数（智能日期格式）
  - [x] 实现 `formatDuration()` 函数（≥ 60s 转 `1m 23s`）
  - [x] 气泡 + meta 放在 `flex-wrap: wrap; align-items: flex-end` 容器中
  - [x] meta 始终一行：user → 时间，agent → `✻ Xs · 时间`（< 10s 仅时间）
  - [x] 短消息时 meta 与气泡同行，长消息时自动换行到下方
  - [x] 添加 `.message-meta` CSS 样式
  - 状态：✅ 已完成

## 5. 测试验证

### 手动测试
- [x] Context 显示：无百分比时仅显示 token 数
- [x] Context 显示：有百分比时同时显示 token 数和百分比
- [x] 头像显示：Agent 消息左侧显示圆形头像 + 模型名称
- [x] 头像显示：User 消息右侧显示圆形头像，无名称
- [x] 头像显示：无图片时显示字母缩写 + 彩色边框
- [x] 气泡箭头：正确指向对应头像方向
- [x] 连续消息合并：同角色连续消息仅首条显示头像和名称
- [x] 用户消息：每条都显示时间
- [x] 用户消息：当天消息只显示时间
- [x] Agent 消息：turn 完成后始终显示完成时间
- [x] Agent 消息：< 10s 仅显示时间，≥ 10s 显示 `✻ 35s · 14:30` 格式
- [x] 响应式布局：短消息时 meta 与气泡同行（底部对齐）
- [x] 响应式布局：长消息时 meta 换行到气泡下方
- [x] 多条用户消息：agent 忙碌时连续发送多条，同 turn 归组显示
- [x] 历史恢复：重新连接后头像、时间元数据正确显示
