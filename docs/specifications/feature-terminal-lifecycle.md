# 终端会话生命周期管理

**状态**：✅ 已完成
**日期**：2026-02-23
**类型**：问题修复 / 功能增强

## 1. 问题描述

### 1.1 现状

当前终端会话存在生命周期管理缺陷：

1. **PTY 进程泄漏**：前端创建终端面板后，PTY 进程在服务器端运行。当用户关闭面板标签页、刷新浏览器或切换布局时，前端 WebSocket 断开，`routes.py` 的 `websocket_terminal` 在 `finally` 中调用 `tm.kill(term_id)` 直接杀死 PTY 进程。但如果布局被持久化时包含了终端面板的配置，下次恢复布局时该终端 ID 已不存在，导致终端面板显示断开状态。

2. **不可重连**：终端 WebSocket 断开后 PTY 即被销毁，无法重新连接到同一个终端会话。用户刷新页面后所有终端历史丢失。

3. **终端不随 Workspace 持久化**：Workspace 没有记录关联的终端会话列表，服务器重启后所有终端丢失（这是合理的，因为 PTY 进程不跨重启存活），但缺少优雅的处理——前端可能尝试连接已不存在的终端 ID。

4. **无终端列表 API**：前端无法查询当前 Workspace 下有哪些活跃终端，只能在创建时拿到 ID。

### 1.2 期望行为

- 关闭终端面板标签页 → PTY 进程被清理（当前行为，合理）
- 刷新浏览器 → 如果终端 PTY 仍然存活，应能重新连接并恢复输出
- WebSocket 临时断开（网络抖动）→ 重连后恢复终端，不丢失 PTY 进程
- 服务器重启 → 终端自然丢失（PTY 进程不存活），前端优雅降级
- 多客户端 → 同一终端可被多个浏览器标签页查看

## 2. 根因分析

```
当前流程：
TerminalPanel mount → POST /api/terminals → 获得 term_id → WS connect
                                                              ↓
                      TerminalPanel unmount ← WS close → tm.kill(term_id)
                      （PTY 进程被杀）

问题所在：
WS 断开 = PTY 死亡，这个等式过于激进
```

核心问题是 **WebSocket 生命周期与 PTY 生命周期强绑定**。应该将两者解耦：

- PTY 生命周期由 TerminalManager 管理，独立于 WebSocket 连接
- WebSocket 仅作为 I/O 通道，断开不影响 PTY 存活
- 显式的终端关闭操作（用户关闭面板、DELETE API）才销毁 PTY

## 3. 设计方案

### 3.1 终端生命周期解耦

```
创建：POST /api/terminals → 启动 PTY → 返回 term_id
          ↓
连接：WS /ws/terminal/{term_id} → 附着到 PTY I/O
          ↓
断开：WS close → 仅移除 I/O 通道，PTY 继续运行
          ↓
重连：WS /ws/terminal/{term_id} → 重新附着到 PTY I/O
          ↓
销毁：DELETE /api/terminals/{term_id} → 杀死 PTY 进程
      或：面板关闭事件 → 前端调用 DELETE
```

### 3.2 终端输出缓冲（滚回）

为支持重连后恢复屏幕内容，TerminalSession 需要维护一个有限的输出环形缓冲区：

```python
@dataclass
class TerminalSession:
    # ... 现有字段 ...
    _scrollback: bytearray  # 环形缓冲，保留最近 N 字节输出
    _scrollback_max: int = 64 * 1024  # 64KB
```

重连时，先发送 scrollback 缓冲内容，再切换到实时流。

### 3.3 多客户端支持

借鉴 Agent 会话的 ConnectionManager 模式，终端也支持多 WebSocket 连接：

```python
# terminal.py 新增
_connections: dict[str, set[WebSocket]]  # term_id → WebSocket 集合
```

所有连接的客户端都收到 PTY 输出，任一客户端的输入都发送到 PTY。

### 3.4 终端列表 API

新增终端查询和显式销毁的 REST API：

```
GET    /api/workspaces/{wid}/terminals          — 列出活跃终端
DELETE /api/terminals/{term_id}                  — 显式销毁终端
```

### 3.5 前端终端面板改进

- `TerminalPanel` 挂载时：如果有 `terminalId` 配置，先尝试连接已有终端；连接失败（4004）则创建新终端
- 面板标签页关闭时（flexlayout `onRenderTabSet` 或 `onModelChange` 检测 tab 删除）：调用 `DELETE /api/terminals/{term_id}` 销毁 PTY
- WebSocket 断开时：不显示 "disconnected"，而是尝试重连（复用 ReconnectingWebSocket 或自行实现二进制重连）

### 3.6 服务器重启的优雅降级

- 前端尝试连接不存在的终端 → 服务器返回 4004 → 前端自动创建新终端替代
- 布局恢复时终端面板的 `terminalId` 已失效 → 同上处理

## 4. 文件变更

### 修改文件

| 文件 | 变更 |
|------|------|
| `src/mutbot/web/terminal.py` | +scrollback 缓冲，+多连接支持，+`list_by_workspace()`，+`detach()`（替代 WS 断开时的 kill） |
| `src/mutbot/web/routes.py` | WS 断开时调用 `detach()` 而非 `kill()`，+`GET /api/workspaces/{wid}/terminals`，+`DELETE /api/terminals/{term_id}`，重连时发送 scrollback |
| `frontend/src/panels/TerminalPanel.tsx` | 重连逻辑，面板关闭时调用 DELETE API，优雅降级处理 |
| `frontend/src/lib/api.ts` | +`fetchTerminals()`，+`deleteTerminal()` |
| `frontend/src/App.tsx` | 面板关闭回调，检测终端标签删除并调用 deleteTerminal |

## 5. 实施步骤

- [x] **Step 1**: 后端终端生命周期解耦
  - [x] TerminalSession 增加 scrollback 环形缓冲
  - [x] TerminalManager 增加多连接管理（`attach`/`detach`）
  - [x] reader 线程输出同时写入 scrollback 和所有连接
  - [x] `list_by_workspace()` 查询活跃终端
  - 状态：✅ 已完成 (2026-02-23)

- [x] **Step 2**: 路由层适配
  - [x] WS 断开时 `detach()` 替代 `kill()`
  - [x] WS 连接时发送 scrollback 缓冲内容
  - [x] `GET /api/workspaces/{wid}/terminals` 列出终端
  - [x] `DELETE /api/terminals/{term_id}` 显式销毁
  - 状态：✅ 已完成 (2026-02-23)

- [x] **Step 3**: 前端终端面板改进
  - [x] 支持重连已有终端（连接失败则自动新建）
  - [x] 面板关闭时调用 DELETE API
  - [x] WebSocket 断开时尝试重连（指数退避，最多 10 次）
  - 状态：✅ 已完成 (2026-02-23)

## 6. 验证

1. **重连**：打开终端 → 运行命令 → 刷新浏览器 → 终端恢复之前的输出，可继续操作
2. **面板关闭**：关闭终端标签 → 服务端 PTY 进程被清理
3. **网络抖动**：模拟 WS 断开 → 自动重连 → 终端不中断
4. **服务器重启**：重启服务器 → 终端面板自动创建新终端替代
5. **多客户端**：两个浏览器打开同一终端 → 输出同步，任一端输入可见
