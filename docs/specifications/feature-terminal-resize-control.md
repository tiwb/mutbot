# 终端大小控制权管理 设计规范

**状态**：🔄 实施中
**日期**：2026-03-13
**类型**：重构

## 背景

多客户端连接同一终端时，需要决定 PTY 尺寸跟随哪个客户端。

### 第一版实现（已完成，待重构）

第一版采用 `_primary_client` + `_primary_locked` 双状态：
- `_primary_client`：当前控制 resize 的客户端
- `_primary_locked`：是否禁止自动切换（bool）

存在的问题：
1. **lock 语义模糊**：`_primary_locked` 只是一个 bool，不记录谁锁的。任何客户端发 `claim_resize(lock=false)` 都能清除别人的锁并抢占 primary
2. **前端参与决策**：前端维护 `isPrimary` / `fitPaused`，基于这些状态决定是否发送 resize、是否暂停 fit。前端不应做这些决策
3. **首连自动成为 primary**：第一个连接的客户端自动成为 primary，但这个抢占应该是用户主动触发的
4. **claim_resize 协议混乱**：`lock=true` 和 `lock=false` 两个操作语义不对称，`lock=false` 既是"解锁"又是"抢占 primary"

### 已确认的触发场景

1. **最小化的浏览器窗口**：用户只有一个活跃标签页，但存在一个被最小化的旧 Chrome 窗口（innerWidth=1, innerHeight=1），其 xterm 报告 rows=1，PTY 被 resize 到 1 行
2. **Playwright 自动化工具**：另一个 Claude Code session 通过 Playwright MCP 操控浏览器，打开了页面。Playwright 控制的浏览器 viewport 极小，创建了第二个 WebSocket 客户端，拖垮了正常客户端的 PTY 尺寸
3. **手机连接抢占 PC 尺寸**：PC 端已 Follow Me，手机端连接或刷新时仍然把 PTY 尺寸抢占为手机尺寸
4. **手机输入框 auto-grow 触发频繁 resize**：移动端输入框（textarea）随输入内容自动增长/缩小 1-4 行，每次高度变化挤压终端容器 → ResizeObserver → fitAddon.fit() → PTY resize → 闪烁。输入框是临时使用的 UI，不应影响终端尺寸

## 设计方案

### 核心模型：两层控制

**第一层 — Auto（默认）**：PTY 尺寸跟随最后一次输入（打字）的客户端。客户端 A 打字 → PTY 切到 A 的尺寸；客户端 B 打字 → PTY 切到 B 的尺寸。

**第二层 — Follow Me（用户主动触发）**：覆盖 Auto，将 PTY 尺寸锁定到指定客户端。不管谁打字，PTY 尺寸不变。只有 Follow Me 客户端的 resize 生效。

### 状态模型

```python
# 用户主动锁定的客户端（None = Auto 模式）
_follow_me: dict[str, str | None] = {}        # {term_id: client_id | None}

# Auto 模式下，最后打字的客户端
_last_input_client: dict[str, str | None] = {} # {term_id: client_id | None}

# 每个客户端报告的尺寸（始终记录，用于切换时查找）
_client_sizes: dict[str, dict[str, tuple[int, int]]] = {}
```

### resize 决策逻辑

谁的 resize 生效（后端唯一决策者）：

| follow_me | last_input_client | resize 来自 | 结果 |
|-----------|------------------|------------|------|
| 有值 | （忽略） | follow_me 客户端 | 生效 |
| 有值 | （忽略） | 其他客户端 | 仅记录 |
| None | 有值 | last_input_client | 生效 |
| None | 有值 | 其他客户端 | 仅记录 |
| None | None | 任何客户端 | 生效（初始状态，last-write-wins） |

### 事件驱动行为

| 事件 | 行为 |
|------|------|
| 终端创建 | 无 follow_me，无 last_input_client。PTY 用创建时的尺寸 |
| 客户端连接 | 记录连接，**不改变**任何控制权状态。发送当前 resize 状态 + pty_resize |
| 客户端打字（follow_me=None） | last_input_client = 该客户端，PTY resize 到该客户端尺寸 |
| 客户端打字（follow_me 有值） | 不改变任何状态 |
| 客户端 resize | 记录 client_sizes；如果该客户端是当前控制者 → 应用到 PTY + 广播 pty_resize |
| 点 "Follow Me" | follow_me = 该客户端，PTY resize 到该客户端尺寸，广播 |
| 点 "Auto" | follow_me = None，恢复 Auto 模式，广播 |
| follow_me 客户端断开 | follow_me = None，Auto 恢复 |
| last_input_client 断开 | last_input_client = None（下次打字时更新） |

### 消息协议

| 方向 | type | 字段 | 说明 |
|------|------|------|------|
| 前端→后端 | `resize` | `rows, cols` | 报告客户端容器尺寸（无条件发送） |
| 前端→后端 | `set_resize_mode` | `mode: "auto" \| "follow_me"` | 用户主动切换模式 |
| 后端→前端 | `resize_owner` | `follow_me: client_id \| null` | 广播当前 Follow Me 状态 |
| 后端→前端 | `pty_resize` | `rows, cols` | 广播 PTY 实际尺寸，前端据此调整 xterm |

