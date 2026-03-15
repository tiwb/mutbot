# Terminal 功能设计

**日期**：2026-03-06

## 概述

Terminal 功能允许用户在浏览器中通过 WebSocket 与服务器上的 PTY（伪终端）进行交互。每个终端可以独立存在，也可以与一个 Session 绑定，由 Session 管理其完整生命周期。

终端有两种模式：

- **独立终端**：不与 Session 绑定，用户关闭即销毁
- **Session 终端**：绑定到 TerminalSession，具备持久化和重启能力

## 核心概念

### TerminalSession 与 PTY 实例

TerminalSession 是终端的持久化表示，保存在磁盘上。PTY 实例是实际运行的进程，驻留在内存中，服务重启后消失。

两者的关系：
- 一个 TerminalSession 在任意时刻最多对应一个 PTY 实例
- PTY 死亡后 TerminalSession 继续存在，保留历史输出（scrollback）
- 用户点击"Restart Terminal"后，TerminalSession 创建新的 PTY 实例

### Scrollback Buffer

PTY 的所有输出都被服务端缓存在内存中（Scrollback Buffer）。新客户端连接时，服务端先回放完整历史，再进入实时模式。

Scrollback 在以下情况持久化到磁盘（session JSON 文件）：
- PTY 进程结束时（读线程通过 `on_dead` 回调拷贝 scrollback 并标记 dirty，5 秒内异步写盘）
- 服务器正常关闭时（`on_stop` 同步持久化）

服务重启后，新 PTY 创建时会先注入已保存的 scrollback，使历史连续。

## 生命周期

### PTY 状态

```
[不存在] → [运行中] → [已结束]
              ↑           ↓
              └── Restart ←┘
```

- **不存在**：TerminalSession 刚创建，或 PTY 尚未启动
- **运行中**：PTY 进程活跃，接受输入，产生输出
- **已结束**：PTY 进程退出（用户 exit、被 kill、服务重启等）；此时历史输出仍可查看

### Restart 流程

用户主动重启时（点击"Restart Terminal"按钮）：

1. 服务端 kill 旧 PTY（若还存活）
2. 清空已持久化的 scrollback（新会话从空白开始）
3. 创建新 PTY，绑定到同一 TerminalSession
4. 前端建立新 WebSocket 连接

## 前端 UX 状态

前端 Terminal 面板有三种视觉状态：

| 状态 | 表现 |
|------|------|
| **Connecting** | 深色遮罩 + "Connecting..." 文字；终端不可交互 |
| **Connected** | 正常终端界面；可输入 |
| **Expired** | 深色遮罩 + "Restart Terminal" 按钮；PTY 已结束，历史可见于遮罩下方 |

状态转换：
- 页面加载 / WebSocket 断开 → **Connecting**
- 服务端确认 scrollback 回放完成 → **Connected**
- PTY 进程结束 → **Expired**（历史仍可见）
- 点击 Restart Terminal → **Connecting**（开始创建新 PTY）

WebSocket 断开后自动重连（指数退避），重连期间保持 Connecting 状态。

## WebSocket 通信协议

终端 channel 同时使用 Binary 帧和 JSON 帧，按消息频率分工：Binary 帧传输高频 I/O 数据，JSON 帧传输低频控制信号。

### Binary 帧 — I/O 数据

Binary 帧不含 msg_type 前缀，payload 即为原始数据。方向由 WebSocket 帧的发送方隐式区分：

| 方向 | 含义 |
|------|------|
| Client→Server | 用户键盘输入（UTF-8 编码） |
| Server→Client | PTY 输出数据（终端内容） |

### JSON 帧 — 控制信号

**Client→Server**：

| type | 含义 | 字段 |
|------|------|------|
| `resize` | 终端尺寸变更 | `rows`, `cols` |

**Server→Client**：

| type | 含义 | 字段 |
|------|------|------|
| `ready` | Scrollback 回放完成，告知终端当前状态 | `alive`（bool），`exit_code`（可选，仅 alive=false 时） |
| `process_exit` | PTY 进程运行中退出（运行时事件） | `exit_code`（可选） |
| `pty_resize` | PTY 实际尺寸（取所有客户端最小值后） | `rows`, `cols` |

