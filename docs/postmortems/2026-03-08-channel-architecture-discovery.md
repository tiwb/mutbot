# 从"文件太大"到发现架构缺失 — Channel 设计对话记录

**日期**：2026-03-08
**类型**：架构设计对话复盘

## 前提

`mutbot/src/mutbot/web/routes.py` 膨胀到 1,618 行，包含 34 个 RPC handler、3 个 WebSocket 端点、channel 消息处理、终端生命周期管理等所有逻辑。用户提出"routes.py 太大了，看看能否切分"。

## 对话过程

### 第一步：AI 提出按领域拆分文件

AI 分析完 routes.py 的结构后，提出按 RPC 领域拆分为 5 个模块的方案：

```
routes.py          → 瘦协调层：Router、WebSocket 端点、全局状态
rpc_app.py         → App 级 RPC
rpc_session.py     → Session RPC
rpc_workspace.py   → Workspace 级杂项 RPC
rpc_channel.py     → Channel 消息处理 + 终端生命周期
```

这是一个常规的"按功能域分文件"方案，直觉上合理。

### 第二步：用户发现 rpc_channel 混合了不同层次

用户看到 `rpc_channel.py` 将 channel 基础设施和 terminal 业务逻辑放在一起，指出这不合理。用户提出应该从**架构基础设施**和**具体功能**两个维度来区分，而不是简单地按"跟 channel 相关的都放一起"。

> "我觉得整体应该从架构基础设施，和具体功能来区分"

这个反馈迫使 AI 重新审视 `rpc_channel.py` 中的内容，发现它实际上混合了三个层次：

1. Channel 基础设施（消息路由、channel RPC）
2. Terminal 业务逻辑（attach/detach、scrollback replay）
3. Agent 业务逻辑（消息转发、bridge 管理）

### 第三步：AI 提出 ChannelHandler + Session 类型匹配

AI 识别出 `_handle_channel_json` 中的 `isinstance` switch 是耦合的根源，提出引入 `ChannelHandler` 抽象，在 `channel.open` 时根据 Session 类型绑定对应的 handler。

讨论了两种注册方向：
- Session 声明自己的 handler（`channel_handler = "mutbot.web.xxx"`）
- ChannelHandler 声明自己处理哪种 Session

用户明确要求"不可接受 isinstance switch"，因为"需求源头本来就在具体的 Session 上，架构应该能处理这个问题"。

### 第四步：AI 提出反向注册 — ChannelHandler 按 Session 类型匹配

AI 调整为 ChannelHandler 作为 Declaration 子类，声明 `session_type`，通过 `discover_subclasses` 自动发现，按 MRO 匹配最具体的 handler。

这个方案消除了 isinstance switch，符合 mutobj 的子类发现模式。但用户提出了关键问题：

### 第五步：用户追问——一个 Session 多个 Channel 怎么办？

> "ChannelHandler 跟 Session 类型匹配的话，如果一个 Session 想用多个 Channel 怎么处理？"

这个问题揭示了按 Session 类型匹配的方案仍然是 1:1 绑定思维。用户进一步指出：

> "Channel 和 Workspace 可能都是关键的 mutbot 概念，mutbot 定义了 Session 跟前端的通信方式：通过 channel。"

### 第六步：AI 提出 Channel 有类型（target）

AI 调整方案：Channel 本身有类型，ChannelHandler 按 `target`（如 "terminal"、"chat"、"log"）注册，前端 open channel 时指定 target。

```
channel.open { target: "terminal", session_id: "..." }
channel.open { target: "chat", session_id: "..." }
```

这样一个 Session 可以有多个不同类型的 Channel。看似解决了问题。

### 第七步：用户的关键洞察——channel.open 不应该暴露给前端

用户指出了设计的根本问题：

> "连接 channel 是前端发起的吗？我似乎理解问题的根源了。session 应该要有 channel，因为没有，session 就无法跟前端通信，这个绑定关系是自然存在的。我们把 session 的通信功能和 channel 这个基础设施混在一起了。"

> "前端要的不是通用的 channel.open，前端要的是具体的 api。比如 session.connect。"

