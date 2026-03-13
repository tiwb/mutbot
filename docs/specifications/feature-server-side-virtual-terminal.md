# 服务端虚拟终端 设计规范

**状态**：📝 设计中
**日期**：2026-03-13
**类型**：功能设计

## 背景

### 问题 1：终端闪烁

终端 scrollback 积累较大后，用户在正常使用过程中会看到终端闪烁（清屏重绘或内容跳动）。闪烁在 session 使用较长时间后、scrollback 接近上限时开始频繁出现。**主客户端和非主客户端都会闪烁**。

已确认的触发因素之一：WebSocket 断连重连时，`on_connect` 发送 `_CLEAR_SCREEN + 完整 scrollback`（最大 64KB）造成清屏→从头重绘。日志证据（`server-20260313_100445`）显示每秒一次的断连重连循环。

但断连重连**不是唯一原因**——主客户端在未断连时也会闪烁。可能的其他因素：
- 实时数据流中大量数据突发到达，xterm.js 集中渲染导致跳动
- PTY 输出中包含清屏/光标跳转序列
- scrollback 满时的 bytearray 裁剪（`del _scrollback[:overflow]`）对输出流的影响
- 手机端激活输入导致 PTY resize → scrollback history 增长 → 更容易触发

**根因待进一步确认**：需要在闪烁发生时抓取实时日志和消息数据。
26
### 问题 2：多尺寸客户端显示

多个客户端（桌面 100×30、手机 40×20）连接同一终端时，PTY 只有一个尺寸。非主客户端的 xterm 被强制 resize 到 PTY 尺寸后，超出容器的部分被裁剪，最新内容（底部）不可见，且无法滚动查看完整内容。

### 现有方案的局限

`feature-terminal-resize-control` 已实现主客户端优先策略，解决了"谁控制 PTY 尺寸"的问题。但 PTY 输出仍然以 raw bytes 广播给所有客户端，非主客户端的显示体验受限于 PTY 尺寸与自身容器的不匹配。

## 设计方案

### 核心思路

在服务端引入终端模拟器（pyte），解析 PTY 输出为结构化的屏幕缓冲区。主客户端仍接收 raw bytes（零开销），非主客户端接收服务端裁剪的视口更新。连接/重连时统一发送屏幕快照替代全量 scrollback replay。

```
PTY (cols×rows, 由主客户端决定)
  ↓ raw bytes
pyte.HistoryScreen (服务端解析，维护屏幕 buffer + scrollback)
  ↓
  ├→ 主客户端: 直接转发 raw bytes（xterm 尺寸 = PTY 尺寸，完全匹配）
  └→ 非主客户端: 裁剪视口 → 渲染 ANSI → 发送
```

### 已确认的决策

- **pyte 作为终端模拟器**：零外部依赖，纯 Python，VT100/VT220 完整支持，1300+ 用户的成熟项目（LGPL-3.0）。性能先实测，不够再优化。
- **合并实施**：不分 Phase 1/2，一步完成 pyte 集成 + 视口渲染（分阶段的临时方案会被立即替换，增加总工作量）。
- **渲染格式为 ANSI 序列**：服务端将屏幕状态渲染为 ANSI/VT 转义序列，前端 xterm.js 直接 `write()` 即可。不引入新的结构化 JSON 渲染协议，前端改动最小。
- **使用 HistoryScreen**：Phase 1 直接用 `pyte.HistoryScreen`（继承 Screen，API 一致），提前积累 scrollback 历史，为后续视口滚动做准备。

### 主客户端 vs 非主客户端

| | 主客户端 | 非主客户端 |
|---|---|---|
| xterm 尺寸 | = PTY 尺寸（fitAddon 正常） | = 自身容器（fitAddon 正常） |
| 实时数据 | raw bytes 直接转发（不变） | 服务端视口渲染 → ANSI |
| 连接/重连 | pyte 屏幕快照（~2-5KB） | pyte 视口快照（更小） |
| 性能开销 | 无额外开销 | 服务端裁剪 + 渲染 |

主客户端优先策略保持不变（`feature-terminal-resize-control`）。多数时间只有一个客户端，视口渲染的性能开销只在多客户端时产生。

### 数据流改造

#### `_on_pty_output`（实时输出）

```python
def _on_pty_output(self, term_id: str, data: bytes) -> None:
    # 1. feed 到 pyte 维护屏幕状态
    stream = self._streams.get(term_id)
    if stream:
        stream.feed(data.decode("utf-8", errors="replace"))

    # 2. 分流广播
    primary = self._primary_client.get(term_id)
    for client_id, (on_output, _) in conns.items():
        if client_id == primary:
            on_output(data)                    # 主客户端：raw bytes
        else:
            viewport_data = self._render_viewport(term_id, client_id)
            if viewport_data:
                on_output(viewport_data)       # 非主客户端：视口 ANSI
```

#### `on_connect`（连接/重连）

