# PtyHost 窗口显示/隐藏控制 设计规范

**状态**：🔄 实施中
**日期**：2026-03-17
**类型**：功能设计

## 背景

Windows 上 ptyhost 守护进程以 `CREATE_NEW_CONSOLE` 启动，会弹出一个可见的控制台窗口（显示 banner 和日志）。对普通用户来说这个窗口是干扰项，但调试时又需要查看。

需求：默认隐藏 ptyhost 窗口，但提供前端操作让用户可以显示/隐藏。

## 设计方案

### 核心设计

**启动时隐藏窗口**：在 `_spawn_ptyhost()` 中使用 `STARTUPINFO` + `SW_HIDE`，让 ptyhost 启动时窗口不可见。

```python
# _bootstrap.py
startupinfo = subprocess.STARTUPINFO()
startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
startupinfo.wShowWindow = 0  # SW_HIDE
subprocess.Popen(
    [python, "-m", "mutbot.ptyhost"],
    creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NEW_CONSOLE,
    startupinfo=startupinfo,
    close_fds=True,
)
```

仍保留 `CREATE_NEW_CONSOLE`（ptyhost 需要独立控制台来管理 ConPTY），但通过 `SW_HIDE` 让窗口初始不可见。

**ptyhost 侧窗口控制**：在 ptyhost 进程中通过 Windows API 控制自身窗口可见性：

```python
# __main__.py（仅 Windows）
import ctypes
_hwnd = ctypes.windll.kernel32.GetConsoleWindow()

def set_window_visible(visible: bool) -> None:
    SW_SHOW, SW_HIDE = 5, 0
    ctypes.windll.user32.ShowWindow(_hwnd, SW_SHOW if visible else SW_HIDE)
```

**通信链路**：新增 ptyhost WebSocket 命令 `window`，mutbot 通过 PtyHostClient 转发前端请求：

```
前端 → Menu.execute() → PtyHostClient → ptyhost (window command)
```

**前端 UI**：通过 Menu Declaration 机制，在 `SessionList/Header` 菜单中添加 "Show/Hide PtyHost Console" 菜单项。

- 使用 `check_visible()` 判断 `sys.platform == "win32"`，非 Windows 自动隐藏
- 使用 `dynamic_items()` 根据当前窗口状态动态切换菜单文本（"Show PtyHost Console" / "Hide PtyHost Console"）
- `execute()` 中调用 PtyHostClient 发送 `window` 命令

### 菜单定义

```python
class TogglePtyHostWindowMenu(Menu):
    """全局菜单 — 显示/隐藏 PtyHost 控制台窗口"""
    display_name = "Show PtyHost Console"
    display_icon = "terminal"
    display_category = "SessionList/Header"
    display_order = "2debug:0"

    @classmethod
    def check_visible(cls, context: dict) -> bool | None:
        return sys.platform == "win32"
```

### ptyhost 命令协议

```json
// 请求
{"cmd": "window", "visible": true}   // 显示窗口
{"cmd": "window", "visible": false}  // 隐藏窗口

// 响应
{"ok": true, "visible": true}        // 当前窗口状态
```

## 实施步骤清单

### 阶段一：ptyhost 侧 [✅ 已完成]

- [x] **Task 1.1**: 修改 `_bootstrap.py` — 启动时隐藏窗口
  - 在 `_spawn_ptyhost()` 的 Windows 分支中添加 `STARTUPINFO(SW_HIDE)`
  - 状态：✅ 已完成

- [x] **Task 1.2**: 修改 `__main__.py` — 记录 HWND 并提供窗口控制函数
  - Windows 上 `GetConsoleWindow()` 获取 HWND
  - 提供 `set_window_visible(visible)` 和 `get_window_visible()` 函数
  - 状态：✅ 已完成

- [x] **Task 1.3**: 修改 `_app.py` — 新增 `window` 命令处理
  - 在 `_handle_command()` 中添加 `"window"` 分支
  - 支持查询（无 visible 参数）和设置（有 visible 参数）
  - 状态：✅ 已完成

### 阶段二：mutbot 侧 [✅ 已完成]

- [x] **Task 2.1**: 修改 `_client.py` — PtyHostClient 新增方法
  - `set_window(visible)` 设置窗口可见性
  - `get_window()` 查询当前状态
  - 状态：✅ 已完成

- [x] **Task 2.2**: 在 `builtins/menus.py` 中添加 `TogglePtyHostWindowMenu`
  - `display_category = "SessionList/Header"`，`display_order = "2debug:0"`
  - `check_visible()` 判断 `sys.platform == "win32"`
  - `execute()` 先查询当前状态再 toggle
  - 状态：✅ 已完成

### 阶段三：验证 [待开始]

- [ ] **Task 3.1**: 手动验证
  - 启动 mutbot，确认 ptyhost 窗口默认不可见
  - 通过菜单点击 Toggle PtyHost Console，确认窗口正确切换
  - 状态：⏸️ 待开始

## 关键参考

### 源码
- `mutbot/src/mutbot/ptyhost/_bootstrap.py` — ptyhost 启动逻辑，`_spawn_ptyhost()` 第 42-61 行
- `mutbot/src/mutbot/ptyhost/__main__.py` — ptyhost 入口，banner 打印，第 32-77 行
- `mutbot/src/mutbot/ptyhost/_app.py` — ASGI 应用，`_handle_command()` 第 161-239 行
- `mutbot/src/mutbot/ptyhost/_client.py` — PtyHostClient，命令发送
- `mutbot/src/mutbot/builtins/menus.py` — 内置菜单定义，`SessionList/Header` category
- `mutbot/src/mutbot/menu.py` — Menu Declaration 基类（`check_visible`、`dynamic_items`）
- `mutbot/src/mutbot/runtime/menu_impl.py` — MenuRegistry 实现
