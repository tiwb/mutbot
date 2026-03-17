# 终端渲染闪烁与滚动异常 设计规范

**状态**：✅ 已完成
**日期**：2025-03-14（完成：2026-03-15）
**类型**：Bug修复

## 背景

当 terminal session 运行较长时间后，终端面板出现内容闪烁和异常滚动。正常使用初期无此问题，随使用时间增长逐渐恶化。

## 症状

### 症状 1：内容上下跳动/闪烁

- 终端内容在上下跳动，视觉上呈现闪烁
- **PC 和手机同时闪烁** → 问题源于服务端
- Claude Code 持续输出时尤为明显
- 刷新页面后依然闪烁

### 症状 2：Scrollback 滚动到最前端

- 偶发终端视口跳到 scrollback 最老数据位置
- 无断线/重连、无 SendBuffer 溢出

### 症状 3：resize 触发闪烁

- 手机输入框展开/收起 → xterm fit → PTY resize（主客户端）
- Claude Code 检测到 resize → 完整重绘 TUI → 大量控制序列到达前端

### 共同条件

- 长时间 session 后出现，短 session 正常
- 刷新页面无法修复

## 调查发现

### 根本原因：SendBuffer 积压 + 原始字节流

实测数据（服务器重启 10 分钟后）：

| 客户端 | 已发送消息 | SendBuffer 积压 |
|--------|-----------|----------------|
| PC | 4,426 条 | **341KB** |
| 手机 | 4,412 条 | **339KB** |

**两个客户端都积压了 ~340KB 数据**。PTY 输出速度超过 WebSocket 送达速度，数据持续积压后突发到达前端，表现为闪烁。

用 raw bytes 转发时此问题**无解** — 终端字节流是顺序的，不能丢弃中间数据（丢了会破坏终端状态），积压只会越来越大。

同时，raw scrollback replay 的问题也存在：
- scrollback 存储原始字节流，包含 TUI 重绘的控制序列
- replay 时控制序列在新上下文中执行产生异常
- 字节截断可能切断 ANSI 序列

### 数据通路

```
PTY process
  → ptyhost reader thread: proc.read(4096)
    → ptyhost ASGI: 逐帧 WebSocket binary
      → mutbot server: _on_pty_output（16ms 批处理）
        → channel.send_binary → SendBuffer → WebSocket → 前端 term.write()
```

### 关键参数

| 参数 | 值 | 说明 |
|------|------|------|
| PTY read chunk | 4096 bytes | ptyhost 每次读取 |
| SendBuffer 上限 | 1000 条 / 1MB | WebSocket 可靠传输缓冲 |
| Claude Code 输出频率 | ~42 msg/sec | 实测值 |
| 服务端批处理窗口 | 16ms / 32KB | 已实施的临时优化 |

## 设计方案

### 已实施的临时优化

1. **PTY 输出批处理** ✅ — `_on_pty_output` 中 16ms 窗口合并，减少包数量
2. **Scrollback 上限提升** ✅ — 64KB → 10MB，减少字节截断频率
3. **Replay 截取+清洗** ✅ — 尾部 256KB 行边界对齐，剥离 TUI 控制序列

这些优化不能根本解决问题 — raw bytes 传输量 = PTY 原始输出量，管道积压时无法降级。

### 根本性修复：pyte diff 模式

**核心思路**：在服务端用 pyte 维护一份终端屏幕状态。不再转发 raw bytes，而是计算屏幕差异（dirty lines），只发送变化的行。所有客户端收相同的 diff 数据。

```
PTY 输出 → feed pyte（维护屏幕状态）
         → 每次 flush 时计算 dirty lines diff
         → 渲染为 ANSI（光标定位 + 变化行内容）→ 发送给所有客户端

连接/重连 → pyte 全屏快照 → 渲染为 ANSI → 发送（替代 scrollback replay）
```

**解决的问题**：

| 问题 | raw bytes | pyte diff |
|------|-----------|-----------|
| 传输量 | = PTY 原始输出量（大） | = 屏幕可见变化量（小） |
| resize 重绘 | 全量 TUI 重绘序列到达前端 | 如果内容没变，diff 为空 |
| 管道积压 | 无法降级，积压→突发→闪烁 | 传输量低，不会积压 |
| scrollback replay | 原始字节流，含脏数据 | 屏幕快照，干净的 ANSI |
| 数据一致性 | 丢帧会破坏终端状态 | 每帧都是完整的 diff，丢了下一帧自动修复 |

**设计要点**：

- **一份 pyte Screen**：所有客户端共享同一份屏幕状态和 diff，不需要 per-client 计算（多尺寸视口留给 `feature-server-side-virtual-terminal`）
- **与批处理合并**：在现有 16ms flush 窗口内，先 feed pyte，再计算 diff，再发送。不增加额外延迟
- **UTF-8 增量解码**：PTY 输出是 bytes，pyte 需要 str。使用 `codecs.getincrementaldecoder("utf-8")` 处理跨 chunk 的不完整字节
- **resize 同步**：PTY resize 成功后同步 resize pyte Screen，确保一致
- **前端改动**：前端从收 raw bytes 改为收 diff ANSI 帧。由于 diff 也是标准 ANSI 序列，xterm.js 仍然用 `term.write()` 即可，前端改动极小
- **fallback**：pyte 不可用时（初始化失败等），退回 raw bytes + scrollback replay

### 实施概要

1. 添加 pyte 依赖
2. `TerminalManager` 中为每个终端创建 `pyte.Screen` + `pyte.Stream` + `codecs` 增量解码器
3. 改造 `_on_pty_output` flush 路径：feed pyte → 计算 dirty diff → 渲染 ANSI → 广播（替代直接广播 raw bytes）
4. `resize` 成功后同步 resize pyte Screen
5. `on_connect` 中用 pyte 全屏快照替代 scrollback replay
6. 实现 `render_dirty_as_ansi(screen)` 和 `render_screen_as_ansi(screen)` 工具函数

