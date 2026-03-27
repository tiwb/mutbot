# Session 启动配置设计规范

**状态**：📝 设计中
**日期**：2026-03-19
**类型**：功能设计

## 背景

当前创建终端时直接 spawn 默认 shell（Windows: `COMSPEC`，Unix: `SHELL`），用户无法配置：
- 工作目录（已通过 Settings UI 支持修改，但需 restart 生效）
- 启动命令（如 `python`、`node`、`ssh user@host`、`conda activate env`）
- 不局限于终端——未来其他 Session 类型也可能需要启动配置

需求：
1. 新建终端时先弹出配置界面，用户设置 cwd 和启动命令后再创建
2. 支持启动任意程序，不局限于 shell
3. 已有终端的 Settings 也能修改这些配置

## 待定问题

### QUEST Q1: 启动命令的粒度
**问题**：启动命令是完整的 command line（如 `python -m http.server 8080`），还是分为 program + args？
**建议**：单一 command 字符串，交给 shell 解析。简单直觉，用户输入和终端执行一致。如果 command 为空则启动默认 shell。

### QUEST Q2: command 与 shell 的关系
**问题**：如果用户指定 `python`，是直接 spawn `python` 进程，还是先启动 shell 再执行 `python`？
**建议**：直接 spawn 指定程序（不套 shell）。原因：
- 套 shell 会多一层进程，退出行为不一致
- 用户想要 shell 时，不填 command 即可（回退到默认 shell）
- 想要 shell + 自动命令（如 `conda activate`），填 `bash -c "conda activate env && exec bash"`

### QUEST Q3: 新建终端的 UI 流程
**问题**：新建终端时是否必须弹配置界面？还是可选（默认跳过，用默认配置创建）？
**建议**：默认直接创建（当前行为不变），菜单中新增"New Terminal (with config)"选项弹配置。或者在 AddSessionMenu 的 Terminal 项上用快捷键区分（如 Shift+点击弹配置）。

### QUEST Q4: ptyhost create 接口扩展
**问题**：当前 ptyhost `create` 只接受 `rows, cols, cwd`，需要新增 `command` 参数。Windows（winpty）和 Unix（subprocess）的 spawn 逻辑都需要适配。
**建议**：`create(rows, cols, cwd, command=None)` — command 为 None 时用默认 shell（当前行为），非 None 时 spawn 指定程序。

### QUEST Q5: Settings UI 与新建 UI 的复用
**问题**：已有终端的 Settings 表单和新建时的配置表单内容相同（cwd + command），如何复用？
**建议**：共用同一个 view schema 构建函数。区别在于：
- 已有终端 Settings：Apply 只保存 config，不 restart
- 新建时配置：确认后创建 TerminalSession 并 spawn
- 按钮文案不同（"Apply" vs "Create"）

## 关键参考

### 源码
- `src/mutbot/ptyhost/_manager.py:88` — `PtyManager.create()`，spawn shell 进程
- `src/mutbot/ptyhost/_manager.py:114` — `_spawn_windows()`，`winpty.PtyProcess.spawn(shell)`
- `src/mutbot/ptyhost/_manager.py:125` — `_spawn_unix()`，`subprocess.Popen([shell])`
- `src/mutbot/ptyhost/_client.py:241` — `PtyHostClient.create()`，发 create 命令
- `src/mutbot/ptyhost/_app.py:179` — `_handle_command("create")`，调用 manager.create
- `src/mutbot/runtime/terminal.py:339` — `_terminal_on_create()`，从 config 读 cwd 调 tm.create
- `src/mutbot/runtime/terminal.py:629` — `_handle_open_settings()`，Settings UI 实现
- `src/mutbot/builtins/menus.py` — `AddSessionMenu`（新建 session 菜单）、`TerminalSettingsMenu`
