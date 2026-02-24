# Web UI 体验改进批次 2 设计规范

**状态**：✅ 已完成
**日期**：2026-02-24
**类型**：Bug修复 / 功能改进

## 1. 背景

Web 前端在上一批改进（bugfix-web-ui-improvements）完成后，仍有若干体验问题需要修复，涉及终端恢复光标、过期提示 UI、Session 软删除、侧边栏精简模式、终端关闭确认及 tab 重复创建。后续又发现消息去重、终端退出通知、重命名同步、Session 状态恢复等问题。本文档统一设计这两批改进，共 15 项。

## 2. 设计方案

### 2.1 Bug: 终端恢复后首次输入光标位置错误 + 概率性乱码字符

**现象**：
1. 页面刷新或 WebSocket 重连后，终端 scrollback 回放完成，但用户第一次输入时光标位置不正确
2. 刷新时终端概率性出现 `^[[?1;2c` 等乱码字符（已记录于 known-issues.md）

两个问题同根同源：scrollback 回放与 input unmute 的时序问题。

**根因分析**：

当前重连流程（`TerminalPanel.tsx` + `routes.py`）：

1. 客户端建立 WS 连接
2. 服务端发送 scrollback 缓冲（`\x01` + 数据）
3. 服务端发送 `\x03`（回放完成信号）
4. 客户端收到 `\x03` → **立即** 取消 input mute → 发送 resize
5. 服务端收到 resize → `tm.resize()` → PTY 收到 SIGWINCH（或 winpty resize）

**问题 1（光标位置）**：scrollback 可能在旧终端尺寸下产生，重连后终端尺寸不同。resize 消息要等到步骤 4 才从客户端发出，存在明显延迟（client → server → PTY）。这期间用户输入会在错误位置显示。

**问题 2（`^[[?1;2c` 乱码）**：`^[[?1;2c` 是 xterm.js 对 DA1 查询（`\e[c`）的应答。scrollback 中可能包含 DA1 查询序列，xterm.js 在 `term.write()` 时处理这些序列并通过 `onData` 回调返回应答。但 `term.write()` 是异步的（使用内部写入队列），步骤 3 收到 `\x03` 时，步骤 2 的 `term.write(scrollback)` 可能尚未处理完毕。此时 unmute 导致 xterm.js 的 DA1 应答被作为用户输入发送给 PTY，PTY echo 回来就显示为 `^[[?1;2c`。

**修复方案**：

#### A. 服务端即时 resize（修复光标位置）

在 WS 连接建立时携带终端尺寸，让服务端在 scrollback 发送后立即 resize（无需等待客户端消息）：

1. **客户端**：WS URL 增加 `rows` 和 `cols` 查询参数
2. **服务端**（`routes.py` `websocket_terminal`）：从查询参数获取尺寸，scrollback 发送后立即 resize
3. **客户端**：收到 `\x03` 后仍然发送 resize（保持现有逻辑不变，作为双保险）

#### B. 延迟 unmute（修复 `^[[?1;2c` 乱码）

利用 xterm.js 的 `term.write(data, callback)` API，确保 scrollback 写入完全处理后再 unmute：

```typescript
if (bytes[0] === 0x03) {
  term.write("", () => {
    inputMuted = false;
    sendResize(termRef.current?.rows ?? rows, termRef.current?.cols ?? cols);
  });
  return;
}
```

### 2.2 改进: 终端过期提示 UI 调整

**现象**：终端过期后底部的 "Create New Terminal" 按钮文案让人误解为"新建一个独立终端"，实际意图是"在当前 session 内重新启动"。同时底部条形提示不够醒目。

**修复方案**：

将 `.terminal-expired-bar` 从底部条形改为面板中央 overlay，按钮文案改为 "Restart Terminal"。

### 2.3 Bug: 删除 Session 后刷新页面仍然显示

**现象**：通过右键菜单 "Delete" 删除 session 后，前端 `setSessions` 过滤掉了该条目。但刷新页面后，`fetchSessions()` 从后端重新加载，由于后端只做了 `stopSession()` 而没有真正删除，session 又出现了。

**修复方案**：

#### Session API 路由重构

| 操作 | 旧路由 | 新路由 | 说明 |
|------|--------|--------|------|
| Stop session | `DELETE /api/sessions/{id}` | `POST /api/sessions/{id}/stop` | 结束 session，保留记录 |
| Delete session | 无 | `DELETE /api/sessions/{id}` | 软删除 session（先 stop 再标记 deleted） |
| Update session | `PATCH /api/sessions/{id}` | `PATCH /api/sessions/{id}` | 不变 |

后端 `Session` dataclass 新增 `deleted` 字段，`list_by_workspace()` 过滤已删除的 session。

### 2.4 改进: Session 列表精简模式内容一致性

