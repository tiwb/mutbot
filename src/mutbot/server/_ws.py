"""WebSocket ASGI 桥接（基于 wsproto）。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import wsproto
import wsproto.events as ws_events

logger = logging.getLogger("mutbot.server.ws")

# WebSocket 消息队列上限 — 超过后暂停读取
MAX_QUEUE_SIZE = 16


class WSProtocol(asyncio.Protocol):
    """WebSocket 连接处理器。

    由 HTTPProtocol 在检测到 upgrade 后创建，通过 transport.set_protocol() 交接。
    """

    def __init__(
        self,
        app: Any,
        scope: dict[str, Any],
        *,
        server_state: dict[str, Any],
    ) -> None:
        self.app = app
        self.scope = scope
        self.server_state = server_state

        self.transport: asyncio.Transport = None  # type: ignore[assignment]
        self.ws: wsproto.WSConnection = wsproto.WSConnection(wsproto.ConnectionType.SERVER)
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.task: asyncio.Task[None] | None = None

        self._handshake_complete = False
        self._closed = False
        self._close_sent = False
        self._text_buffer: list[str] = []
        self._bytes_buffer: list[bytes] = []

    # --- asyncio.Protocol callbacks ---

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self.server_state["connections"].add(self)

    def connection_lost(self, exc: Exception | None) -> None:
        self.server_state["connections"].discard(self)
        self._closed = True
        self.queue.put_nowait({"type": "websocket.disconnect", "code": 1006})

        if self.task and not self.task.done():
            self.task.cancel()

    def data_received(self, data: bytes) -> None:
        self.ws.receive_data(data)
        self._handle_events()

    def eof_received(self) -> bool | None:
        return False

    # --- wsproto event handling ---

    def _handle_events(self) -> None:
        for event in self.ws.events():
            if isinstance(event, ws_events.Request):
                self._handle_connect(event)

            elif isinstance(event, ws_events.TextMessage):
                self._handle_text(event)

            elif isinstance(event, ws_events.BytesMessage):
                self._handle_bytes(event)

            elif isinstance(event, ws_events.CloseConnection):
                self._handle_close(event)

            elif isinstance(event, ws_events.Ping):
                # 自动回复 pong
                self.transport.write(self.ws.send(ws_events.Pong(payload=event.payload)))

    def _handle_connect(self, event: ws_events.Request) -> None:
        # 将 connect 事件入队，启动 ASGI task
        self.queue.put_nowait({"type": "websocket.connect"})
        loop = asyncio.get_running_loop()
        self.task = loop.create_task(self._run_asgi())

    def _handle_text(self, event: ws_events.TextMessage) -> None:
        self._text_buffer.append(event.data)
        if event.message_finished:
            text = "".join(self._text_buffer)
            self._text_buffer.clear()
            self._enqueue({"type": "websocket.receive", "text": text})

    def _handle_bytes(self, event: ws_events.BytesMessage) -> None:
        self._bytes_buffer.append(event.data)
        if event.message_finished:
            data = b"".join(self._bytes_buffer)
            self._bytes_buffer.clear()
            self._enqueue({"type": "websocket.receive", "bytes": data})

    def _handle_close(self, event: ws_events.CloseConnection) -> None:
        code = event.code or 1000
        if not self._close_sent:
            self._close_sent = True
            # 回复 close 帧
            try:
                data = self.ws.send(ws_events.CloseConnection(code=code))
                self.transport.write(data)
            except Exception:
                pass
        self._closed = True
        self.queue.put_nowait({"type": "websocket.disconnect", "code": code})

    def _enqueue(self, message: dict[str, Any]) -> None:
        self.queue.put_nowait(message)
        # 背压：队列满则暂停读取
        if self.queue.qsize() >= MAX_QUEUE_SIZE:
            self.transport.pause_reading()

    # --- ASGI interface ---

    async def _run_asgi(self) -> None:
        try:
            await self.app(self.scope, self.receive, self.send)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("ASGI app raised exception for WebSocket %s",
                             self.scope["path"])
        finally:
            if not self._closed:
                self.transport.close()

    async def receive(self) -> dict[str, Any]:
        msg = await self.queue.get()
        # 恢复读取
        if self.queue.qsize() < MAX_QUEUE_SIZE:
            try:
                self.transport.resume_reading()
            except Exception:
                pass
        return msg

    async def send(self, message: dict[str, Any]) -> None:
        msg_type = message["type"]

        if msg_type == "websocket.accept":
            await self._send_accept(message)

        elif msg_type == "websocket.send":
            await self._send_data(message)

        elif msg_type == "websocket.close":
            await self._send_close(message)

        elif msg_type == "websocket.http.response.start":
            # 拒绝 WebSocket — 发送 HTTP 响应
            await self._send_http_reject_start(message)

        elif msg_type == "websocket.http.response.body":
            await self._send_http_reject_body(message)

    async def _send_accept(self, message: dict[str, Any]) -> None:
        headers = message.get("headers", [])
        subprotocol = message.get("subprotocol")

        extra_headers = [(k, v) for k, v in headers]
        data = self.ws.send(ws_events.AcceptConnection(
            subprotocol=subprotocol,
            extra_headers=extra_headers,
        ))
        self.transport.write(data)
        self._handshake_complete = True

    async def _send_data(self, message: dict[str, Any]) -> None:
        if "text" in message:
            data = self.ws.send(ws_events.TextMessage(data=message["text"]))
        elif "bytes" in message:
            data = self.ws.send(ws_events.BytesMessage(data=message["bytes"]))
        else:
            return
        self.transport.write(data)

    async def _send_close(self, message: dict[str, Any]) -> None:
        code = message.get("code", 1000)
        reason = message.get("reason", "")
        if not self._close_sent:
            self._close_sent = True
            try:
                data = self.ws.send(ws_events.CloseConnection(code=code, reason=reason))
                self.transport.write(data)
            except Exception:
                pass
        self.transport.close()

    async def _send_http_reject_start(self, message: dict[str, Any]) -> None:
        """Reject WebSocket upgrade with HTTP response (e.g. 403)."""
        status = message["status"]
        headers = message.get("headers", [])
        # 构建 HTTP 响应拒绝 WebSocket
        data = self.ws.send(ws_events.RejectConnection(
            status_code=status,
            headers=headers,
            has_body=True,
        ))
        self.transport.write(data)

    async def _send_http_reject_body(self, message: dict[str, Any]) -> None:
        body = message.get("body", b"")
        if body:
            data = self.ws.send(ws_events.RejectData(data=body))
            self.transport.write(data)
        if not message.get("more_body", False):
            self.transport.close()

    def shutdown(self) -> None:
        """Graceful shutdown — 发送 1012 close 帧。"""
        if self._handshake_complete and not self._close_sent:
            self._close_sent = True
            try:
                data = self.ws.send(ws_events.CloseConnection(
                    code=1012,  # Service Restart
                    reason="Server shutting down",
                ))
                self.transport.write(data)
            except Exception:
                pass
        self.transport.close()
