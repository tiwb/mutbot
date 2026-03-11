"""mutbot.ptyhost — 独立 PTY 宿主守护进程。

轻量 PTY 进程池，通过 WebSocket 提供 I/O 中继。
独立于 mutbot 主进程运行，终端在 mutbot 重启后存活。
"""
