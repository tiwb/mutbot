# 清除终端滚动历史 设计规范

**状态**：✅ 已完成
**日期**：2026-03-17
**类型**：功能设计

## 背景

当前"Clear Terminal"操作只清除前端 xterm.js 的 DOM 缓冲，服务端 pyte 的 scrollback 历史不受影响。刷新或滚动后历史内容又全部恢复。

实际需求：清除服务端 pyte 的 scrollback history buffer，释放内存，让终端"重新开始"。

## 设计方案

### 数据流

```
前端 "清除历史" 菜单点击
  → channel message: {"type": "clear_scrollback"}
  → terminal.py handler
  → PtyHostClient.clear_scrollback(term_id)
  → ptyhost _manager.py: screen.history.top.clear() + 重置所有 view scroll_offset
  → 向所有连接的前端推送：全量帧 + scroll_state（offset=0, total=visible）
  → 前端 xterm clear + 重写当前屏幕内容
```

### 各层实现

**ptyhost `_manager.py`**：新增 `clear_scrollback(term_id)` 方法
- `screen.history.top.clear()` — 清除 pyte 历史缓冲
- 遍历该 term 的所有 TermView，`scroll_offset = 0` — 重置滚动位置
- 触发一次全量渲染推送（让所有客户端刷新）

**ptyhost `_client.py`**：新增 `clear_scrollback(term_id)` 命令
- 发送 `{"cmd": "clear_scrollback", "term_id": term_id}` 到 ptyhost 进程

**`runtime/terminal.py`**：新增 `"clear_scrollback"` 消息处理
- 收到前端消息后调用 `tm._client.clear_scrollback(term_id)`
- 完成后向所有连接的 channel 广播 `scroll_state`（offset=0）

**前端 `TerminalPanel.tsx`**：修改菜单项
- 标签改为"Clear History"（或保持"Clear Terminal"）
- `onClick` 改为发送 channel message `{"type": "clear_scrollback"}`，不再调用 `termRef.current?.clear()`
- 前端收到清除后的全量帧时 xterm 自然会更新显示

### `scrollback_b64` 持久化

当前 `scrollback_b64` 字段未实际使用（只在 restart 时清零），无需额外处理。清除 scrollback 不影响持久化逻辑。

## 关键参考

### 源码
- `mutbot/src/mutbot/ptyhost/_manager.py:580-672` — scroll 方法和 scroll_state 计算
- `mutbot/src/mutbot/ptyhost/_manager.py:105` — history=50000 创建 screen
- `mutbot/src/mutbot/ptyhost/_client.py:268-282` — scroll 命令发送
- `mutbot/src/mutbot/runtime/terminal.py:467-541` — 消息处理（scroll/resize 等）
- `mutbot/frontend/src/panels/TerminalPanel.tsx:680-684` — 当前 Clear Terminal 菜单
- `mutbot/frontend/src/panels/TerminalPanel.tsx:250` — scroll_state 消息处理

## 实施步骤清单

- [x] **Task 1**: ptyhost `_manager.py` — 新增 `clear_scrollback(term_id)` 方法
  - 状态：✅ 已完成

- [x] **Task 2**: ptyhost `_client.py` — 新增 `clear_scrollback(term_id)` 命令
  - 状态：✅ 已完成

- [x] **Task 3**: `runtime/terminal.py` — 新增 `"clear_scrollback"` 消息处理 + 广播 scroll_state
  - 状态：✅ 已完成

- [x] **Task 4**: 前端 `TerminalPanel.tsx` — 修改菜单项，发送 clear_scrollback 消息
  - 状态：✅ 已完成

- [x] **Task 5**: 构建前端 + 验收测试
  - 状态：✅ 已完成