**现象**：精简模式（collapsed）下只显示当前 `activeSessionId` 对应的单个 session 图标，展开模式下则显示全部 session。

**修复方案**：精简模式显示所有 session（与展开模式相同），仅以图标形式紧凑展示。通过 `opacity: 0.5` 区分 ended session。

### 2.5 改进: Session 列表关闭 Terminal 时增加确认提示

**现象**：在 Session 列表右键菜单点击 "Close Session" 关闭 Terminal 时，没有任何确认提示。

**修复方案**：在 `App.tsx` 层面拦截 terminal session 的关闭操作，复用 `ConfirmDialog` 组件。消息："End this terminal session? The terminal process will be terminated."，按钮 "End Session" / "Cancel"。

### 2.6 改进: 已结束的 Terminal 关闭 Tab 无需确认

**现象**：关闭一个已经 ended 的 Terminal tab 时，仍然弹出确认对话框。

**修复方案**：在 `handleAction` 的 `DELETE_TAB` 分支中增加 session 状态检查，ended 状态的 terminal 直接关闭。`handleAction` 的 `useCallback` 依赖数组需要加入 `sessions`。

### 2.7 Bug: ended Terminal 重建后点击 Session 列表创建重复面板

**现象**：ended Terminal 重建 PTY 后，点击 Session 列表中该条目会创建新 tab。

**根因分析**：`onTerminalCreated` 传入的 config 不包含 `sessionId`，`Actions.updateNodeAttributes` 整体替换 config 导致 `sessionId` 丢失。

**修复方案**：

1. `TerminalPanel` Props 新增 `sessionId`，`onTerminalCreated` config 中包含 `sessionId`
2. 后端 `SessionManager.rename()` 重构为 `update()` 支持 `config` 更新
3. 后端 PATCH endpoint 支持 `config` 字段
4. 前端 `api.ts` 重构 `renameSession` 为 `updateSession`（保留便捷方法）
5. 前端重建 PTY 后调用 `updateSession()` 同步 `terminal_id`

### 2.8 Bug: 刷新页面后 Agent Session 用户消息显示两遍

**现象**：页面刷新后，Agent Session 的聊天面板中，用户之前发送的消息每条都显示了两次。

**根因分析**：

`handleSend` 的乐观本地添加与事件回放存在重复。`lastSentTextRef` 去重在页面刷新后失效（ref 重置为空），导致 `fetchSessionEvents()` 回放的 `user_message` 事件与本地添加的消息重复。

**修复方案**：

改用**事件驱动单一来源**模式：
1. `handleSend` 只通过 WS 发送消息，不再本地添加 user message
2. 用户消息的显示完全依赖后端广播的 `user_message` 事件
3. 移除 `lastSentTextRef` 相关的去重逻辑

### 2.9 Bug: 点击 Session 列表中的 Terminal 创建新 Tab

**现象**：点击已有 Terminal session，没有选中已打开的 Terminal tab，而是创建了新 tab。

**根因分析**：旧的持久化布局中 tab config 缺少 `sessionId`。

**修复方案**：

在 `handleSelectSession` 中增加 fallback 匹配策略——当 `sessionId` 匹配失败时，对 Terminal tab 额外尝试通过 `terminalId` 匹配，匹配成功时自动补全 `sessionId`。同时，`RENAME_TAB` 和 `DELETE_TAB` 处理中也应用同样的 fallback 策略。

### 2.10 Bug: 终端进程退出 / 被 End 后面板无过期提示

**现象**：
1. 终端进程退出（`exit`）后，面板无提示，停留在不可操作状态
2. 通过 Session 列表 End Terminal 后，面板也无提示

**根因分析**：没有机制通知客户端终端进程已退出。

**修复方案**：

#### A. 进程退出通知（0x04 信号）

`terminal.py` 新增 `_notify_process_exit()` 方法，reader 线程退出时发送 0x04 信号给所有 attached WS 客户端。信号包含 4 字节 exit code（big-endian int32）。

#### B. Kill 时发送 0x04

`session.py` 的 `stop()` 在 `tm.kill()` 之前调用 `await tm.async_notify_exit()` 确保信号可靠送达。

#### C. 服务端 alive-check 兜底

`websocket_terminal` 的 `receive_bytes()` 改为 2 秒超时循环，超时后检查 `session.alive`，若进程已退出则发送 0x04 并关闭 WS。

#### D. 客户端处理 0x04

`TerminalPanel.tsx` 的 `ws.onmessage` 中增加 0x04 处理，显示 `[Terminal process has ended (exit code: N)]` 并设置 expired 状态。使用 `processExited` 标志防止重复处理。

### 2.11 Bug: Tab 右键菜单重命名不同步到 Session

**现象**：右键菜单 "Rename" 后，Session 列表名称不更新。

