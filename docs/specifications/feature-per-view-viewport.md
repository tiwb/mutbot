# Per-View Viewport — 独立视口滚动 设计规范

**状态**：✅ 已完成
**日期**：2026-03-17
**类型**：功能设计

## 背景

Per-client view 架构完成后，每个客户端拥有独立的 ptyhost view 和 scroll_offset。但当 PTY 尺寸由桌面控制（如 40 行）时，手机端 xterm 容器只能显示约 20 行，底部内容（含 shell prompt）被 CSS `overflow: hidden` 裁掉，用户无法看到。

现有 scroll_offset 只能滚动历史（已滚出屏幕的内容），无法在当前屏幕内部滚动。

## 设计方案

### 核心设计

给每个 TermView 增加 `viewport_rows` 字段，表示该 view 的可见行数。当 `viewport_rows < screen.lines` 时，view 只渲染终端屏幕的一个"窗口"。

```
终端实际 40 行（桌面 resize owner 决定）
┌─────────────────┐
│  行 1-20        │ ← 手机往上滚能看到
├─────────────────┤
│  行 21-40       │ ← 手机默认看到这里（offset=0，底部含 shell prompt）
│  $ _            │
└─────────────────┘

桌面 view: viewport_rows=40 = screen.lines → 行为不变
手机 view: viewport_rows=20 < screen.lines → viewport 模式
```

### scroll_offset 语义扩展

复用现有 scroll_offset，无缝串联屏幕内滚动和历史滚动：

| offset 范围 | 可见内容 | 说明 |
|------------|---------|------|
| 0 | 屏幕底部 viewport_rows 行 | live，含 shell prompt |
| 1 ~ (screen.lines - viewport_rows) | 屏幕内向上平移 | 穿过当前屏幕 |
| > (screen.lines - viewport_rows) | 进入历史 | 与现有历史滚动一致 |

用户触摸/滚轮操作统一为"往上滚"，不区分屏幕内滚动和历史滚动。

### 渲染策略

- **viewport_rows == screen.lines**：走现有增量 dirty-line 路径（桌面，零改动）
- **viewport_rows < screen.lines**：每次 dirty 触发时走 `_render_scrolled_view` 风格的全量渲染（20 行全量帧几 KB，可接受）

### viewport_rows 的设置时机

- `on_connect`：客户端上报 rows，作为 view 的 viewport_rows
- `resize` 消息：客户端上报新 rows，更新 viewport_rows（但不改 PTY 尺寸，除非该客户端是 resize owner）
- resize owner 的 viewport_rows 自然等于 screen.lines，不需要特殊逻辑

### 数据结构变更

```python
# _screen.py TermView
@dataclass
class TermView:
    id: str
    term_id: str
    scroll_offset: int = 0
    viewport_rows: int = 0     # 新增：0 表示使用 screen.lines（兼容默认）
```

### 渲染变更

`_render_scrolled_view` 中的 `visible` 从 `screen.lines` 改为 `view.viewport_rows`（非零时）：

```python
visible = view.viewport_rows if view.viewport_rows > 0 else screen.lines
```

live 帧推送逻辑变更：viewport < screen.lines 的 view 不走增量路径，而是在每次 dirty 触发时生成独立的全量帧。

### 前端变更

无需前端改动。手机 xterm 已经通过 FitAddon 计算出正确的 rows 并上报给服务端。服务端按 viewport_rows 渲染的帧行数恰好匹配 xterm 的 rows。

### 需要注意的边界情况

1. **viewport_rows > screen.lines**：clamp 到 screen.lines，等同于全屏
2. **PTY resize 后 viewport_rows 的关系变化**：PTY 从 40 行 resize 到 30 行，某个 view 的 viewport_rows=20 仍然有效；如果 resize 到 15 行，viewport_rows=20 > screen.lines，退化为全屏
3. **scroll_state 的 total/visible 语义**：`visible` 应返回 viewport_rows 而非 screen.lines，确保前端滚动条比例正确

## 关键参考

