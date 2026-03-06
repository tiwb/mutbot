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
- PTY 进程结束时
- 服务器正常关闭时

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

客户端与服务端通过二进制 WebSocket 消息通信，消息首字节标识类型。

**服务端 → 客户端**：

| 字节 | 含义 |
|------|------|
| `0x01` | PTY 输出数据（后续字节为终端内容） |
| `0x03` | Scrollback 回放完成，客户端可开始接受输入 |
| `0x04` | PTY 进程已结束（后续 4 字节为 exit code） |

**客户端 → 服务端**：

| 字节 | 含义 |
|------|------|
| `0x00` | 用户键盘输入（后续字节为 UTF-8 内容） |
| `0x02` | 终端尺寸变更（后续 2+2 字节为 rows/cols，大端序） |

WebSocket 关闭码 `4004` 表示 terminal_id 不存在（已被清理）。

### Input Muting

为防止 xterm.js 在 scrollback 回放期间将终端查询响应序列作为用户输入发送，客户端在收到 `0x03` 之前静音所有输入。

## 多客户端支持

同一终端可被多个浏览器标签页同时连接。

- 所有客户端实时收到相同的 PTY 输出
- 终端尺寸取所有连接客户端中的最小值（与 tmux 行为一致），避免某个小窗口客户端看到内容截断
- 某个客户端断开时，剩余客户端的最小尺寸重新计算并应用

## PTY 环境

PTY 进程（通常为 bash）使用以下环境配置：

- `TERM=xterm-256color`
- `COLORTERM=truecolor`
- 其余环境变量继承自 mutbot server 进程

PTY 输出中的 OSC 0/1/2 序列（终端标题变更）在服务端被过滤，不写入 scrollback，不发送给客户端。其他 OSC 序列（超链接、颜色等）正常透传。
