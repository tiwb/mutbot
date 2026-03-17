"""ptyhost 发现与启动工具。

负责发现已运行的 ptyhost 或 spawn 新实例。按需调用（懒启动）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys

logger = logging.getLogger("mutbot.ptyhost")

_PORT_FILE = os.path.join(os.path.expanduser("~"), ".mutbot", "ptyhost.port")


def _read_port_file() -> int | None:
    """读取端口文件，返回端口号或 None。"""
    try:
        with open(_PORT_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


async def _try_connect(host: str, port: int) -> bool:
    """尝试连接 ptyhost 并发 list 命令验证身份。"""
    from mutbot.ptyhost._client import PtyHostClient
    client = PtyHostClient(host, port)
    try:
        await client.connect()
        # 验证是真正的 ptyhost（不是其他进程占用端口）
        await asyncio.wait_for(client.list_terminals(), timeout=2.0)
        await client.close()
        return True
    except Exception:
        return False


def _spawn_ptyhost() -> None:
    """启动 ptyhost 守护进程（detached）。"""
    python = sys.executable
    if sys.platform == "win32":
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        subprocess.Popen(
            [python, "-m", "mutbot.ptyhost"],
            creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW,
            close_fds=True,
        )
    else:
        subprocess.Popen(
            [python, "-m", "mutbot.ptyhost"],
            start_new_session=True,
            close_fds=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


async def ensure_ptyhost() -> int:
    """确保 ptyhost 正在运行，返回端口号。

    1. 读取端口文件
    2. 尝试连接验证
    3. 失败则 spawn 新实例，等待就绪
    """
    host = "127.0.0.1"

    # 尝试连接已有的 ptyhost
    port = _read_port_file()
    if port is not None:
        if await _try_connect(host, port):
            logger.info("Found running ptyhost on port %d", port)
            return port

    # Spawn 新实例
    logger.info("Spawning ptyhost daemon...")
    _spawn_ptyhost()

    # 等待就绪（轮询，超时 10s）
    for _ in range(50):
        await asyncio.sleep(0.2)
        port = _read_port_file()
        if port is not None:
            if await _try_connect(host, port):
                logger.info("ptyhost ready on port %d", port)
                return port

    raise RuntimeError("Failed to start ptyhost daemon (timeout)")
