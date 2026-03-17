"""PtyHost ASGI WebSocket 应用。

处理 JSON 命令和 binary I/O。
新 binary 帧格式：[term_id 16B][view_id 8B][ANSI frame]。
日志通过 WebSocket 转发到 mutbot，支持 MCP 实时查询。
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from mutbot.ptyhost._manager import TerminalManager

logger = logging.getLogger("mutbot.ptyhost")


class _WebSocketLogHandler(logging.Handler):
    """将 ptyhost 进程的日志通过 WebSocket 转发到 mutbot。"""

    def __init__(self, app: PtyHostApp) -> None:
        super().__init__()
        self._app = app

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = json.dumps({
                "type": "log",
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            })
            loop = self._app._loop
            if loop is None:
                return
            for queue in list(self._app._connections.values()):
                loop.call_soon_threadsafe(queue.put_nowait, ("text", msg))
        except Exception:
            pass  # 日志转发失败不能抛异常


class PtyHostApp:
    """PTY 进程池 + pyte 渲染引擎 ASGI 应用。"""

    def __init__(self) -> None:
        self._manager = TerminalManager(
            on_frame=self._on_frame,
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
        # 日志转发 handler
        self._log_handler: _WebSocketLogHandler | None = None

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
                self._manager.set_loop(self._loop)
                # 安装日志转发 handler
                handler = _WebSocketLogHandler(self)
                handler.setLevel(logging.DEBUG)
                logging.getLogger("mutbot.ptyhost").addHandler(handler)
                self._log_handler = handler
                self._check_idle()
                await send({"type": "lifespan.startup.complete"})
            elif msg["type"] == "lifespan.shutdown":
                # 移除日志转发 handler
                if self._log_handler:
                    logging.getLogger("mutbot.ptyhost").removeHandler(self._log_handler)
                    self._log_handler = None
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

        elif action == "create_view":
            view_id = self._manager.create_view(cmd["term_id"])
            if view_id:
                return {"ok": True, "view_id": view_id}
            return {"ok": False, "error": "terminal not found"}

        elif action == "destroy_view":
            self._manager.destroy_view(cmd["view_id"])
            return {"ok": True}

        elif action == "snapshot":
            data = self._manager.get_snapshot(cmd["view_id"])
            view_id = cmd["view_id"]
            if data:
                view = self._manager._views.get(view_id)
                if view:
                    self._on_frame(view.term_id, view.id, data)
            return {"ok": True}

        elif action == "scroll":
            self._manager.scroll_view(cmd["view_id"], cmd["lines"])
            return {"ok": True}

        elif action == "scroll_to":
            self._manager.scroll_view_to(cmd["view_id"], cmd["offset"])
            return {"ok": True}

        elif action == "scroll_to_bottom":
            self._manager.scroll_view_to_bottom(cmd["view_id"])
            return {"ok": True}

        elif action == "clear_scrollback":
            self._manager.clear_scrollback(cmd["term_id"])
            return {"ok": True}

        elif action == "scroll_state":
            state = self._manager.get_scroll_state(cmd["view_id"])
            if state:
                return state
            return {"offset": 0, "total": 0, "visible": 0}

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
    # 回调（从事件循环线程调用）
    # ------------------------------------------------------------------

    def _on_frame(self, term_id: str, view_id: str, frame: bytes) -> None:
        """渲染帧 → 封装为 binary 帧广播到所有连接。

        帧格式：[term_id 16B raw UUID][view_id 8B raw][ANSI frame]
        """
        # term_id 是 32 字符 hex → 16 bytes
        header = bytes.fromhex(term_id) + view_id.encode("ascii").ljust(8, b"\0")[:8]
        msg = header + frame
        for queue in list(self._connections.values()):
            queue.put_nowait(("binary", msg))

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
