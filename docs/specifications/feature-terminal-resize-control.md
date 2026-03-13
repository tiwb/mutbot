# 终端大小控制权管理 设计规范

**状态**：🔄 实施中
**日期**：2026-03-13
**类型**：功能设计

## 背景

多客户端连接同一终端时，后端取所有客户端尺寸的 **min** 作为 PTY 有效尺寸。当某个客户端窗口极小或异常时，会将所有客户端的终端拖到极小尺寸（如 1 行 1 列），导致正常使用的客户端看不到任何输出。

### 已确认的触发场景

1. **最小化的浏览器窗口**：用户只有一个活跃标签页，但存在一个被最小化的旧 Chrome 窗口（innerWidth=1, innerHeight=1），其 xterm 报告 rows=1，PTY 被 resize 到 1 行。

2. **Playwright 自动化工具**：另一个 Claude Code session 通过 Playwright MCP 操控浏览器，打开了 `http://127.0.0.1:8741` 页面。Playwright 控制的浏览器 viewport 极小，创建了第二个 WebSocket 客户端，其 xterm 报告极小的 rows/cols，拖垮了正常客户端的 PTY 尺寸。Playwright 操作是间歇性的，导致问题时有时无——操作期间尺寸错乱，操作结束后用户浏览器重新发 resize 恢复。此场景还会导致终端闪烁（两个客户端交替发送不同 resize 值，PTY 尺寸反复跳动）。

### 问题特征

- 前端 xterm 容器和 screen 尺寸始终正确（如 870x900），问题在 PTY 侧
- 即使只有 1 个用户活跃连接，Playwright 等自动化工具仍可悄无声息地创建额外连接
- 服务器重启可临时恢复（`_client_sizes` 被清空），但问题会再次出现
- 终端闪烁（scrollback 大量输出时尤为明显）是 resize 值反复跳动的表现

## 设计方案

### 核心思路

将 "所有客户端取 min" 的无差别策略改为 **主客户端优先** 策略。同一时刻只有一个客户端拥有终端大小的控制权（"主客户端"），PTY 尺寸跟随该客户端。

### 主客户端选举规则

1. **输入优先（默认自动模式）**：最后一个向终端发送输入的客户端自动成为主客户端
2. **手动锁定**：用户通过右键菜单手动声明"跟随此客户端"，锁定控制权
3. **回退**：如果主客户端断开，自动降级为 `_client_sizes` 中任意存活客户端（回退场景少见，不值得追踪 last_input 增加复杂度）

### 后端改动

#### TerminalManager 新增状态

```python
# 每个终端的主客户端 ID
self._primary_client: dict[str, str] = {}         # {term_id: client_id}
# 是否锁定（用户手动指定）
self._primary_locked: dict[str, bool] = {}         # {term_id: locked}
```

#### resize() 方法改造

收到客户端 resize 消息时：
- 仍然记录所有客户端的 `_client_sizes`（用于主客户端切换时查找尺寸）
- **只有主客户端的尺寸生效**，其他客户端的 resize 消息只记录不应用
- 如果发送 resize 的客户端不是主客户端，不调用 ptyhost resize，不广播 `pty_resize`

#### 输入触发主客户端切换

在 `_terminal_on_data`（键盘输入转发）中：
- 如果 `_primary_locked` 为 False，且发送输入的客户端不是当前主客户端 → 切换主客户端
- 切换时从 `_client_sizes` 读取新主客户端的尺寸，调用 ptyhost resize，广播 `pty_resize`
- **边缘情况**：如果新主客户端在 `_client_sizes` 中无记录（首次 resize 被最小尺寸保护拦截），不执行 resize，等其发送下一次有效 resize

#### 新增消息类型

| 消息方向 | type | 字段 | 说明 |
|---------|------|------|------|
| 前端→后端 | `claim_resize` | `lock: bool` | 手动声明控制权。`lock=true` 锁定，`lock=false` 解锁回自动模式 |
| 后端→前端 | `resize_owner` | `client_id: str, locked: bool` | 广播当前主客户端信息，前端据此更新 UI 状态 |

#### detach 时的处理

客户端断开时：
- 如果断开的是主客户端 → 清除主客户端，按回退规则选新主客户端
- 如果 `_primary_locked` 且主客户端断开 → 解锁，回退到自动模式
- **`detach()` 方法重构**：当前 `detach()` 内部做 min 重算 + fire-and-forget resize，新设计下需改为：断开主客户端时选新主并用新主尺寸 resize；断开非主客户端时仅清理 `_client_sizes` 不 resize

