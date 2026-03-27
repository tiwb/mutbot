# 刷新时 Auto Resize 抢占修复

**状态**：✅ 已完成
**日期**：2026-03-17
**类型**：Bug修复

## 背景

服务器热重启触发前端 reload 时，多个客户端同时重连，每个客户端都在收到 `ready` 消息后立即发送 resize，导致 Auto 模式下 PTY 尺寸被反复覆盖（"抢来抢去"）。

### 问题链路

1. 客户端 A（PC 108x63）连接 → ready → sendResize(63, 108) → PTY resize → broadcast pty_resize
2. 客户端 B（手机 40x50）连接 → ready → sendResize(40, 50) → PTY resize → broadcast pty_resize
3. 客户端 A 收到 pty_resize(40, 50) → xterm.resize → ResizeObserver → sendResize(63, 108) → 循环

### 根本原因

- 前端在 `ready` 时**无条件**发送 resize（`TerminalPanel.tsx:232`）
- Auto 模式下无 `last_input_client` 时，`_get_resize_controller()` 返回 None → 任何客户端都能 resize PTY
- 连接时不应改变 PTY 尺寸 — Auto resize 的语义是"谁在用就跟谁的尺寸"，连接不等于"在用"

## 设计方案

### 核心设计

**原则：Auto resize 仅在输入时触发，连接时只注册尺寸。**

1. **前端**：`ready` 时不发 `resize`，改发 `register_size`（仅注册客户端尺寸）
2. **后端**：新增 `register_size` 消息处理 — 存入 `_client_sizes` + 设置 viewport，但不 resize PTY
3. **不影响已有行为**：
   - 用户输入时，`on_data` 中 `last_input_client` 变更仍触发 auto resize（已有逻辑）
   - `follow_me` 模式不受影响
   - ResizeObserver（窗口拖拽）仍正常发送 `resize`（用户主动操作）

### 实施概要

前端 `TerminalPanel.tsx` ready 处理改为发 `register_size`；后端 `terminal.py` on_message 增加 `register_size` 处理（复用 resize 的存储逻辑，跳过 PTY resize）。改动量很小。

## 实施步骤清单

- [x] **Task 1**: 前端 — ready 时发 `register_size` 替代 `resize`
  - [x] `TerminalPanel.tsx` ready 处理中，将 `sendResize()` 改为发送 `{ type: "register_size", rows, cols }`
  - 状态：✅ 已完成

- [x] **Task 2**: 后端 — on_message 增加 `register_size` 处理
  - [x] `terminal.py` on_message 中新增 `register_size` 分支：存入 `_client_sizes` + 设置 viewport，不调用 `tm.resize()`
  - 状态：✅ 已完成

- [x] **Task 3**: 构建验证
  - [x] 前端 build 通过
  - [x] 重启服务器，多客户端重连时不再互相抢 resize
  - 状态：✅ 已完成

## 关键参考

### 源码
- `frontend/src/panels/TerminalPanel.tsx:220-235` — ready 消息处理，无条件 sendResize
- `frontend/src/panels/TerminalPanel.tsx:176-184` — sendResize 函数
- `src/mutbot/runtime/terminal.py:178-218` — resize() 方法，controller 决策逻辑
- `src/mutbot/runtime/terminal.py:233-238` — _get_resize_controller()，follow_me > last_input_client > None
- `src/mutbot/runtime/terminal.py:489-510` — on_message resize 处理
- `src/mutbot/runtime/terminal.py:597-620` — on_data 中 auto resize 逻辑（last_input_client 变更时触发）