### 前端职责（仅上报 + 执行）

前端**不做任何决策**，不维护 `isPrimary` / `fitPaused` 等状态。

**上报事件**：
- 容器尺寸变化 → 发 `resize`（无条件，不检查是否是控制者）
- 用户点菜单 → 发 `set_resize_mode`

**执行命令**：
- 收到 `pty_resize` → 调整 xterm 到该尺寸。如果与容器尺寸不一致，暂停 fitAddon（基于尺寸比较，不基于 isPrimary）
- 收到 `resize_owner` → 更新菜单状态

### 右键菜单

两个互斥项：

| follow_me 状态 | "Auto (follow input)" | "Follow Me" |
|---------------|----------------------|-------------|
| null（Auto 模式） | ✓ | ✗ |
| == 我的 clientId | ✗ | ✓ |
| == 别人的 clientId | ✗ | ✗ |

操作：
- 点 "Auto" → 发 `set_resize_mode: auto`
- 点 "Follow Me" → 发 `set_resize_mode: follow_me`

### 最小尺寸保护（保留）

`resize()` 入口处 `rows < 2` 或 `cols < 10` 的请求直接忽略，不记入 `_client_sizes`。独立于控制权机制的底线保护。

### 移动端输入框浮动布局（减少 resize 频率）

**问题**：移动端输入框（textarea）随输入内容 auto-grow（1-4 行），每次高度变化挤压终端容器 → 触发 resize → PTY resize → 闪烁。

**方案**：输入框容器（wrapper）高度固定为单行，textarea 使用 `position: absolute; bottom: 0` 向上浮动扩展，覆盖终端区域而非挤压。终端容器尺寸不受输入框内容影响，不触发 resize。

- wrapper 高度固定：`height: calc(1.4em * 1 + 16px + 2px)`
- textarea 绝对定位向上扩展，最大 8 行（从原来的 4 行增加，因为不再影响终端尺寸）
- 输入栏整体高度始终为单行，终端获得稳定的可用空间

## 实施步骤清单

### Phase 1: 后端 TerminalManager 状态重构 [✅ 已完成]

- [x] **Task 1.1**: 替换状态字段
  - [x] `_primary_client` → `_follow_me`（`dict[str, str | None]`）
  - [x] `_primary_locked` → `_last_input_client`（`dict[str, str | None]`）
  - [x] 删除 `get_primary_info()` 和 `try_set_primary()`
  - [x] `kill()` 和 `_on_ptyhost_disconnect()` 中更新清理逻辑
  - 状态：✅ 已完成

- [x] **Task 1.2**: 新增 resize 决策方法
  - [x] 新增 `_get_resize_controller(term_id) -> str | None` — 返回当前控制者（follow_me 优先，其次 last_input_client，都无则 None）
  - [x] 新增 `get_follow_me(term_id) -> str | None` — 供 @impl 层查询并广播
  - 状态：✅ 已完成

- [x] **Task 1.3**: 重写 `resize()` 决策逻辑
  - [x] 记录 `_client_sizes` 不变
  - [x] 用 `_get_resize_controller()` 判断：controller 存在且 != client_id → 仅记录；controller 为 None 或 == client_id → 执行 resize
  - 状态：✅ 已完成

- [x] **Task 1.4**: 重写 `attach()` — 不再自动设 primary
  - [x] 只注册回调，不设置任何控制权状态
  - [x] 返回值改为 `None`（不再返回 became_primary）
  - 状态：✅ 已完成

- [x] **Task 1.5**: 重写 `detach()` — follow_me / last_input_client 清理
  - [x] 如果断开的是 follow_me 客户端 → `_follow_me[term_id] = None`
  - [x] 如果断开的是 last_input_client → `_last_input_client[term_id] = None`
  - [x] 不再选新主、不再 resize（PTY 保持当前尺寸，等下次输入或 Follow Me 操作）
  - 状态：✅ 已完成

### Phase 2: 后端 @impl 消息处理重构 [✅ 已完成]

- [x] **Task 2.1**: 重写 `_terminal_on_data` — Auto 模式下更新 last_input_client
  - [x] 如果 `_follow_me` 有值 → 不改变任何状态，只转发输入
  - [x] 如果 `_follow_me` 为 None → 更新 `_last_input_client`，如果变化了则 resize PTY 到该客户端尺寸 + 广播
  - 状态：✅ 已完成

- [x] **Task 2.2**: 重写 `_terminal_on_message` — `claim_resize` 替换为 `set_resize_mode`
  - [x] `mode: "follow_me"` → 设 `_follow_me = client_id`，resize PTY 到该客户端尺寸，广播 `resize_owner` + `pty_resize`
  - [x] `mode: "auto"` → 设 `_follow_me = None`，广播 `resize_owner`（PTY 尺寸不变）
  - [x] resize 消息处理保持，用新的决策逻辑
  - 状态：✅ 已完成

