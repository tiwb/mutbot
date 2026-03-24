# Terminal 纯光标移动不更新 设计规范

**状态**：✅ 已完成
**日期**：2026-03-24
**类型**：Bug修复

## 背景

Terminal 在 PTY 输出仅包含光标移动序列（如 `\x1b[10;20H`）而无内容变更时，前端不会收到更新帧，导致光标位置不刷新。

典型场景：
- 命令行编辑器（vim/nano）中用方向键移动光标
- shell 中按左右键移动光标（readline）
- 程序输出纯光标定位序列

## 问题分析

### 数据流

```
PTY 输出 → pyte.stream.feed() → screen.cursor 更新 ✓
                                → screen.dirty 为空 ✗
                                        ↓
           _manager.py:353 检查 dirty → 跳过
           ansi_render.py:122 检查 dirty → 返回 b""
                                        ↓
           不发 WebSocket 帧 → 前端光标不动
```

### 根因

pyte 的 `screen.dirty` 只追踪**内容变更的行号**。光标移动序列只更新 `screen.cursor.x/y`，不会将任何行加入 dirty 集合。渲染管线有两处门控依赖 dirty 非空：

1. `_manager.py:353` — `if has_live and term.screen.dirty:` 控制是否标记 render pending
2. `ansi_render.py:122` — `if not screen.dirty: return b""` 控制是否生成帧

## 设计方案

### 核心设计

在 `_flush_and_feed()` 中检测光标位置变化，当光标移动但 dirty 为空时，生成一个**轻量光标帧**（只含光标定位序列，不含行内容）。

**方案要点**：

1. feed 前记录 `(cursor.x, cursor.y)`，feed 后比较
2. 如果光标位置变了但 `screen.dirty` 为空，走单独的「光标帧」路径
3. 光标帧极小（约 20 bytes），不需要 BSU/ESU 同步更新包裹
4. 如果 dirty 非空，走原有 `render_dirty()` 路径（已包含光标定位）

**选择此方案的理由**：
- 改动最小，只在 `_flush_and_feed` 增加光标比较逻辑
- 不改变 `render_dirty()` 的职责和行为
- 光标帧开销极低，无性能担忧
- 前端（xterm.js）天然支持纯光标定位序列，无需改动

### 光标帧格式

```
\x1b[?25l          隐藏光标（避免移动过程中的闪烁）
\x1b[{row};{col}H  移动光标
\x1b[?25h          显示光标
```

约 15-20 bytes，直接作为二进制帧推送。

### 实施概要

在 `_flush_and_feed()` 的 feed 前后比较光标位置，dirty 为空但光标变了时生成轻量帧并推送。新增一个 `_emit_cursor_frame()` 方法处理帧生成和分发。

## 已确认决策

- **节流**：不需要额外节流，现有 16ms+300ms 缓冲天然合并连续光标移动
- **BSU 期间**：不需要特殊处理，BSU return 门控已覆盖

## 实施步骤清单

- [x] **Task 1**: `_flush_and_feed()` 增加光标位置比较
  - [x] feed 前记录 `(cursor.x, cursor.y)`
  - [x] feed 后在 dirty 检查分支中增加光标变化判断：dirty 为空但光标变了 → 走光标帧路径
  - 状态：✅ 已完成

- [x] **Task 2**: 新增 `_emit_cursor_frame()` 方法
  - [x] 生成轻量光标帧（隐藏光标 + 定位 + 显示光标，~20 bytes）
  - [x] 遍历 live views 推送帧（viewport 模式的 view 也需要推送）
  - 状态：✅ 已完成

- [x] **Task 3**: `_force_end_sync()` 补充光标检查
  - [x] BSU 超时后如果 dirty 为空但光标变了，也应发光标帧
  - 状态：✅ 已完成

- [x] **Task 4**: 验证测试
  - [x] 启动 mutbot，在 terminal 中用方向键移动光标，确认光标实时跟随
  - [x] vim/nano 中移动光标验证
  - [x] 快速按住方向键验证无闪烁/卡顿
  - 状态：✅ 已完成

## 关键参考

### 源码
- `src/mutbot/ptyhost/_manager.py:294-360` — `_flush_and_feed()` 主逻辑，dirty 检查门控在 353
- `src/mutbot/ptyhost/_manager.py:385-410` — `_do_render_term()` 渲染入口
- `src/mutbot/ptyhost/_manager.py:410-430` — `_on_frame` 回调分发帧到 views
- `src/mutbot/ptyhost/ansi_render.py:117-142` — `render_dirty()` 帧生成，dirty 为空时返回 b""
- `src/mutbot/ptyhost/_screen.py` — `_SafeHistoryScreen` 继承 pyte.HistoryScreen