### Input Muting

为防止 xterm.js 在 scrollback 回放期间将终端查询响应序列作为用户输入发送，客户端在收到 `ready` JSON 消息之前静音所有输入。客户端使用 xterm.js 的写回调（`term.write('', callback)`）确保所有 scrollback 数据解析完毕后再解除静音。

## 多客户端支持

同一终端可被多个浏览器标签页同时连接。

- 所有客户端实时收到相同的 PTY 输出
- 终端尺寸取所有连接客户端中的最小值（与 tmux 行为一致），避免某个小窗口客户端看到内容截断
- 某个客户端断开时，剩余客户端的最小尺寸重新计算并应用

### PTY 尺寸广播

每个客户端通过 FitAddon 计算并上报自己面板的期望尺寸。服务端取所有客户端的最小值设为 PTY 尺寸后，广播 `pty_resize` JSON 消息给所有客户端。客户端收到后调用 `term.resize()` 将 xterm 的逻辑尺寸覆盖为 PTY 实际尺寸，多余的面板空间显示为终端背景色。

广播时机：
- 任一客户端上报 resize 后
- 某客户端 detach 后（剩余客户端的最小尺寸可能变化）

前端使用标志位抑制 `term.resize()` 触发的 `onResize` 事件引发的回环上报。

## PTY 环境

PTY 进程（通常为 bash）使用以下环境配置：

- `TERM=xterm-256color`
- `COLORTERM=truecolor`
- 其余环境变量继承自 mutbot server 进程

PTY 输出中的 OSC 0/1/2 序列（终端标题变更）在服务端被过滤，不写入 scrollback，不发送给客户端。其他 OSC 序列（超链接、颜色等）正常透传。

## pyte 虚拟终端渲染

### 架构

服务端使用 pyte HistoryScreen 作为虚拟终端模拟器。PTY 原始输出不再直接转发给前端，而是经过以下管线：

```
PTY → ptyhost → mutbot → pyte Screen → render_dirty() → ANSI 帧 → 前端 xterm.js
```

pyte 消费所有原始 ANSI 转义序列，维护一个虚拟屏幕 buffer。`render_dirty()` 只渲染变化的行（dirty lines），生成紧凑的 ANSI 帧发送给前端。

### 渲染节奏

- **Flush 定时器**（16ms）：将缓冲的 PTY 数据喂给 pyte
- **Render 定时器**（80ms）：render 前先 flush 所有剩余缓冲数据，再调用 `render_dirty()` 生成帧

Render 前 flush all 确保 pyte 看到尽可能完整的状态，避免渲染中间态。

### 防闪烁：Synchronized Update (DEC Mode 2026)

`render_dirty()` 生成的 ANSI 帧使用 **Synchronized Update** 协议包裹：

```
\x1b[?2026h     ← BSU (Begin Synchronized Update)
\x1b[?25l       ← 隐藏光标
... dirty lines 更新 ...
\x1b[?25h       ← 显示光标
\x1b[?2026l     ← ESU (End Synchronized Update)
```

xterm.js 在收到 BSU 后暂停屏幕渲染，缓冲所有后续 ANSI 处理，直到 ESU 时一次性刷新。这解决了大帧（多行重绘，4000+ bytes）被 xterm.js 分帧处理导致的可见闪烁——部分行已更新、其余行仍是旧内容的混合状态。

**为什么需要这个协议**：pyte 的 `render_dirty()` 采用"整行重写"策略，每个 dirty 行都从行首定位、写入全部字符、清除行尾。对于全屏重绘（如 TUI 应用状态切换），产生的数据量远大于应用原始的增量 PTY 输出，触发 xterm.js 的输入分帧处理机制。

**注意**：pyte 会消费原始 PTY 输出中的 `?2026h`/`?2026l` 序列（如果应用自身发出了这些标记），不会透传给前端。因此必须在 `render_dirty()` 的输出中重新添加。

### 已知性能问题

服务器重启后首次连接需要从 ptyhost 获取 scrollback 数据并通过 pyte 重新 replay。scrollback 累积较大时（如 10MB），pyte 逐字符解析需要数十秒。后续优化方向：限制 scrollback 大小、按需加载历史、或持久化 pyte 屏幕状态。
