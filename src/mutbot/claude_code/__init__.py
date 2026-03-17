"""Claude Code 自包含模块（未完成）。

本包包含 Claude Code Web 界面集成的所有代码：
- session.py — ClaudeCodeSession 声明
- runtime.py — 子进程管理 + @impl 生命周期 + channel 通信

当前状态：代码已从核心模块移入，但 import 路径等尚未适配，不可直接运行。
后续需要：修复 import、配置驱动加载、前端动态面板注册。

参见：docs/specifications/refactor-claude-code-self-contained.md
"""
