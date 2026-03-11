"""PtyHost ASGI WebSocket 应用。

处理 JSON 命令（create/resize/scrollback/status/list/kill）和 binary I/O。
binary 帧格式：[16 bytes UUID] [raw data]。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

from mutbot.ptyhost._manager import TerminalManager

logger = logging.getLogger("mutbot.ptyhost")


class PtyHostApp:
    """纯 PTY 进程池 ASGI 应用。"""

    def __init__(self) -> None:
        self._manager = TerminalManager(
            on_output=self._on_output,
            on_exit=self._on_exit,
        )
        # 活跃 WebSocket 连接：conn_id → send queue
        self._connections: dict[int, asyncio.Queue[tuple[str, Any]]] = {}
        self._next_conn_id = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        # 空闲退出
        self._idle_task: asyncio.Task[None] | None = None
        self._idle_seconds: float = 60.0
        # 由 __main__ 设置，用于触发退出
        self.should_exit_callback: Any = None

    # ------------------------------------------------------------------
    # ASGI 入口
    # ------------------------------------------------------------------

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any,
    ) -> None:
        if scope["type"] == "lifespan":
            await self._handle_lifespan(scope, receive, send)
        elif scope["type"] == "websocket":
            await self._handle_ws(scope, receive, send)

    async def _handle_lifespan(
        self, scope: dict[str, Any], receive: Any, send: Any,
    ) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                self._loop = asyncio.get_running_loop()
                self._check_idle()
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                self._manager.kill_all()
                await send({"type": "lifespan.shutdown.complete"})
                return

    # ------------------------------------------------------------------
    # WebSocket 处理
    # ------------------------------------------------------------------

    async def _handle_ws(
        self, scope: dict[str, Any], receive: Any, send: Any,
    ) -> None:
        msg = await receive()
        if msg["type"] != "websocket.connect":
            return
        await send({"type": "websocket.accept"})

        conn_id = self._next_conn_id
        self._next_conn_id += 1
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self._connections[conn_id] = queue
        self._cancel_idle()

        sender = asyncio.create_task(self._sender(queue, send))
        try:
            while True:
                msg = await receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg["type"] == "websocket.receive":
                    text = msg.get("text")
                    data = msg.get("bytes")
                    if text:
                        cmd = json.loads(text)
                        reply = self._handle_command(cmd)
                        if "seq" in cmd:
                            reply["seq"] = cmd["seq"]
                        await send({
                            "type": "websocket.send",
                            "text": json.dumps(reply),
                        })
                    elif data:
                        self._handle_binary(data)
        finally:
            sender.cancel()
            self._connections.pop(conn_id, None)
            self._check_idle()

    async def _sender(
        self, queue: asyncio.Queue[tuple[str, Any]], send: Any,
    ) -> None:
        """后台任务：将 PTY 输出/事件发送到 WebSocket 客户端。"""
        try:
            while True:
                msg_type, payload = await queue.get()
                if msg_type == "binary":
                    await send({"type": "websocket.send", "bytes": payload})
                else:
                    await send({"type": "websocket.send", "text": payload})
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------
    # JSON 命令处理
    # ------------------------------------------------------------------

    def _handle_command(self, cmd: dict[str, Any]) -> dict[str, Any]:
        action = cmd.get("cmd")

        if action == "create":
            try:
                term_id = self._manager.create(
                    rows=cmd.get("rows", 24),
                    cols=cmd.get("cols", 80),
                    cwd=cmd.get("cwd"),
                )
                self._cancel_idle()
                return {"ok": True, "term_id": term_id}
            except Exception as e:
                return {"ok": False, "error": str(e)}

        elif action == "resize":
            result = self._manager.resize(
                cmd["term_id"], cmd["rows"], cmd["cols"],
            )
            if result:
                return {"ok": True, "rows": result[0], "cols": result[1]}
            return {"ok": False, "error": "terminal not found"}

        elif action == "scrollback":
            data = self._manager.get_scrollback(cmd["term_id"])
            return {
                "term_id": cmd["term_id"],
                "data_b64": base64.b64encode(data).decode(),
            }

        elif action == "status":
            info = self._manager.status(cmd["term_id"])
            if info:
                return info
            return {"alive": False, "exit_code": None, "rows": 0, "cols": 0}

        elif action == "list":
            return {"terminals": self._manager.list_all()}

        elif action == "kill":
            self._manager.kill(cmd["term_id"])
            self._check_idle()
            return {"ok": True}

        return {"ok": False, "error": f"unknown command: {action}"}

    # ------------------------------------------------------------------
    # Binary I/O
    # ------------------------------------------------------------------

    def _handle_binary(self, data: bytes) -> None:
        """处理 binary 帧：[16 bytes UUID] [payload] → 写入 PTY。"""
        if len(data) < 16:
            return
        term_id = data[:16].hex()
        payload = data[16:]
        self._manager.write(term_id, payload)

    # ------------------------------------------------------------------
    # PTY 回调（从 reader 线程调用）
    # ------------------------------------------------------------------

    def _on_output(self, term_id: str, data: bytes) -> None:
        """PTY 输出 → 广播 binary 帧到所有连接。"""
        frame = bytes.fromhex(term_id) + data
        loop = self._loop
        if loop is None:
            return
        for queue in list(self._connections.values()):
            loop.call_soon_threadsafe(queue.put_nowait, ("binary", frame))

    def _on_exit(self, term_id: str, exit_code: int | None) -> None:
        """PTY 退出 → 广播 exit 事件到所有连接。"""
        msg = json.dumps({"type": "exit", "term_id": term_id, "exit_code": exit_code})
        loop = self._loop
        if loop is None:
            return
        for queue in list(self._connections.values()):
            loop.call_soon_threadsafe(queue.put_nowait, ("text", msg))
        # 检查是否需要空闲退出
        loop.call_soon_threadsafe(self._check_idle)

    # ------------------------------------------------------------------
    # 空闲退出
    # ------------------------------------------------------------------

    def _check_idle(self) -> None:
        """无终端且无连接时启动空闲退出计时器。"""
        if self._manager.count == 0 and len(self._connections) == 0:
            if self._idle_task is None or self._idle_task.done():
                loop = self._loop
                if loop:
                    self._idle_task = loop.create_task(self._idle_timer())
        else:
            self._cancel_idle()

    def _cancel_idle(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()
            self._idle_task = None

    async def _idle_timer(self) -> None:
        try:
            await asyncio.sleep(self._idle_seconds)
            # 再次确认仍然空闲
            if self._manager.count == 0 and len(self._connections) == 0:
                logger.info("Idle timeout (%.0fs), exiting", self._idle_seconds)
                if self.should_exit_callback:
                    self.should_exit_callback()
        except asyncio.CancelledError:
            pass
