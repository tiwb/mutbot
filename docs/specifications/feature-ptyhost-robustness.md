# PTY Host 健壮性改进 设计规范

**状态**：🔄 实施中
**日期**：2026-03-13
**类型**：功能设计

## 背景

ptyhost 守护进程在 Windows 上启动时会打开一个可见的控制台窗口，但窗口中没有任何输出信息。用户不知道这个窗口的用途，可能会手动关闭它。关闭后：

1. 所有 PTY 进程随 ptyhost 一起终止
2. mutbot server 的 `PtyHostClient._receive_loop` 读到 EOF 后静默设置 `_connected = False`
3. 没有任何通知机制告知 TerminalManager 或前端
4. 前端终端卡死（无输出、无法输入），用户误以为整个服务器不可用

**需求**：
1. ptyhost 终端窗口显示 banner 信息，让用户知道不要关闭
2. ptyhost 被 kill 后，mutbot server 正常运行，所有终端显示为 disconnected

## 设计方案

### 改进一：ptyhost 控制台 Banner

**问题**：`_bootstrap.py` 中 `stdout=subprocess.DEVNULL` 导致控制台无输出。

**方案**：
- Windows 平台：移除 `stdout=subprocess.DEVNULL` 和 `stderr=subprocess.DEVNULL`（日志量不大，输出到控制台有助调试）
- `__main__.py`：启动后 `print()` 输出 banner，包含进程用途、端口、关闭警告
- Unix 平台：保持 DEVNULL 不变（无可见窗口，headless 运行）
- Banner 语言：纯英文（避免 Windows 控制台 GBK/UTF-8 编码问题）

Banner 内容：
```
================================================
  MutBot PTY Host
  Listening on 127.0.0.1:{port}

  This process manages all terminal sessions.
  Closing this window will disconnect all terminals.
================================================
```

### 改进二：ptyhost 断开时优雅通知

**当前断开链路**（无通知）：
```
ptyhost 死亡
  → PtyHostClient._receive_loop 读到 EOF
  → self._connected = False（静默）
  → 无人知晓
  → 前端终端卡死
```

**改进后链路**：
```
ptyhost 死亡
  → PtyHostClient._receive_loop 读到 EOF
  → 取消所有 pending futures（ConnectionError）
  → 调用 on_disconnect 回调
  → TerminalManager._on_ptyhost_disconnect()
  → 遍历所有 _known_terms，调用已 attach 的 on_exit(None) 回调
  → 前端收到 {"type": "process_exit"}，终端显示为已退出
  → 清理内部状态（_client=None, _known_terms.clear()）
```

**复用现有机制**：
- `on_exit` 回调已存在，`_terminal_on_connect` 中定义的闭包会发送 `{"type": "process_exit"}` 到前端
- 前端已能处理 `process_exit` 事件
- 只需新增一个 `on_disconnect` 回调串联整个链路

**不做自动重连**：ptyhost 死后旧 PTY 进程不可恢复。用户需要创建新终端。mutbot server 本身不受影响。创建新终端时 `TerminalManager.create()` 检测到未连接会自动调用 `ensure_ptyhost()` 重连。

## 关键参考

### 源码
- `mutbot/src/mutbot/ptyhost/__main__.py` — ptyhost 入口，`main()` 函数
- `mutbot/src/mutbot/ptyhost/_bootstrap.py` — `_spawn_ptyhost()` 使用 DEVNULL 启动
- `mutbot/src/mutbot/ptyhost/_client.py` — `PtyHostClient._receive_loop()` 断开处理
- `mutbot/src/mutbot/runtime/terminal.py` — `TerminalManager.connect()` + `_on_pty_exit()` 回调机制
- `mutbot/src/mutbot/runtime/terminal.py:314-321` — `on_exit` 闭包发送 `process_exit` 到前端

### 相关规范
- `mutbot/docs/specifications/feature-persistent-terminal.md` — ptyhost 原始设计

## 实施步骤清单

### Phase 1: ptyhost 控制台 Banner [✅ 已完成]

- [x] **Task 1.1**: `_bootstrap.py` — Windows 平台移除 stdout/stderr DEVNULL
  - [x] `_spawn_ptyhost()` 中 Windows 分支不再传 `stdout=DEVNULL, stderr=DEVNULL`
  - [x] Unix 分支保持不变
  - 状态：✅ 已完成

- [x] **Task 1.2**: `__main__.py` — 启动后打印 banner
  - [x] `main()` 中绑定端口后、启动 ASGI server 前，`print()` 输出 banner
  - [x] banner 内容包含端口号、用途说明、关闭警告（纯英文）
  - 状态：✅ 已完成

### Phase 2: ptyhost 断开时优雅通知 [✅ 已完成]

- [x] **Task 2.1**: `_client.py` — `PtyHostClient` 新增 `on_disconnect` 回调
  - [x] 新增 `DisconnectCallback` 类型和 `on_disconnect` 回调属性
  - [x] `_receive_loop` 结束时：取消所有 pending futures（设置 ConnectionError）、调用 `on_disconnect`
  - [x] `CancelledError` 路径（正常 close）不触发 `on_disconnect`
  - 状态：✅ 已完成

- [x] **Task 2.2**: `terminal.py` — `TerminalManager` 处理 ptyhost 断开
  - [x] `connect()` 中注册 `client.on_disconnect` 回调
  - [x] 新增 `_on_ptyhost_disconnect()` 方法：遍历所有已知终端，调用已 attach 的 `on_exit(None)` 回调
  - [x] 清理内部状态（`_client = None`、`_connections`、`_client_sizes`、`_known_terms`）
  - [x] 新增 `_reconnect()` 方法：调用 `ensure_ptyhost()` 重连
  - [x] `create()` 中检测 `_client` 为 None 时自动重连
  - 状态：✅ 已完成

### Phase 3: 验证 [待开始]

- [ ] **Task 3.1**: 手动测试
  - [ ] 启动 mutbot，确认 Windows 控制台显示 banner
  - [ ] 手动关闭 ptyhost 窗口，确认前端终端显示为已退出
  - [ ] 关闭后创建新终端，确认 ptyhost 自动重启并正常工作
  - 状态：⏸️ 待开始

## 测试验证
（实施阶段填写）