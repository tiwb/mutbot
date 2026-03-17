# PtyHost Kill 菜单 + 菜单通用 confirm 机制 设计规范

**状态**：🔄 实施中
**日期**：2026-03-17
**类型**：功能设计

## 背景

原需求是 ptyhost 窗口 toggle（显示/隐藏控制台窗口），已实施但 `ShowWindow(SW_SHOW)` 在 `SW_HIDE` 启动的窗口上不生效。

实际上显示窗口的唯一目的是关闭它，而日志可通过 CLI/MCP 查询，不需要窗口。因此：
- 将菜单功能改为 **Kill PtyHost**（kill 后创建终端会自动重新拉起）
- 启动改为无窗口模式，简化代码
- Kill 操作需要确认，顺便为菜单系统新增**通用 confirm 机制**

## 设计方案

### 一、菜单通用 confirm 机制

**协议**：后端 `execute()` 返回 `MenuResult(action="confirm", data={...})`，前端弹确认对话框，用户确认后带 `confirmed: true` 重新调用。

```
前端点击菜单 → menu.execute({menu_id, params})
后端返回 → {action: "confirm", data: {message: "...", confirm_style: "danger"}}
前端弹 confirm → 用户确认
前端再次调用 → menu.execute({menu_id, params: {..., confirmed: true}})
后端执行实际操作 → 返回正常结果
```

**后端模式**：execute 方法检查 `params.get("confirmed")`，未确认时返回 confirm result：

```python
async def execute(self, params: dict, context: RpcContext) -> MenuResult:
    if not params.get("confirmed"):
        return MenuResult(action="confirm", data={
            "message": "确定终止 PtyHost？所有终端连接将断开。",
            "confirm_style": "danger",  # 可选：danger / warning / default
        })
    # 实际执行...
```

**前端处理**：在 `RpcMenu.handleExecute` 中，检查 result.action == "confirm"，弹 `window.confirm()`，确认后重新调用（简洁方案，用原生 confirm 即可，后续可升级为自定义对话框）。

### 二、PtyHost 启动简化

**去掉窗口相关代码**：
- `_bootstrap.py`：去掉 `CREATE_NEW_CONSOLE` + `SW_HIDE`，改为 `CREATE_NO_WINDOW`（`0x08000000`）。ptyhost 不需要自己的控制台，winpty 内部管理 ConPTY
- `__main__.py`：删除 `_console_hwnd`、`set_window_visible()`、`get_window_visible()`、banner `print()`

### 三、Kill PtyHost 菜单

**ptyhost 侧**：`_app.py` 新增 `shutdown` 命令，触发 graceful shutdown（删除 `window` 命令）

**client 侧**：`_client.py` 删除 `set_window()`/`get_window()`，新增 `shutdown()`

**菜单**：`TogglePtyHostWindowMenu` → `KillPtyHostMenu`
- 菜单文本："Kill PtyHost"
- 图标：terminal（保持）
- execute：先 confirm，确认后调用 `client.shutdown()`

### 变更清单

| 文件 | 变更 |
|------|------|
| `frontend/src/components/RpcMenu.tsx` | `handleExecute` 中处理 `action="confirm"` |
| `ptyhost/_bootstrap.py` | `CREATE_NEW_CONSOLE + SW_HIDE` → `CREATE_NO_WINDOW` |
| `ptyhost/__main__.py` | 删除窗口控制代码和 banner print |
| `ptyhost/_app.py` | 删除 `window` 命令，新增 `shutdown` 命令 |
| `ptyhost/_client.py` | 删除 `set/get_window`，新增 `shutdown()` |
| `builtins/menus.py` | `TogglePtyHostWindowMenu` → `KillPtyHostMenu`，使用 confirm 机制 |
| 设计文档 | 文件重命名为 `feature-menu-confirm-and-kill-ptyhost.md` |

## 实施步骤清单

### 阶段一：菜单通用 confirm 机制 [✅ 已完成]

- [x] **Task 1.1**: 前端 RpcMenu — handleExecute 支持 confirm 流程
  - result.action === "confirm" 时，弹 `window.confirm(result.data.message)`
  - 用户确认后，带 `confirmed: true` 重新调用 `menu.execute`
  - 用户取消则不做任何操作
  - 状态：✅ 已完成

### 阶段二：PtyHost 启动简化 [✅ 已完成]

- [x] **Task 2.1**: 简化 `_bootstrap.py` — 无窗口启动
  - 去掉 `CREATE_NEW_CONSOLE` + `STARTUPINFO(SW_HIDE)`
  - 改为 `CREATE_NO_WINDOW`（`0x08000000`），保留 `CREATE_NEW_PROCESS_GROUP`
  - 状态：✅ 已完成

- [x] **Task 2.2**: 清理 `__main__.py` — 删除窗口控制代码
  - 删除 `_console_hwnd`、`_console_visible` 全局变量
  - 删除 `set_window_visible()`、`get_window_visible()` 函数
  - 删除 banner `print()` 调用（`_BANNER` 常量和 `GetConsoleWindow` 调用）
  - 状态：✅ 已完成

### 阶段三：Kill PtyHost 功能 [✅ 已完成]

- [x] **Task 3.1**: `_app.py` — 删除 `window` 命令，新增 `shutdown` 命令
  - shutdown 触发 graceful 关闭（设置 should_exit）
  - 状态：✅ 已完成

- [x] **Task 3.2**: `_client.py` — 删除 `set_window`/`get_window`，新增 `shutdown()`
  - 状态：✅ 已完成

- [x] **Task 3.3**: `menus.py` — `TogglePtyHostWindowMenu` → `KillPtyHostMenu`
  - 使用 confirm 机制，确认后调用 `client.shutdown()`
  - 状态：✅ 已完成

### 阶段四：验证 [待开始]

- [ ] **Task 4.1**: 构建前端并手动验证
  - 启动 mutbot，确认 ptyhost 无窗口弹出
  - 菜单点击 Kill PtyHost，确认弹出确认对话框
  - 确认后 ptyhost 进程终止
  - 新建终端，确认 ptyhost 自动重新拉起
  - 状态：⏸️ 待开始

## 关键参考

### 源码
- `mutbot/frontend/src/components/RpcMenu.tsx:263-286` — `handleExecute`，menu execute 调用入口
- `mutbot/src/mutbot/menu.py:42-46` — `MenuResult` 定义
- `mutbot/src/mutbot/ptyhost/_bootstrap.py:42-57` — `_spawn_ptyhost()` 当前实现
- `mutbot/src/mutbot/ptyhost/__main__.py:22-40` — 窗口控制代码（待删除）
- `mutbot/src/mutbot/ptyhost/_app.py:239-246` — `window` 命令（待替换为 shutdown）
- `mutbot/src/mutbot/builtins/menus.py:331-364` — `TogglePtyHostWindowMenu`（待改为 Kill）