## 实施步骤清单

### Phase 1: pyte 集成与 ANSI 渲染器 [完成]

- [x] **Task 1.1**: 添加 pyte 依赖
  - [x] `pyproject.toml` 中添加 pyte 依赖
  - 状态：✅ 完成

- [x] **Task 1.2**: 实现 ANSI 渲染器模块
  - [x] 新建 `mutbot/src/mutbot/runtime/ansi_render.py`
  - [x] `render_dirty(screen) -> bytes`：遍历 dirty lines，输出光标定位 + SGR 属性 + 字符内容 + 行尾清除，清空 dirty set
  - [x] `render_full(screen) -> bytes`：全屏渲染（清屏 + 所有行），用于连接快照
  - [x] SGR 属性映射：前景色/背景色（标准色 + RGB）、粗体、斜体、下划线、删除线、反显
  - [x] 宽字符（CJK）跳过 pyte 占位列
  - 状态：✅ 完成

### Phase 2: TerminalManager pyte 集成 [完成]

- [x] **Task 2.1**: TerminalManager 中维护 per-terminal pyte 实例
  - [x] 添加 `_screens`、`_streams`、`_decoders` 字典
  - [x] `create()` 时创建 Screen + Stream + 增量解码器（大小 = PTY 初始大小）
  - [x] `kill()` 和 `_on_ptyhost_disconnect()` 时清理
  - 状态：✅ 完成

- [x] **Task 2.2**: 改造 `_flush_output` 为 pyte diff 广播
  - [x] flush 时：增量解码 bytes → str，feed 到 pyte Stream
  - [x] 计算 dirty diff（`render_dirty`），广播 diff ANSI 给所有客户端
  - [x] 无 dirty lines 时不发送（如 resize 后内容未变）
  - [x] pyte 不可用时 fallback 到 raw bytes
  - 状态：✅ 完成

- [x] **Task 2.3**: resize 同步
  - [x] `resize()` 成功后调用 `screen.resize(rows, cols)`
  - 状态：✅ 完成

- [x] **Task 2.4**: `on_connect` 快照替代 scrollback replay
  - [x] 用 `render_full(screen)` 生成全屏快照
  - [x] 替代原有的 `get_scrollback()` + `_strip_replay_sequences` + `_truncate_replay` 路径
  - [x] 保留 scrollback fallback（pyte Screen 不存在时）
  - 状态：✅ 完成

### Phase 3: 验证与最终修复 [完成]

- [x] **Task 3.1**: 功能验证
  - [x] 终端基本交互正常（输入、输出、颜色、光标）
  - [x] Claude Code TUI 显示正确
  - [x] 连接/刷新后屏幕快照正确恢复
  - [x] resize 后屏幕正常
  - 状态：✅ 完成

- [x] **Task 3.2**: 闪烁验证与最终修复
  - [x] pyte diff 模式显著减少闪烁但未完全消除
  - [x] 根因定位：render_dirty 大帧（4000+ bytes 多行重绘）被 xterm.js 分帧处理，显示新旧内容混合态
  - [x] 修复：所有 ANSI 渲染函数使用 Synchronized Update (DEC Mode 2026) — BSU `\x1b[?2026h` / ESU `\x1b[?2026l` 包裹输出，xterm.js 暂停渲染直到 ESU 一次性刷新
  - [x] 用户验证：闪烁彻底消除
  - 状态：✅ 完成

### 最终根因总结

闪烁有两层原因，需要两个方案叠加解决：

1. **传输层闪烁**（pyte diff 解决）：raw bytes 转发时，PTY 输出按 ~100 bytes 小块到达前端，xterm.js 逐块渲染导致中间态可见
2. **渲染层闪烁**（BSU/ESU 解决）：pyte `render_dirty()` 的"整行重写"策略对多行重绘产生 4000+ bytes 大帧，xterm.js 为避免阻塞主线程分帧处理，部分行更新完毕而其余行未更新的混合态可见

**为什么 VS Code 不闪烁**：VS Code 的终端 PTY 在同一进程内，数据直接传给 xterm.js。应用自身的增量更新数据量小，且应用自己发出的 BSU/ESU 标记完整包含在单次 write 中。而 pyte 消费了原始 PTY 输出中的 BSU/ESU 序列，必须在 render_dirty 输出中重新添加。

### 已发现的性能问题

- **scrollback replay 性能**：服务器重启后首次连接需从 ptyhost 获取 scrollback 并通过 pyte 重新 replay。长 session 累积 10MB scrollback → pyte feed 耗时 28.5 秒。优化方向：限制 scrollback 大小、按需加载历史、持久化 pyte 屏幕状态。

## 关键参考

### 源码

- `mutbot/src/mutbot/runtime/terminal.py` — TerminalManager（`_on_pty_output` 批处理、`_flush_output` 广播、`on_connect` replay）
- `mutbot/src/mutbot/ptyhost/_manager.py` — ptyhost scrollback（保留作为 fallback）
- `mutbot/src/mutbot/web/transport.py` — WebSocket SendBuffer 限制
- `mutbot/frontend/src/panels/TerminalPanel.tsx` — 前端终端面板

### 相关规范

- `mutbot/docs/specifications/feature-server-side-virtual-terminal.md` — 后续多尺寸视口渲染（per-client pyte Screen）

### 外部依赖

- [pyte](https://github.com/selectel/pyte) — 纯 Python VT100 终端模拟器，零外部依赖，HistoryScreen 支持 scrollback（LGPL-3.0）