**根因分析**：`model.doAction()` 不经过 `onAction` 回调，绕过了 `handleAction` 中的 `RENAME_TAB` 同步逻辑。

**修复方案**：在 `getTabContextMenuItems` 的 Rename handler 中手动执行 session 同步逻辑，包含 fallback 通过 terminalId 查找 session。

### 2.12 改进: Tab 双击重命名同步到 Session 列表

**现象**：双击重命名后，Session 列表名称不同步。

**修复方案**：在 `handleAction` 的 `RENAME_TAB` 分支增加 fallback 获取 sessionId（通过 terminalId 反查）。

### 2.13 Bug: Restart Terminal 后 Session 状态仍为 ended

**现象**：重建 PTY 后，Session 列表状态仍为 "ended"。

**修复方案**：
1. 后端 `SessionManager.update()` 支持 `status` 字段
2. 后端 PATCH endpoint 接受 `status`
3. 前端 `handleUpdateTabConfig` 更新状态为 "active" 并同步 sessions state
4. 新增 `onTerminalExited` 回调，终端退出时同步状态为 "ended"

### 2.14 Bug: Tab 右键菜单失效，显示浏览器默认菜单

**现象**：Tab 标题栏上右键不再显示自定义菜单（Rename/Close/Close Others），而是浏览器默认右键菜单。

**根因分析**：

`handleLayoutContextMenu` 的 node ID 解析逻辑在 2.9 修改后引入了两个 bug：

1. **Strategy 1**（`el.id`）：flexlayout-react 的 `TabButton` 组件不设置 DOM `id` 属性，`el.id` 始终为空字符串，此策略永远不会匹配。
2. **Strategy 2**（`data-layout-path`）：Tab 按钮的 `data-layout-path` 是渲染路径（如 `/ts0/tb0`），与 `node.getId()` 返回的节点 ID（如 `#1`）完全不同。`node.getId() === layoutPath` 永远为 false。

两个策略全部失败 → `matchedNodeId` 为 null → 函数提前 return → 不调用 `e.preventDefault()` → 浏览器默认菜单显示。

**修复方案**：

利用 flexlayout-react 的路径规则：Tab 按钮的 `data-layout-path` 格式为 `{tabsetPath}/tb{index}`，对应的 Tab 节点路径格式为 `{tabsetPath}/t{index}`。通过正则替换 `/tb(\d+)$` 为 `/t$1` 得到 tab 节点路径，再用 `node.getPath()` 匹配：

```typescript
const layoutPath = el.getAttribute("data-layout-path");
if (layoutPath) {
  const tabPath = layoutPath.replace(/\/tb(\d+)$/, "/t$1");
  model.visitNodes((node) => {
    if (node.getType() === "tab" && node.getPath() === tabPath) {
      matchedNodeId = node.getId();
    }
  });
}
```

### 2.15 Bug: 终端进程退出后 WS 无限重连循环

**现象**：终端进程退出后，客户端不停地重连 WS，每 2-3 秒一次循环，日志中持续出现 `attached client → detached client` 交替。

**根因分析**：

1. 终端进程退出 → 服务端发送 0x04 → 客户端设置 `processExited = true`
2. 服务端 `websocket_terminal` 的 alive-check（2 秒超时）检测到 `session.alive == false` → 发送 exit payload → `break` → WS 关闭
3. 客户端 `ws.onclose` 触发，关闭码非 4004 → 进入自动重连逻辑
4. 新 WS 连接成功 → `retryCount` 重置为 0 → 2 秒后 alive-check 再次关闭 → 无限循环

**修复方案**：

在 `ws.onclose` 中增加 `processExited` 检查：

```typescript
ws.onclose = (event) => {
  if (!active) return;
  if (event.code === 4004) { /* ... */ }
  // Don't reconnect if the terminal process has already exited
  if (processExited) return;
  // Auto-reconnect with backoff ...
};
```

## 3. 已确认的设计决策

| 决策项 | 结论 |
|--------|------|
| 终端光标修复 | WS 连接时携带 rows/cols，服务端在 scrollback 发送后立即 resize |
| `^[[?1;2c` 乱码修复 | 利用 `term.write("", callback)` 延迟 unmute |
| 终端过期提示 | 面板中央 overlay，按钮文案 "Restart Terminal" |
| Session API 路由 | stop 改为 `POST /stop`，delete 使用 `DELETE`，语义与 HTTP 方法对齐 |
| 精简模式 ended session | 显示全部 session，通过 `opacity: 0.5` 视觉区分 |
| 终端关闭确认 | Session 列表 Close 活动 terminal 时弹出确认，ended terminal 直接关闭 |
| 用户消息去重策略 | 去掉乐观本地添加，完全依赖事件系统（单一真相来源） |
| Terminal tab 匹配 fallback | 先按 sessionId 匹配，失败后按 terminalId 匹配 |
| 终端退出通知协议 | 新增 0x04 信号（含 exit code），覆盖进程自然退出 + kill() + alive-check 兜底 |
| 右键 Rename 同步 | 在 context menu handler 中手动执行 session 同步 |
| Session 状态恢复 | 重建 PTY 后通过 PATCH API 将 status 改回 "active" |
| Tab 右键菜单节点匹配 | 通过 `data-layout-path` 路径转换 + `node.getPath()` 匹配 |
| 终端退出后 WS 重连 | 检查 `processExited` 标志阻止重连 |

