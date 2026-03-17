# 终端滚动后实时输出叠加 设计规范

**状态**：✅ 已完成
**日期**：2026-03-17
**类型**：Bug修复

## 背景

用户报告：终端屏幕滚动后，实时输出仍输出在当前屏幕位置，导致信息叠加。

## 根因分析

### 问题链路

```
mutbot 重启 → TerminalManager 重建 → _view_ids 清空
           → on_connect 为每个终端创建新 view
           → ptyhost 未重启，旧 view 仍在 → view 累积泄漏
           → N 个 view 全部 scroll_offset=0
           → 用户滚动只改 _view_ids 指向的那 1 个 view
           → 其余 N-1 个泄漏 view 仍为 live → _do_render_term 继续推帧
           → _on_pty_frame 按 term_id 广播 → 前端收到不该收到的 live 帧
           → 叠加在滚动视图上
```

### 日志证据

重启前 Terminal 1 有 2 views，重启后迅速累积到 3-4 views：

```
[重启前] Large frame: c2f4345a 11.7KB (63 dirty lines, 2 views)
[重启后] Large frame: c2f4345a 11.3KB (63 dirty lines, 3 views)
         Large frame: d1c855ed 10.9KB (63 dirty lines, 4 views)
```

注：日志中 "N views" 是 `sum(v.scroll_offset == 0 for v in views)` —— 即 live view 数量。

### 根本缺陷

Phase 1 "共享 view" 设计存在两个根本问题：

1. **view 生命周期无锚点**：共享 view 绑定到终端而非客户端连接，mutbot 重启后无法清理 ptyhost 中的旧 view
2. **帧路由无过滤**：`_on_pty_frame` 按 term_id 广播，不检查 view_id，泄漏 view 的帧送达所有客户端

## 设计方案：Per-Client View

将 Phase 1 "共享 view" 升级为 per-client view：每个客户端连接拥有独立的 view，生命周期绑定到连接（connect 创建、disconnect 销毁），彻底消除泄漏。

### 数据结构变更

```python
# Phase 1（当前）
_view_ids: dict[str, str]  # {term_id: view_id}  — 一个终端一个 view

# Per-client view（目标）
_client_views: dict[str, dict[str, str]]  # {term_id: {client_id: view_id}}
```

### 核心改动

#### 1. `_on_pty_frame`：按 view_id 路由

```python
def _on_pty_frame(self, term_id: str, view_id: str, frame: bytes) -> None:
    conns = self._connections.get(term_id)
    if not conns:
        return
    # 找到拥有该 view 的 client
    views = self._client_views.get(term_id, {})
    for client_id, vid in views.items():
        if vid == view_id:
            cb = conns.get(client_id)
            if cb:
                try:
                    cb[0](frame)  # on_output
                except Exception:
                    logger.warning("send_binary failed for client %s", client_id[:8], exc_info=True)
            break
```

#### 2. `on_connect`：创建 per-client view

```python
# 为该客户端创建独立 view
view_id = await tm._client.create_view(term_id)
tm._client_views.setdefault(term_id, {})[client_id] = view_id
```

#### 3. `on_disconnect`：销毁 per-client view

```python
# 销毁该客户端的 view
views = tm._client_views.get(term_id, {})
view_id = views.pop(client_id, None)
if view_id and tm._client:
    asyncio.ensure_future(tm._client.destroy_view(view_id))
```

#### 4. scroll 命令：使用 per-client view_id

```python
# 从 client_views 取当前客户端的 view_id
view_id = tm._client_views.get(term_id, {}).get(client_id)
```

#### 5. `TerminalManager.create()`：不再创建初始 view

终端创建时不创建 view，view 完全由 on_connect/on_disconnect 管理。

#### 6. `TerminalManager.kill()`：清理所有 client view

```python
views = self._client_views.pop(term_id, None)
if views and self._client:
    for vid in views.values():
        asyncio.ensure_future(self._client.destroy_view(vid))
```

### 获取 client_id 的方式

on_connect 中已有获取 client_id 的代码（`terminal.py:389`）：

