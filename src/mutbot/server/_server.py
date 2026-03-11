"""ASGI Server — asyncio 事件循环 + TCP 服务器管理 + lifespan 协议。"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket as _socket
import sys
from typing import Any

from mutbot.server._http import HTTPProtocol

logger = logging.getLogger("mutbot.server")


class Server:
    """轻量 ASGI server。

    用法::

        server = Server(app)
        server.run(host="127.0.0.1", port=8000)

        # 预绑定 socket
        server = Server(app)
        server.run(sockets=[sock1, sock2])
    """

    def __init__(self, app: Any, *, root_path: str = "") -> None:
        self.app = app
        self.root_path = root_path

        self._servers: list[asyncio.AbstractServer] = []
        self._server_state: dict[str, Any] = {
            "connections": set(),
        }
        self.should_exit = False
        self._force_exit = False

        # Lifespan state
        self._lifespan_task: asyncio.Task[None] | None = None
        self._lifespan_receive_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._lifespan_startup_complete = asyncio.Event()
        self._lifespan_shutdown_complete = asyncio.Event()
        self._lifespan_startup_failed = False
        self._app_state: dict[str, Any] = {}

    @property
    def ports(self) -> list[int]:
        """返回所有 TCP server 实际绑定的端口。"""
        result: list[int] = []
        for server in self._servers:
            if server.sockets:
                for sock in server.sockets:
                    result.append(sock.getsockname()[1])
        return result

    def run(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        sockets: list[_socket.socket] | None = None,
        on_startup: Any = None,
    ) -> None:
        """阻塞运行 server。

        on_startup: 可选的 async callback，在所有 TCP server 启动后调用。
        """
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._serve(
                host=host, port=port, sockets=sockets, on_startup=on_startup,
            ))
        except KeyboardInterrupt:
            pass
        finally:
            loop.close()

    async def _serve(
        self,
        *,
        host: str | None,
        port: int | None,
        sockets: list[_socket.socket] | None,
        on_startup: Any,
    ) -> None:
        """主服务循环：lifespan startup → TCP startup → main_loop → shutdown。"""
        # 1. ASGI lifespan startup
        await self._lifespan_startup()

        if self._lifespan_startup_failed:
            logger.error("Lifespan startup failed, aborting")
            return

        # 2. TCP servers
        await self.startup(host=host, port=port, sockets=sockets)

        if on_startup:
            await on_startup()

        self._install_signal_handlers()

        # 3. Main loop
        await self.main_loop()

        # 4. Shutdown
        await self.shutdown()

        # 5. ASGI lifespan shutdown
        await self._lifespan_shutdown()

    async def _lifespan_startup(self) -> None:
        """发送 lifespan.startup 事件给 ASGI app。"""
        scope: dict[str, Any] = {
            "type": "lifespan",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "state": self._app_state,
        }

        async def receive() -> dict[str, Any]:
            return await self._lifespan_receive_queue.get()

        async def send(message: dict[str, Any]) -> None:
            msg_type = message["type"]
            if msg_type == "lifespan.startup.complete":
                self._lifespan_startup_complete.set()
            elif msg_type == "lifespan.startup.failed":
                self._lifespan_startup_failed = True
                self._lifespan_startup_complete.set()
                logger.error("Lifespan startup failed: %s",
                             message.get("message", ""))
            elif msg_type == "lifespan.shutdown.complete":
                self._lifespan_shutdown_complete.set()

        self._lifespan_task = asyncio.get_running_loop().create_task(
            scope_runner(self.app, scope, receive, send)
        )

        # 发送 startup 事件
        await self._lifespan_receive_queue.put({"type": "lifespan.startup"})

        # 等待 app 响应
        await self._lifespan_startup_complete.wait()

    async def _lifespan_shutdown(self) -> None:
        """发送 lifespan.shutdown 事件给 ASGI app。"""
        if self._lifespan_task is None or self._lifespan_task.done():
            return

        await self._lifespan_receive_queue.put({"type": "lifespan.shutdown"})

        try:
            await asyncio.wait_for(self._lifespan_shutdown_complete.wait(), timeout=30.0)
        except asyncio.TimeoutError:
            logger.warning("Lifespan shutdown timed out")

        if not self._lifespan_task.done():
            self._lifespan_task.cancel()
            try:
                await self._lifespan_task
            except asyncio.CancelledError:
                pass

    async def startup(
        self,
        *,
        host: str | None = None,
        port: int | None = None,
        sockets: list[_socket.socket] | None = None,
    ) -> None:
        """创建 TCP server 并开始监听。"""
        loop = asyncio.get_running_loop()

        def _create_protocol() -> HTTPProtocol:
            return HTTPProtocol(
                self.app,
                server_state=self._server_state,
                root_path=self.root_path,
            )

        if sockets:
            for sock in sockets:
                server = await loop.create_server(
                    _create_protocol,
                    sock=sock,
                )
                self._servers.append(server)
        elif host is not None and port is not None:
            server = await loop.create_server(
                _create_protocol,
                host=host,
                port=port,
                reuse_address=True,
            )
            self._servers.append(server)
        else:
            raise ValueError("Must provide either (host, port) or sockets")

        logger.info("ASGI server started (%d listener(s))", len(self._servers))

    async def main_loop(self) -> None:
        """主循环 — 等待退出信号。"""
        while not self.should_exit:
            await asyncio.sleep(0.1)

    async def shutdown(self, timeout: float = 10.0) -> None:
        """Graceful shutdown。"""
        logger.info("Shutting down...")

        # 1. 停止接受新连接
        for server in self._servers:
            server.close()

        # 2. 通知所有活跃连接关闭（必须在 wait_closed 之前）
        connections = set(self._server_state["connections"])
        if connections:
            logger.info("Shutting down %d connection(s)", len(connections))
            for conn in connections:
                conn.shutdown()

        # 3. 等待 TCP server 关闭（此时连接已在关闭中）
        for server in self._servers:
            await server.wait_closed()

        # 4. 等待剩余连接完成（带超时）
        remaining = self._server_state["connections"]
        if remaining:
            try:
                await asyncio.wait_for(
                    self._wait_connections_closed(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("Shutdown timeout — closing %d remaining connection(s)",
                               len(remaining))
                for conn in set(remaining):
                    conn.transport.close()

        logger.info("Server stopped")

    async def _wait_connections_closed(self) -> None:
        while self._server_state["connections"]:
            await asyncio.sleep(0.1)

    def _install_signal_handlers(self) -> None:
        """安装 SIGINT/SIGTERM 处理器。"""
        if sys.platform == "win32":
            # Windows: 只能用 signal.signal
            signal.signal(signal.SIGINT, self._handle_signal_sync)
            signal.signal(signal.SIGBREAK, self._handle_signal_sync)  # type: ignore[attr-defined]
        else:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGINT, self._handle_signal)
            loop.add_signal_handler(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self) -> None:
        """Unix 信号处理。"""
        if self.should_exit:
            # 第二次信号 → 强制退出
            self._force_exit = True
            print("\nForce shutting down...", flush=True)
            os._exit(0)
        self.should_exit = True
        print("\nShutting down gracefully... Press Ctrl+C again to force exit", flush=True)

    def _handle_signal_sync(self, signum: int, frame: Any) -> None:
        """Windows 同步信号处理。"""
        self._handle_signal()


async def scope_runner(app: Any, scope: dict[str, Any],
                       receive: Any, send: Any) -> None:
    """运行 ASGI app，捕获异常。"""
    try:
        await app(scope, receive, send)
    except Exception:
        logger.exception("ASGI lifespan raised exception")
