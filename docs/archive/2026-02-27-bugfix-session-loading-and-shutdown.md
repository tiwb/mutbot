# Session 加载优化与关闭报错修复 设计规范

**状态**：✅ 已完成
**日期**：2026-02-27
**类型**：Bug修复 / 重构

## 1. 背景

### 问题现象

1. **加载过多 session**：服务器启动时加载磁盘上全部 42 个 session JSON 文件到内存，包括已删除的 7 个。关闭时对全部 42 个 session 执行 `stop()`，日志冗长且浪费。
2. **退出 traceback**：Ctrl+C 退出时 Python 3.14 的 `asyncio.Runner` 将 CancelledError 重抛为 KeyboardInterrupt，`__main__.py` 未捕获导致打印完整 traceback。

### 架构问题

当前 session 删除机制存在设计缺陷：

- **创建时**：`ws.sessions.append(session.id)` + `sm.create()` — workspace 列表和 session 文件双写
- **删除时**：只设 `session.deleted = True`，**从未从 `ws.sessions` 列表中移除** session ID
- **加载时**：`load_all_sessions()` 扫描全部 JSON 文件，依赖 `session.deleted` 标记过滤
- **列表查询**：`list_by_workspace()` 遍历全部 `_sessions`，用 `workspace_id` + `not s.deleted` 双重过滤

Workspace 的 `sessions` 列表本应是 session 归属的权威来源，但 `deleted` 标记的引入让两套数据产生了冗余和不一致。

## 2. 设计方案

### 2.1 移除 Session.deleted，workspace.sessions 作为唯一权威源

- 删除 `Session.deleted` 字段
- 删除 session = 从 `ws.sessions` 列表移除 + 从 `_sessions` 字典移除
- 磁盘上的 session JSON 文件保留不删除（便于数据恢复）

### 2.2 按需加载：workspace.sessions 驱动

启动时加载顺序：
1. `workspace_manager.load_from_disk()` — 加载所有 workspace
2. 收集所有 workspace 的 `sessions` 列表，得到需要加载的 session ID 集合
3. `session_manager.load_from_disk(session_ids)` — 只加载这些 session

`load_all_sessions()` 保留（兼容），新增 `load_sessions(session_ids)` 按 ID 集合加载。

### 2.3 delete 操作同步 workspace

`handle_session_delete` 和 menu action handler 的 `session_deleted` 处理流程：
1. `sm.stop(session_id)` — 停止 runtime
2. `sm.delete(session_id)` — 从 `_sessions` 字典移除
3. 从 `ws.sessions` 列表移除 session ID
4. `wm.update(ws)` — 持久化 workspace
5. 广播 `session_deleted` 事件

### 2.4 list_by_workspace 简化

由于 `_sessions` 中只有被 workspace 引用的 session，直接按 `workspace_id` 过滤即可，移除 `not s.deleted` 判断。

### 2.5 shutdown 只 stop 有 runtime 的 session

`_shutdown_cleanup()` 改为遍历 `_runtimes` 而非 `_sessions`。

### 2.6 __main__.py 捕获 KeyboardInterrupt

`server.run()` 外包 `try/except KeyboardInterrupt: pass`。

## 4. 实施步骤清单

### 阶段一：Session 删除流程修正 [✅ 已完成]
- [x] **Task 1.1**: 删除操作同步 workspace — 修改 `routes.py` 的 `handle_session_delete` 和 menu action handler，删除 session 时从 `ws.sessions` 移除并持久化 workspace
  - 状态：✅ 已完成
- [x] **Task 1.2**: `SessionManager.delete()` 改为从 `_sessions` 字典移除（不再设 deleted 标记）
  - 状态：✅ 已完成

### 阶段二：加载优化 [✅ 已完成]
- [x] **Task 2.1**: `storage.load_sessions(session_ids)` — 新增按 ID 集合加载的函数
  - 状态：✅ 已完成
- [x] **Task 2.2**: `SessionManager.load_from_disk(session_ids)` — 改为接收 session ID 集合参数
  - 状态：✅ 已完成
- [x] **Task 2.3**: `server.py` lifespan 调整加载顺序 — 先加载 workspace，收集 session ID，再加载 session
  - 状态：✅ 已完成

### 阶段三：清理与收尾 [✅ 已完成]
- [x] **Task 3.1**: 移除 `Session.deleted` 字段及所有引用（session.py、session_impl.py 的序列化/反序列化/过滤）
  - 状态：✅ 已完成
- [x] **Task 3.2**: `_shutdown_cleanup()` 改为只遍历 `_runtimes`
  - 状态：✅ 已完成
- [x] **Task 3.3**: `__main__.py` 捕获 KeyboardInterrupt
  - 状态：✅ 已完成

## 5. 测试验证

### 手动测试
- [x] 启动服务器，确认加载的 session 数量与 workspace.sessions 一致
- [x] 创建 session → 删除 session → 重启，确认已删除的 session 不再加载
- [x] Ctrl+C 退出无 traceback
- [x] 关闭日志只包含有 runtime 的 session