```python
from mutbot.web.transport import ChannelTransport
ext = ChannelTransport.get(channel)
client_id = ext._client.client_id if ext and ext._client else ""
```

on_message 中需要同样获取 client_id，用于路由 scroll 命令到正确的 view。可从 `ChannelContext` 或 channel 上获取。

### 用户输入时自动回底

当前 `on_data`（`terminal.py:580-583`）使用 `_view_ids` 获取 view_id。改为从 `_client_views` 获取对应客户端的 view_id。

### 需要注意的边界情况

1. **client_id 为空**：ChannelTransport 未就绪时 client_id 可能为空字符串，需要 fallback
2. **on_binary_resume 回调**：快照请求需要使用对应客户端的 view_id
3. **broadcast_json 不受影响**：scroll_state 等 JSON 消息仍可广播（每个客户端会根据自己的 scroll 状态请求更新）

## 关键参考

### 源码

- `mutbot/src/mutbot/ptyhost/_manager.py:473-492` — `create_view` / `destroy_view`（ptyhost 侧已支持多 view）
- `mutbot/src/mutbot/runtime/terminal.py:48-61` — `TerminalManager.__init__` 数据结构
- `mutbot/src/mutbot/runtime/terminal.py:95-104` — `create()` 创建终端+view
- `mutbot/src/mutbot/runtime/terminal.py:107-119` — `kill()` 清理
- `mutbot/src/mutbot/runtime/terminal.py:266-279` — `_on_pty_frame` 帧路由
- `mutbot/src/mutbot/runtime/terminal.py:384-426` — `on_connect` view 创建
- `mutbot/src/mutbot/runtime/terminal.py:445-465` — `on_disconnect`
- `mutbot/src/mutbot/runtime/terminal.py:467-513` — `on_message` scroll 处理
- `mutbot/src/mutbot/runtime/terminal.py:540-585` — `on_data` 用户输入+auto scroll

### 相关规范

- `mutbot/docs/specifications/feature-pyte-frameskip-scroll.md` — 跳帧渲染与服务端滚动设计

## 实施步骤清单

### Phase 1: TerminalManager 数据结构 [✅ 已完成]

- [x] **Task 1.1**: 替换 `_view_ids` 为 `_client_views`
  - `__init__` 中 `_view_ids: dict[str, str]` → `_client_views: dict[str, dict[str, str]]`
  - 新增 `_connect_lock` 防止并发重连
  - 状态：✅ 已完成

### Phase 2: view 生命周期绑定到客户端连接 [✅ 已完成]

- [x] **Task 2.1**: `create()` 不再创建初始 view
  - 状态：✅ 已完成

- [x] **Task 2.2**: `on_connect` 创建 per-client view
  - 状态：✅ 已完成

- [x] **Task 2.3**: `on_disconnect` 销毁 per-client view
  - 状态：✅ 已完成

- [x] **Task 2.4**: `kill()` 清理所有 client view
  - 状态：✅ 已完成

### Phase 3: 帧路由按 view_id [✅ 已完成]

- [x] **Task 3.1**: `_on_pty_frame` 按 view_id 路由到对应 client
  - 状态：✅ 已完成

### Phase 4: scroll/data 命令使用 per-client view_id [✅ 已完成]

- [x] **Task 4.1**: `on_message` 中 scroll/scroll_to/scroll_to_bottom/clear_scrollback 使用 per-client view_id
  - scroll_state 改为 `channel.send_json` 仅发给当前客户端
  - 状态：✅ 已完成

- [x] **Task 4.2**: `on_data` 中自动回底使用 per-client view_id
  - 状态：✅ 已完成

- [x] **Task 4.3**: `on_binary_resume` 回调使用 per-client view_id
  - 状态：✅ 已完成

### Phase 5: 清理与验证 [✅ 已完成]

- [x] **Task 5.1**: 删除 `_view_ids` 所有残留引用
  - 状态：✅ 已完成

- [x] **Task 5.2**: 重启验证
  - 重启 mutbot + 杀掉 ptyhost，验证无 view 泄漏，滚动不再叠加
  - 状态：✅ 已完成