```python
# 替代原来的 get_scrollback() + _CLEAR_SCREEN + scrollback replay
screen = self._screens.get(term_id)
if screen:
    snapshot = render_screen_as_ansi(screen, viewport_rows, viewport_cols)
    channel.send_binary(_CLEAR_SCREEN + snapshot)
```

快照数据量：当前屏幕的可见行渲染为 ANSI（典型 < 5KB），vs 原来的 64KB raw scrollback。即使 WebSocket 每秒重连，闪烁也大幅减轻。

### 视口模型

```
┌──────────── PTY 屏幕 (100 cols × 30 rows) ────────────┐
│                                                        │
│    ┌──── 客户端 B 视口 (40×20) ────┐                   │
│    │                               │                   │
│    │  用户看到的区域                │                   │
│    │                               │                   │
│    └───────────────────────────────┘                   │
│                                                        │
│                                        光标 ▌          │
└────────────────────────────────────────────────────────┘
```

**裁剪规则**（初版）：
- 行方向：取底部 N 行（最新输出可见）
- 列方向：取左侧 N 列（命令行起始可见）
- 视口滚动暂不实现，后续按需扩展

**视口 ANSI 渲染**：
- 只渲染 pyte `dirty` 标记的行与视口的交集
- 用 `\x1b[row;1H`（光标定位）+ 行内容写入
- 不清屏，原地覆盖，避免闪烁

### 节流策略

高频输出时（如 `cat` 大文件），非主客户端的视口渲染不宜每个 PTY chunk 都触发。采用简单节流：
- 合并一段时间内的 dirty lines，每 50-100ms 发一次视口帧
- 主客户端不受影响（raw bytes 直接转发）

### pyte 集成位置

在 `TerminalManager` 中为每个终端维护 pyte 实例：

```python
class TerminalManager:
    self._screens: dict[str, pyte.HistoryScreen] = {}
    self._streams: dict[str, pyte.Stream] = {}
```

生命周期：
- `create()` 时创建 Screen + Stream（大小 = PTY 初始大小）
- `resize()` 成功后同步 resize pyte Screen
- `kill()` 时清理

### 与现有 resize 控制的关系

`feature-terminal-resize-control` 的主客户端优先策略保持不变：
- 主客户端决定 PTY 尺寸
- pyte Screen 以 PTY 尺寸初始化和 resize
- 非主客户端通过视口裁剪查看，xterm 始终 fit 自己的容器
- `TerminalPanel.tsx` 中的 `fitPaused` 机制可以移除——每个客户端的 xterm 始终 fit 容器，服务端负责裁剪

### 性能考量

- pyte 是纯 Python，解析大量输出（如 `cat` 大文件）时可能成为瓶颈
- 缓解策略：节流（合并高频更新）、必要时考虑后台线程或 C 扩展替代
- 64KB scrollback 上限已有效控制了 pyte 需要处理的数据量
- 主客户端走 raw bytes 不经过 pyte 渲染，性能零影响

### 实施概要

引入 pyte 依赖，TerminalManager 中为每个终端创建 HistoryScreen + Stream。PTY 输出 feed 到 pyte，主客户端继续收 raw bytes，非主客户端收视口 ANSI 渲染。on_connect 统一用 pyte 快照替代 scrollback replay。前端移除 fitPaused 机制，xterm 始终 fit 容器。后续可扩展：视口滚动、增量 diff。

## 待定问题

### QUEST Q1: 视口滚动的交互方式
**问题**：非主客户端目前视口固定在底部+左侧。后续如需视口滚动（上下/左右平移），前端交互如何设计？需要新的 UI（如拖拽平移、触摸滑动），还是复用 xterm 的滚动机制？
**建议**：初版不实现视口滚动。后续需要时，可能在 xterm 外层加可平移的容器层。

## 关键参考

### 源码
- `mutbot/src/mutbot/runtime/terminal.py` — TerminalManager（`_on_pty_output`, `attach`, `detach`, `resize`），TerminalSession @impl（`on_connect` 中的 scrollback 发送）
- `mutbot/src/mutbot/ptyhost/_manager.py` — `SCROLLBACK_MAX = 64KB`，scrollback 累积逻辑，`get_scrollback()`
- `mutbot/src/mutbot/ptyhost/_client.py` — `get_scrollback()` base64 编码传输
- `mutbot/src/mutbot/ptyhost/_app.py` — ptyhost WebSocket 命令处理（scrollback 命令）
- `mutbot/frontend/src/panels/TerminalPanel.tsx` — `handleJsonMessage`（pty_resize / resize_owner），`handleBinaryData`（raw PTY 输出写入 xterm），`fitPaused` 机制

### 相关规范
- `mutbot/docs/specifications/feature-terminal-resize-control.md` — 主客户端优先策略（🔄 实施中）

### 外部依赖
- [pyte](https://github.com/selectel/pyte) — 纯 Python VT100 终端模拟器，零外部依赖，HistoryScreen 支持 scrollback + 分页（LGPL-3.0）

### 日志证据
- `server-20260313_100445` — WebSocket 高频断连重连（每秒一次），每次触发 on_connect 全量 scrollback replay
