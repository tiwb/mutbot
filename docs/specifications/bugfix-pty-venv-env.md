# PTY 启动时清理 Python venv 环境变量 设计规范

**状态**：📋 需求中
**日期**：2026-04-10
**类型**：Bug修复

## 需求

1. mutbot 的 ptyhost 在 spawn 终端时继承了宿主进程的 `VIRTUAL_ENV` 环境变量
2. 导致终端内的 `uv run`、`pip` 等工具使用错误的 venv（如 `D:\ai\.venv`），而非项目自身的 `.venv`
3. 终端 spawn 时应清理 Python venv 相关环境变量，让用户进入一个干净的 shell 环境

## 关键参考

- `src/mutbot/ptyhost/_manager.py:240` — Unix spawn 处 `env = {**os.environ, ...}` 直接继承所有环境变量
- `src/mutbot/ptyhost/_manager.py:221` — Windows spawn 处 `PtyProcess.spawn` 未传 `env`，也默认继承
- 需要清理的环境变量：`VIRTUAL_ENV`、`VIRTUAL_ENV_PROMPT`（可能还有 `CONDA_DEFAULT_ENV`、`CONDA_PREFIX` 等）