> "你只有连接了 session 以后，才能知道 session 有哪些功能。"

这段话一次性解决了多个问题：
- **channel.open 是基础设施泄漏**：前端不应该直接操作 channel
- **Session 和 Channel 的关系**：Session 使用 Channel 通信，Channel 是 Session 的基础设施，不是独立的前端 API
- **API 层次**：前端调用 `session.connect`，channel 分配是后端内部实现

### 第八步：确立 Session 定义通信行为

基于上述洞察，设计自然落位：

```python
class Session:
    def on_connect(self, channel, ctx): ...
    async def on_message(self, channel, raw): ...
    async def on_data(self, channel, payload): ...
    def on_disconnect(self, channel): ...
```

- TerminalSession 在 `on_connect` 中 attach PTY、replay scrollback
- AgentSession 在 `on_message` 中处理 message/cancel/run_tool
- Channel 退回纯基础设施

### 第九步：Channel 是 mutbot 核心层概念

用户最后确认：

> "Channel 是不是应该是在 mutbot 核心层抽象的一个概念，它定义了 Session 的通信基础设施，看上去 Session 无法不知道这个概念"

最终架构层次：

```
mutbot/
  channel.py          # Channel 抽象（Declaration）— mutbot 核心概念
  session.py          # Session 子类声明 on_connect / on_message 等
  web/
    transport.py      # Channel 的 WebSocket 实现（多路复用、帧格式）
```

依赖方向：
```
mutbot.session → mutbot.channel（抽象）
mutbot.web.transport → mutbot.channel（实现）
```

## 演进路径总结

```
"文件太大，拆一下"
  → 按领域分文件（常规方案）
    → 发现 channel + terminal 混合不合理
      → 引入 ChannelHandler 按 Session 类型匹配
        → 发现 1:1 匹配限制扩展性
          → 改为 Channel 有类型（target）
            → 发现 channel.open 是基础设施泄漏
              → Session 定义通信行为，Channel 是内部基础设施
                → Channel 是 mutbot 核心层抽象
```

一个看似简单的文件拆分任务，通过逐步追问，最终揭示了一个缺失的架构抽象。

## 值得学习的经验

### 1. 拆文件不等于拆架构

最初的方案是"把 1,618 行按功能域分到 5 个文件"。这能降低单文件行数，但没有解决根本问题——职责混杂的原因不是文件不够多，而是缺少正确的抽象层次。如果只是机械地分文件，混乱只是从一个文件分散到多个文件。

### 2. 发现问题的信号：不同层次的东西被放在一起

用户第一次介入就指出了"channel 和 terminal 放一起不合理"。判断标准很简单：**基础设施和业务逻辑不应该在同一层**。当你看到一个模块里既有"消息怎么路由"又有"scrollback 怎么回放"，这就是抽象层次混乱的信号。

### 3. 从"谁知道谁"反推架构

每次方案调整的驱动力都是"依赖方向是否正确"：
- Session 不应该知道 web 层 → handler 路径不能写在 Session 上
- Channel 不应该知道 Session 类型 → isinstance switch 不可接受
- 前端不应该知道 channel → channel.open 不应该暴露

"谁不应该知道谁"比"谁应该知道谁"更有设计指导力。

### 4. 追问"需求源头在哪"能穿透表面方案

用户多次通过追问需求源头来否定方案：
- "需求源头在 Session 上" → 否定了 channel 层做 isinstance 分发
- "前端要的是 session.connect 不是 channel.open" → 否定了 channel 类型注册方案
- "Session 无法不知道 Channel" → 确定了 Channel 的架构层次

每次追问都不是在讨论"代码怎么写"，而是在问"这件事本质上归谁管"。

### 5. 好的架构对话是逐步收敛的

这次对话经历了 9 个步骤，每一步都在缩小设计空间。AI 提出方案，用户通过反例或边界条件否定不合理的部分，保留合理的部分，下一轮在更小的空间里继续。最终的设计不是一步到位想出来的，而是通过不断排除错误方向收敛出来的。