## 4. 实施步骤清单

### 阶段一：终端恢复与过期提示 [✅ 已完成]

- [x] **Task 1.1**: 修复 ended Terminal 重建后 tab 重复创建
  - [x] `TerminalPanel` Props 新增 `sessionId`，`PanelFactory.tsx` 传入
  - [x] `onTerminalCreated` config 中包含 `sessionId`
  - [x] 后端 `SessionManager.rename()` 重构为 `update()` 支持 `config` 更新
  - [x] 后端 PATCH endpoint 支持 `config` 字段
  - [x] 前端 `api.ts` 重构为 `updateSession`（保留 `renameSession` 便捷方法）

- [x] **Task 1.2**: 修复终端恢复后光标位置错误 + `^[[?1;2c` 乱码
  - [x] `buildWsUrl()` 增加 `rows`/`cols` 参数
  - [x] 后端 `websocket_terminal` 读取 rows/cols 查询参数，scrollback 后 resize
  - [x] 客户端收到 `\x03` 后使用 `term.write("", callback)` 延迟 unmute

### 阶段二：Session API 重构 + 软删除 [✅ 已完成]

- [x] **Task 2.1**: Session API 路由重构
  - [x] stop 改为 `POST /api/sessions/{id}/stop`
  - [x] 新增 `DELETE /api/sessions/{id}` 实现软删除

- [x] **Task 2.2**: Session 软删除
  - [x] `Session` dataclass 增加 `deleted` 字段
  - [x] `list_by_workspace()` 过滤 `deleted`
  - [x] 前端 `handleDeleteSession` 改用 `deleteSession()`

- [x] **Task 2.3**: Session 列表精简模式内容一致性

### 阶段三：Terminal 关闭确认改进 [✅ 已完成]

- [x] **Task 3.1**: Session 列表关闭 Terminal 时增加确认
- [x] **Task 3.2**: ended Terminal 关闭 Tab 无需确认

### 阶段四：UI 调整 + known-issues 分离 [✅ 已完成]

- [x] **Task 4.1**: 终端过期提示改为居中 Overlay
- [x] **Task 4.2**: known-issues.md 分离

### 阶段五：消息去重 + Tab 匹配 + 退出通知 [✅ 已完成]

- [x] **Task 5.1**: 修复 Agent Session 用户消息刷新后重复显示
  - [x] `AgentPanel.tsx`：移除乐观本地添加和 `lastSentTextRef` 去重

- [x] **Task 5.2**: 修复 Terminal Session 点击创建新 tab
  - [x] `handleSelectSession` 增加 terminalId fallback 匹配

- [x] **Task 5.3**: 修复终端进程退出 / 被 End 后面板无过期提示
  - [x] `terminal.py`：新增 `_notify_process_exit()` 和 `async_notify_exit()` 方法
  - [x] `terminal.py`：reader 线程退出时发送 0x04（含 exit code）
  - [x] `session.py`：`stop()` 中 kill 前调用 `async_notify_exit()`
  - [x] `routes.py`：`websocket_terminal` 增加 2 秒 alive-check 兜底
  - [x] `TerminalPanel.tsx`：`ws.onmessage` 增加 0x04 处理

### 阶段六：重命名同步 + Session 状态恢复 [✅ 已完成]

- [x] **Task 6.1**: Tab 右键菜单重命名同步到 Session
- [x] **Task 6.2**: Tab 双击重命名同步到 Session 列表
- [x] **Task 6.3**: Restart Terminal 后更新 Session 状态为 active
  - [x] 后端 `update()` 和 PATCH endpoint 支持 `status` 字段
  - [x] 前端 `handleUpdateTabConfig` 更新状态 + 新增 `onTerminalExited` 回调

### 阶段七：Bug 修复（回归） [✅ 已完成]

- [x] **Task 7.1**: 修复 Tab 右键菜单失效
  - [x] `handleLayoutContextMenu` 改用 `data-layout-path` → `node.getPath()` 匹配

- [x] **Task 7.2**: 修复终端退出后 WS 无限重连循环
  - [x] `TerminalPanel.tsx`：`ws.onclose` 增加 `processExited` 检查阻止重连
