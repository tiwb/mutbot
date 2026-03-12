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
logger = logging.getLogger("mutbot.ptyhost")


def _port_file_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".mutbot", "ptyhost.port")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from mutbot.ptyhost._app import PtyHostApp
    from mutagent.net.server import Server

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

    server = Server(app)

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