### 前端改动

#### 右键菜单新增项

在现有菜单的 "Clear Terminal" 之后添加：

```
--- separator ---
✓ 自动跟随输入 (Auto)        ← 默认选中，自动模式
  跟随此客户端 (Follow Me)    ← 点击后锁定本客户端
```

互斥关系：
- **自动跟随输入**：选中时为自动模式（`lock=false`）。每次在此终端输入都会自动抢夺控制权
- **跟随此客户端**：选中时为锁定模式（`lock=true`）。发送 `claim_resize` 锁定，其他客户端的输入不再触发切换

状态由后端 `resize_owner` 消息驱动：
- 收到 `resize_owner` 时，如果 `client_id === 本客户端 && locked === true` → "跟随此客户端" 显示 ✓
- 否则 → "自动跟随输入" 显示 ✓

#### pty_resize 处理调整

无需变动。后端只在主客户端 resize 时才广播 `pty_resize`，前端逻辑不变。

### 最小尺寸保护

在 `resize()` 方法入口处增加阈值检查：
- `rows < 2` 或 `cols < 10` 的 resize 请求直接忽略，不记入 `_client_sizes`
- 这是独立于主客户端机制的底线保护，防止极端尺寸（最小化窗口、Playwright 小 viewport 等）

### 单客户端场景

只有一个客户端时，行为与现在完全一致：
- 该客户端自动成为主客户端
- resize 直接生效
- 右键菜单项仍可见但无实际多客户端效果

## 已知关联问题：终端闪烁

终端在 scrollback 大量输出后会出现闪烁，这个问题一直存在，不影响功能但体验不佳。

### 两种闪烁场景

1. **多客户端 resize 竞争引起的闪烁**：两个客户端交替发送不同的 resize 值（如 Playwright 发 cols=1、用户浏览器发 cols=100），PTY 尺寸反复跳动，xterm 反复重绘 → 闪烁。**本设计的主客户端机制可解决此场景**。

2. **单客户端大量输出时的闪烁**：终端 scrollback 累积大量数据后，即使只有一个客户端也会闪烁。这与 resize 无关，可能的原因：
   - WebSocket 发送缓冲区压力大，消息到达不均匀，xterm 渲染抖动
   - xterm.js 大量 write() 调用的渲染性能问题
   - 浏览器重绘/回流开销

场景 2 不在本设计范围内，但值得后续独立调查。

## 已决策

- **主客户端视觉提示**：不需要额外提示，右键菜单中的 ✓ 状态已足够（多数情况下用户只有一个客户端）

## 关键参考

### 源码
- `mutbot/src/mutbot/runtime/terminal.py` — TerminalManager 类（`_client_sizes`, `resize`, `attach`, `detach`），TerminalSession @impl（`on_connect`, `on_disconnect`, `on_message`, `on_data`）
- `mutbot/src/mutbot/web/transport.py` — Client 类（`client_id`, `state`, `expire`），ChannelTransport Extension（关联 Channel 与 Client）
- `mutbot/frontend/src/panels/TerminalPanel.tsx` — `sendResize()`, `handleJsonMessage()`, 右键菜单 `menuItems`, `serverResizing` 防反馈
- `mutbot/frontend/src/components/ContextMenu.tsx` — ContextMenuItem 接口（`label`, `checked`, `onClick`, `separator`）
- `mutbot/frontend/src/lib/workspace-rpc.ts` — `clientId`（uuid 生成，行 110）、`sendToChannel`、`sendBinaryToChannel`
- `mutbot/src/mutbot/web/rpc_session.py` — `connect`/`disconnect` RPC 方法

### 架构要点
- resize 消息流：前端 `sendResize` → 后端 `on_message` → `tm.resize()` → ptyhost → broadcast `pty_resize` → 前端 `term.resize()`
- 输入消息流：前端 `onData` → `sendBinaryToChannel` → 后端 `on_data` → `tm.write()`
- 广播通过 SessionChannels Extension，遍历所有 channel 调用 `send_json`
- `client_id` 通过 `ChannelTransport.get(channel)._client.client_id` 获取
- 前端 `clientId` 在 `WorkspaceRpc` 构造时 uuid 生成（workspace-rpc.ts:110），通过 URL query param 传递到后端

