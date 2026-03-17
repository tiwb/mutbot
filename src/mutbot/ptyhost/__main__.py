"""python -m mutbot.ptyhost — PTY 宿主守护进程入口。

启动流程：
1. 预绑定随机端口
2. 写入端口文件 ~/.mutbot/ptyhost.port
3. 启动 ASGI Server
"""

from __future__ import annotations

import logging
import os
import socket
import sys

logger = logging.getLogger("mutbot.ptyhost")

# ---------------------------------------------------------------------------
# Windows 窗口控制
# ---------------------------------------------------------------------------

_console_hwnd: int = 0  # 控制台窗口句柄（仅 Windows）
_console_visible: bool = False  # 当前窗口可见状态


def set_window_visible(visible: bool) -> bool:
    """设置控制台窗口可见性，返回设置后的状态。仅 Windows 有效。"""
    global _console_visible
    if sys.platform != "win32" or not _console_hwnd:
        return False
    import ctypes
    SW_SHOW, SW_HIDE = 5, 0
    ctypes.windll.user32.ShowWindow(_console_hwnd, SW_SHOW if visible else SW_HIDE)
    _console_visible = visible
    return _console_visible


def get_window_visible() -> bool:
    """返回当前控制台窗口可见状态。"""
    return _console_visible

_BANNER = """\
================================================
  MutBot PTY Host
  Listening on 127.0.0.1:{port}

  This process manages all terminal sessions.
  Closing this window will disconnect all terminals.
================================================"""


def _port_file_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".mutbot", "ptyhost.port")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from mutbot.ptyhost._app import PtyHostApp
    from mutagent.net.asgi import Server as _ASGIServer

    app = PtyHostApp()

    # 预绑定随机端口
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    # 写入端口文件
    port_file = _port_file_path()
    os.makedirs(os.path.dirname(port_file), exist_ok=True)
    with open(port_file, "w") as f:
        f.write(str(port))

    logger.info("ptyhost starting on 127.0.0.1:%d", port)

    # Windows: 记录控制台窗口句柄（窗口默认隐藏，用户可通过菜单显示）
    if sys.platform == "win32":
        global _console_hwnd
        import ctypes
        _console_hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        print(_BANNER.format(port=port), flush=True)

    server = _ASGIServer(app)

    # 空闲退出回调
    app.should_exit_callback = lambda: setattr(server, "should_exit", True)

    try:
        server.run(sockets=[sock])
    finally:
        # 清理端口文件
        try:
            os.remove(port_file)
        except OSError:
            pass


if __name__ == "__main__":
    main()