- [x] **Task 2.3**: 重写 `_terminal_on_connect` — 发送状态但不改变状态
  - [x] 发送 `resize_owner`：`{follow_me: client_id | null}`（新协议格式）
  - [x] 发送 `pty_resize`：从当前控制者的 `_client_sizes` 查找，无则不发
  - 状态：✅ 已完成

- [x] **Task 2.4**: 重写 `_terminal_on_disconnect` — 广播新的 resize_owner 格式
  - 状态：✅ 已完成

### Phase 3: 前端重构 [✅ 已完成]

- [x] **Task 3.1**: 移除前端决策逻辑
  - [x] 删除 `isPrimary`、`fitPaused` 变量及相关分支
  - [x] `sendResize` 无条件发送（ready 回调中不再检查 isPrimary）
  - [x] `pty_resize` 处理：xterm resize 后无额外决策
  - 状态：✅ 已完成

- [x] **Task 3.2**: 更新 `resize_owner` 消息处理
  - [x] 适配新协议：`{follow_me: client_id | null}`
  - [x] 新增 state：`followMe: string | null`（记录 follow_me client_id）
  - [x] 删除旧的 `resizeLocked` state
  - 状态：✅ 已完成

- [x] **Task 3.3**: 更新右键菜单
  - [x] "Auto (follow input)"：checked = `followMe === null`，点击发 `{type: "set_resize_mode", mode: "auto"}`
  - [x] "Follow Me"：checked = `followMe === rpc.clientId`，点击发 `{type: "set_resize_mode", mode: "follow_me"}`
  - 状态：✅ 已完成

- [x] **Task 3.4**: 前端构建
  - [x] `npm --prefix mutbot/frontend run build`
  - 状态：✅ 已完成

- [x] **Task 3.5**: 移动端输入框浮动布局
  - [x] `TerminalInput.tsx`：textarea 外包一层 `.terminal-input-textarea-wrapper`
  - [x] CSS：wrapper 固定单行高度，textarea 绝对定位 `bottom: 0` 向上扩展
  - [x] max-height 从 4 行增加到 8 行
  - 状态：✅ 已完成

### Phase 4: 验证 [待开始]

- [ ] **Task 4.1**: 单客户端验证
  - [ ] 终端 resize 跟随窗口变化
  - [ ] Follow Me 切换正常
  - 状态：⏸️ 待开始

- [ ] **Task 4.2**: 多客户端验证
  - [ ] Auto 模式：打字切换 resize 控制
  - [ ] Follow Me：锁定后其他客户端无法抢占
  - [ ] Follow Me 客户端断开 → 恢复 Auto
  - [ ] 点 Auto 释放 Follow Me
  - 状态：⏸️ 待开始

## 关键参考

### 源码

- `mutbot/src/mutbot/runtime/terminal.py` — TerminalManager（`_follow_me`, `_last_input_client`, `_get_resize_controller`, `resize`, `attach`, `detach`），TerminalSession @impl（`on_connect`, `on_disconnect`, `on_message`, `on_data`）
- `mutbot/frontend/src/panels/TerminalPanel.tsx` — `sendResize()`, `handleJsonMessage()`, `followMe` state, 右键菜单 `menuItems`
- `mutbot/frontend/src/components/ContextMenu.tsx` — ContextMenuItem 接口
- `mutbot/frontend/src/lib/workspace-rpc.ts` — `clientId`（uuid 生成）

### 架构要点

- resize 消息流：前端 `sendResize` → 后端 `on_message` → `tm.resize()` → ptyhost → broadcast `pty_resize` → 前端 `term.resize()`
- 输入消息流：前端 `onData` → `sendBinaryToChannel` → 后端 `on_data` → `tm.write()`
- `client_id` 通过 `ChannelTransport.get(channel)._client.client_id` 获取

### 相关规范

- `bugfix-terminal-rendering-flicker.md` — pyte diff 模式（已实施），与 resize 控制权正交
- `feature-server-side-virtual-terminal.md` — 未来多尺寸视口（per-client pyte Screen）

### 设计讨论记录

本次重构经过多轮讨论，从最初的 bug 分析逐步澄清到最终设计：

1. **初始问题**：手机连接时抢占 PC 的 Follow Me 锁定
2. **分析 primary + lock 关系**：发现 lock 只阻止了 on_data 自动切换，其他路径未受保护
3. **用户澄清**：primary 和 lock 都应是纯后端状态，前端不应做决策
4. **简化尝试**：去掉 lock，只留 resize owner → 但发现需要"无 owner"的默认状态
5. **需求深化**：用户期望两层控制（Auto 跟随输入 + Follow Me 显式锁定）
6. **最终设计**：`_follow_me` + `_last_input_client` 双状态，前端纯上报+执行