### 调试信息
- 临时调试日志已在实施过程中移除

## 实施步骤清单

### Phase 1: 后端核心逻辑 [✅ 已完成]

- [x] **Task 1.1**: 最小尺寸保护
  - [x] `resize()` 入口增加阈值检查（rows < 2 或 cols < 10 直接忽略，不记入 `_client_sizes`）
  - 状态：✅ 已完成

- [x] **Task 1.2**: 主客户端状态管理
  - [x] TerminalManager 新增 `_primary_client` 和 `_primary_locked` 状态
  - [x] `attach` 时：如果当前无主客户端，将新连接设为主客户端
  - [x] `detach` 重构：断开主客户端时解锁 + 从 `_client_sizes` 选任意存活客户端为新主 + 用新主尺寸 resize；断开非主客户端时仅清理 `_client_sizes` 不 resize
  - [x] 新增 `try_set_primary()` 和 `get_primary_info()` 辅助方法
  - 状态：✅ 已完成

- [x] **Task 1.3**: resize() 方法改造为主客户端优先
  - [x] 仍记录所有客户端的 `_client_sizes`
  - [x] 只有主客户端的 resize 调用 ptyhost resize + 广播 `pty_resize`
  - [x] 非主客户端的 resize 只记录不应用
  - 状态：✅ 已完成

- [x] **Task 1.4**: 输入触发主客户端自动切换
  - [x] `_terminal_on_data` 中提取 client_id（复用 `ChannelTransport.get(channel)` 模式）
  - [x] 如果未锁定且输入来自非主客户端 → 切换主客户端
  - [x] 切换时从 `_client_sizes` 读取新主客户端尺寸；如果无记录则跳过 resize，等下次有效 resize
  - [x] 切换后广播 `resize_owner` + `pty_resize`
  - 状态：✅ 已完成

### Phase 2: 消息协议与手动锁定 [✅ 已完成]

- [x] **Task 2.1**: 新增 `claim_resize` 消息处理（前端→后端）
  - [x] 在 `_terminal_on_message` 中处理 `claim_resize` 消息
  - [x] `lock=true`：设为主客户端 + 锁定；`lock=false`：解锁回自动模式
  - [x] 处理后广播 `resize_owner`
  - 状态：✅ 已完成

- [x] **Task 2.2**: `resize_owner` 广播统一处理
  - [x] 主客户端变更时广播 `resize_owner`（含 `client_id` 和 `locked`）
  - [x] 新客户端 attach 时向其发送当前 `resize_owner` 状态
  - [x] 广播触发点：attach 首个客户端、输入切换、claim_resize、detach 回退（共 4 处）
  - 状态：✅ 已完成

### Phase 3: 前端改动 [✅ 已完成]

- [x] **Task 3.1**: 暴露 clientId 并处理 `resize_owner` 消息
  - [x] `WorkspaceRpc` 新增 `clientId` getter（private `_clientId` + public getter）
  - [x] TerminalPanel 通过 `rpc.clientId` 与 `resize_owner.client_id` 比较
  - [x] TerminalPanel 新增状态：`resizeLocked`（bool）
  - [x] `handleJsonMessage` 中处理 `resize_owner` 消息，更新状态
  - 状态：✅ 已完成

- [x] **Task 3.2**: 右键菜单新增控制项
  - [x] 在 "Clear Terminal" 之后添加 separator + 两个互斥菜单项
  - [x] "Auto (follow input)"：checked 由 `!resizeLocked` 决定，点击发送 `claim_resize {lock: false}`
  - [x] "Follow Me"：checked 由 `resizeLocked` 决定，点击发送 `claim_resize {lock: true}`
  - 状态：✅ 已完成

### Phase 4: 清理与验证 [✅ 已完成]

- [x] **Task 4.1**: 移除临时调试日志
  - [x] on_message 重写时已移除临时日志
  - 状态：✅ 已完成

- [x] **Task 4.2**: 前端构建与手动验证
  - [x] 构建前端：`npm --prefix mutbot/frontend run build` ✅
  - [ ] 启动服务器，单客户端验证 resize 正常
  - [ ] 多客户端验证：打开第二个极小窗口，确认不影响正常客户端
  - [ ] 右键菜单切换模式验证
  - 状态：🔄 待手动验证

## 测试验证

（实施阶段填写）