### 源码
- `mutbot/src/mutbot/ptyhost/_screen.py:112-122` — TermView 数据结构
- `mutbot/src/mutbot/ptyhost/_manager.py:569-585` — `_render_scrolled_view` 渲染逻辑
- `mutbot/src/mutbot/ptyhost/_manager.py:342,397` — live 渲染触发与推帧决策
- `mutbot/src/mutbot/ptyhost/_manager.py:473-492` — `create_view` / `destroy_view`
- `mutbot/src/mutbot/runtime/terminal.py:410-426` — `on_connect` 创建 per-client view
- `mutbot/src/mutbot/runtime/terminal.py:487-540` — `on_message` scroll 命令处理
- `mutbot/src/mutbot/ptyhost/_client.py` — PTY host 客户端方法（scroll/get_scroll_state）

### 相关规范
- `mutbot/docs/specifications/bugfix-terminal-scroll-overlay.md` — Per-client view 架构（前置）
- `mutbot/docs/specifications/bugfix-terminal-scrollbar-interaction.md` — 滚动条交互

## 实施步骤清单

### Phase 1: TermView 数据结构 + 渲染适配 [✅ 已完成]

- [x] **Task 1.1**: TermView 新增 `viewport_rows` 字段
  - 状态：✅ 已完成

- [x] **Task 1.2**: `_render_scrolled_view` 使用 viewport_rows
  - 新增 `visible` 参数，所有调用点传入 `view.viewport_rows`
  - `scroll_view` / `scroll_view_to` 的 max_offset 扩展：viewport 模式下加上 `screen.lines - vp`
  - 状态：✅ 已完成

- [x] **Task 1.3**: live 帧推送适配 viewport 模式
  - `_do_render_term` 中 viewport < screen.lines 的 view 走全量 `_render_scrolled_view`
  - `get_snapshot` / `scroll_view_to_bottom` / `clear_scrollback` 同步适配
  - 状态：✅ 已完成

### Phase 2: ptyhost 接口 + client 方法 [✅ 已完成]

- [x] **Task 2.1**: `create_view` 支持 viewport_rows 参数
  - `_manager.py` / `_app.py` / `_client.py` 三层传递
  - 状态：✅ 已完成

- [x] **Task 2.2**: 新增 `set_viewport` 命令
  - `_manager.py set_viewport()` + `_app.py` action 分发 + `_client.py` async 方法
  - set_viewport 后若为 live view，立即推送新帧
  - 状态：✅ 已完成

- [x] **Task 2.3**: `get_scroll_state` 返回 viewport_rows 对应的 visible
  - 状态：✅ 已完成

### Phase 3: runtime 层集成 [✅ 已完成]

- [x] **Task 3.1**: `on_connect` 创建 view（viewport_rows=0 默认全屏）
  - 客户端连接后通过 resize 消息上报 rows，再更新 viewport
  - 状态：✅ 已完成

- [x] **Task 3.2**: `resize` 方法中更新 viewport_rows
  - 每次 resize 消息都调用 `set_viewport(view_id, rows)`（fire-and-forget）
  - resize owner 的 viewport_rows 自然等于 screen.lines
  - 状态：✅ 已完成

### Phase 4: 构建与验证 [✅ 已完成]

- [x] **Task 4.1**: Python 导入验证通过
  - 状态：✅ 已完成

- [x] **Task 4.2**: viewport 数学验证
  - offset=0 显示底部 viewport_rows 行（含 shell prompt）
  - 往上滚先穿过屏幕再进入历史，无缝衔接
  - 状态：✅ 已完成

### Phase 5: 实施中发现的额外问题修复 [✅ 已完成]

- [x] **Task 5.1**: 前端 pty_resize 导致 xterm 超出容器
  - pty_resize 强制 xterm 匹配 PTY 尺寸，viewport 帧与 xterm 尺寸不匹配
  - 修复：pty_resize 后追加 `fitAddon.fit()` 让 xterm 回到容器尺寸
  - 状态：✅ 已完成

- [x] **Task 5.2**: viewport_cols 列裁剪
  - 水平方向同样存在 PTY 宽于手机的问题（diff 绿色背景行溢出）
  - TermView 新增 `viewport_cols`，`_render_viewport_frame` 同时裁剪行和列
  - 状态：✅ 已完成

- [x] **Task 5.3**: 宽字符边界防溢出
  - 中文/emoji（占 2 列）在 viewport 最后一列时导致换行溢出
  - `render_lines` 中检测 `wcwidth==2 && col+2>cols` 时替换为空格
  - 状态：✅ 已完成
