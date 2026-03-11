"""h11 HTTP/1.1 解析 + ASGI 桥接。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import unquote

import h11

logger = logging.getLogger("mutbot.server.http")

# 64 KiB — 超过此大小暂停读取，等待 app 消费
HIGH_WATER_LIMIT = 65536


class FlowControl:
    """Transport 层读写背压控制。"""

    __slots__ = ("_transport", "_is_writable", "_read_paused")

    def __init__(self, transport: asyncio.Transport) -> None:
        self._transport = transport
        self._is_writable = asyncio.Event()
        self._is_writable.set()
        self._read_paused = False

    def pause_reading(self) -> None:
        if not self._read_paused:
            self._read_paused = True
            self._transport.pause_reading()

    def resume_reading(self) -> None:
        if self._read_paused:
            self._read_paused = False
            self._transport.resume_reading()

    def pause_writing(self) -> None:
        self._is_writable.clear()

    def resume_writing(self) -> None:
        self._is_writable.set()

    async def drain(self) -> None:
        await self._is_writable.wait()


class HTTPProtocol(asyncio.Protocol):
    """每个 TCP 连接一个实例。解析 HTTP/1.1 请求，桥接到 ASGI app。"""

    def __init__(
        self,
        app: Any,
        *,
        server_state: dict[str, Any],
        root_path: str = "",
    ) -> None:
        self.app = app
        self.server_state = server_state
        self.root_path = root_path

        self.conn = h11.Connection(h11.SERVER)
        self.transport: asyncio.Transport = None  # type: ignore[assignment]
        self.flow: FlowControl = None  # type: ignore[assignment]

        self.client: tuple[str, int] | None = None
        self.server: tuple[str, int] | None = None

        self.cycle: RequestResponseCycle | None = None
        self.task: asyncio.Task[None] | None = None

        self._keep_alive = True
        self._timeout_handle: asyncio.TimerHandle | None = None

    # --- asyncio.Protocol callbacks ---

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self.flow = FlowControl(self.transport)

        self.server_state["connections"].add(self)

        peername = transport.get_extra_info("peername")
        if peername:
            self.client = (str(peername[0]), int(peername[1]))
        sockname = transport.get_extra_info("sockname")
        if sockname:
            self.server = (str(sockname[0]), int(sockname[1]))

        self._schedule_timeout()

    def connection_lost(self, exc: Exception | None) -> None:
        self.server_state["connections"].discard(self)
        self._cancel_timeout()

        if self.cycle and not self.cycle.response_complete:
            self.cycle.disconnected = True
            self.cycle._body_event.set()

        if self.task and not self.task.done():
            self.task.cancel()

    def pause_writing(self) -> None:
        self.flow.pause_writing()

    def resume_writing(self) -> None:
        self.flow.resume_writing()

    def data_received(self, data: bytes) -> None:
        self._cancel_timeout()
        self.conn.receive_data(data)
        self._handle_events()

    def eof_received(self) -> bool | None:
        # 返回 False 让 transport 关闭连接
        return False

    # --- HTTP event handling ---

    def _handle_events(self) -> None:
        while True:
            try:
                event = self.conn.next_event()
            except h11.RemoteProtocolError:
                self._send_error_response(400, "Bad Request")
                return

            if event is h11.NEED_DATA:
                break

            if event is h11.PAUSED:
                # HTTP 管线化 — 等待当前响应完成后再处理下一个请求
                self.flow.pause_reading()
                break

            if isinstance(event, h11.Request):
                self._handle_request(event)

            elif isinstance(event, h11.Data):
                if self.cycle:
                    self.cycle._body += event.data
                    if len(self.cycle._body) > HIGH_WATER_LIMIT:
                        self.flow.pause_reading()
                    self.cycle._body_event.set()

            elif isinstance(event, h11.EndOfMessage):
                if self.cycle:
                    self.cycle._more_body = False
                    self.cycle._body_event.set()

            elif isinstance(event, h11.ConnectionClosed):
                break

    def _handle_request(self, event: h11.Request) -> None:
        method = event.method.decode("ascii")
        target = event.target.decode("ascii")
        http_version = event.http_version.decode("ascii")

        # 拆分 path 和 query_string
        if "?" in target:
            raw_path, _, qs = target.partition("?")
        else:
            raw_path = target
            qs = ""

        path = unquote(raw_path)
        headers = [(k.lower(), v) for k, v in event.headers]

        # WebSocket upgrade 检测
        upgrade = None
        for name, value in headers:
            if name == b"upgrade":
                upgrade = value.lower()
                break

        if upgrade == b"websocket":
            self._handle_ws_upgrade(event, path, raw_path, qs, headers, http_version)
            return

        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": http_version,
            "server": self.server,
            "client": self.client,
            "scheme": "http",
            "method": method,
            "root_path": self.root_path,
            "path": path,
            "raw_path": raw_path.encode("ascii"),
            "query_string": qs.encode("ascii"),
            "headers": headers,
        }

        self.cycle = RequestResponseCycle(
            scope=scope,
            conn=self.conn,
            transport=self.transport,
            flow=self.flow,
            keep_alive=self._keep_alive,
            on_response_complete=self._on_response_complete,
        )
        self.task = asyncio.get_running_loop().create_task(
            self.cycle.run(self.app)
        )

    def _handle_ws_upgrade(
        self,
        event: h11.Request,
        path: str,
        raw_path: str,
        query_string: str,
        headers: list[tuple[bytes, bytes]],
        http_version: str,
    ) -> None:
        """WebSocket upgrade — 切换到 WSProtocol。"""
        from mutbot.server._ws import WSProtocol

        scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": http_version,
            "server": self.server,
            "client": self.client,
            "scheme": "ws",
            "root_path": self.root_path,
            "path": path,
            "raw_path": raw_path.encode("ascii"),
            "query_string": query_string.encode("ascii"),
            "headers": headers,
        }

        ws_protocol = WSProtocol(
            app=self.app,
            scope=scope,
            server_state=self.server_state,
        )

        # 重建原始 HTTP 请求头，交给 wsproto 完成握手
        raw_request = _reconstruct_raw_request(event)

        # 协议交接
        self.server_state["connections"].discard(self)
        self.transport.set_protocol(ws_protocol)
        ws_protocol.connection_made(self.transport)
        ws_protocol.data_received(raw_request)

    def _on_response_complete(self) -> None:
        """HTTP 响应完成后的处理。"""
        # h11 要求在 DONE 状态后调用 start_next_cycle
        self.conn.start_next_cycle()
        self.cycle = None
        self.task = None

        if self._keep_alive:
            self.flow.resume_reading()
            self._schedule_timeout()
            # 如果有管线化的数据缓冲，继续处理
            self._handle_events()
        else:
            self.transport.close()

    def _send_error_response(self, status_code: int, reason: str) -> None:
        """直接发送错误响应并关闭连接。"""
        body = f"{status_code} {reason}".encode()
        try:
            response = self.conn.send(h11.Response(
                status_code=status_code,
                headers=[(b"content-type", b"text/plain"),
                         (b"content-length", str(len(body)).encode()),
                         (b"connection", b"close")],
            ))
            data = self.conn.send(h11.Data(data=body))
            end = self.conn.send(h11.EndOfMessage())
            self.transport.write(response + data + end)
        except h11.LocalProtocolError:
            pass
        self.transport.close()

    def _schedule_timeout(self, seconds: float = 30.0) -> None:
        self._cancel_timeout()
        loop = asyncio.get_running_loop()
        self._timeout_handle = loop.call_later(seconds, self._on_timeout)

    def _cancel_timeout(self) -> None:
        if self._timeout_handle:
            self._timeout_handle.cancel()
            self._timeout_handle = None

    def _on_timeout(self) -> None:
        self.transport.close()

    def shutdown(self) -> None:
        """Graceful shutdown — 标记不再 keep-alive，等待当前请求完成。"""
        self._keep_alive = False
        if self.cycle is None or self.cycle.response_complete:
            self.transport.close()


class RequestResponseCycle:
    """单个 HTTP 请求/响应的 ASGI 桥接。"""

    __slots__ = (
        "scope", "conn", "transport", "flow", "keep_alive",
        "on_response_complete",
        "_body", "_body_event", "_more_body",
        "disconnected", "response_started", "response_complete",
        "_chunked", "_expected_content_length",
    )

    def __init__(
        self,
        scope: dict[str, Any],
        conn: h11.Connection,
        transport: asyncio.Transport,
        flow: FlowControl,
        keep_alive: bool,
        on_response_complete: Any,
    ) -> None:
        self.scope = scope
        self.conn = conn
        self.transport = transport
        self.flow = flow
        self.keep_alive = keep_alive
        self.on_response_complete = on_response_complete

        self._body = b""
        self._body_event = asyncio.Event()
        self._more_body = True
        self.disconnected = False
        self.response_started = False
        self.response_complete = False
        self._chunked = False
        self._expected_content_length: int | None = None

    async def run(self, app: Any) -> None:
        try:
            await app(self.scope, self.receive, self.send)
        except Exception:
            if not self.response_started:
                self._send_500()
            logger.exception("ASGI app raised exception for %s %s",
                             self.scope["method"], self.scope["path"])
        finally:
            if not self.response_complete:
                self.response_complete = True
                try:
                    self.on_response_complete()
                except Exception:
                    pass

    async def receive(self) -> dict[str, Any]:
        if self.disconnected:
            return {"type": "http.disconnect"}

        if not self._more_body:
            body = self._body
            self._body = b""
            return {"type": "http.request", "body": body, "more_body": False}

        # 等待数据到达
        await self._body_event.wait()
        self._body_event.clear()

        if self.disconnected:
            return {"type": "http.disconnect"}

        body = self._body
        self._body = b""
        self.flow.resume_reading()

        return {
            "type": "http.request",
            "body": body,
            "more_body": self._more_body,
        }

    async def send(self, message: dict[str, Any]) -> None:
        msg_type = message["type"]

        if msg_type == "http.response.start":
            await self._send_response_start(message)
        elif msg_type == "http.response.body":
            await self._send_response_body(message)

    async def _send_response_start(self, message: dict[str, Any]) -> None:
        self.response_started = True
        status_code: int = message["status"]
        headers: list[tuple[bytes, bytes]] = message.get("headers", [])

        # 检查 content-length 和 transfer-encoding
        has_content_length = False
        has_transfer_encoding = False
        for name, value in headers:
            low = name.lower()
            if low == b"content-length":
                has_content_length = True
                self._expected_content_length = int(value)
            elif low == b"transfer-encoding":
                has_transfer_encoding = True

        if not has_content_length and not has_transfer_encoding:
            self._chunked = True

        try:
            data = self.conn.send(h11.Response(
                status_code=status_code,
                headers=headers,
            ))
        except h11.LocalProtocolError as exc:
            logger.error("h11 protocol error sending response: %s", exc)
            self.transport.close()
            return

        await self.flow.drain()
        self.transport.write(data)

    async def _send_response_body(self, message: dict[str, Any]) -> None:
        body: bytes = message.get("body", b"")
        more_body: bool = message.get("more_body", False)

        if body:
            try:
                data = self.conn.send(h11.Data(data=body))
            except h11.LocalProtocolError:
                return
            await self.flow.drain()
            self.transport.write(data)

        if not more_body:
            try:
                data = self.conn.send(h11.EndOfMessage())
            except h11.LocalProtocolError:
                pass
            else:
                self.transport.write(data)

            self.response_complete = True
            self.on_response_complete()

    def _send_500(self) -> None:
        """发送 500 错误（仅在响应未开始时）。"""
        body = b"Internal Server Error"
        try:
            response = self.conn.send(h11.Response(
                status_code=500,
                headers=[(b"content-type", b"text/plain"),
                         (b"content-length", b"21"),
                         (b"connection", b"close")],
            ))
            data_bytes = self.conn.send(h11.Data(data=body))
            end = self.conn.send(h11.EndOfMessage())
            self.transport.write(response + data_bytes + end)
        except h11.LocalProtocolError:
            pass


def _reconstruct_raw_request(event: h11.Request) -> bytes:
    """从 h11.Request 重建原始 HTTP 请求（用于 WebSocket 协议交接）。"""
    lines: list[bytes] = []
    lines.append(event.method + b" " + event.target + b" HTTP/" + event.http_version)
    for name, value in event.headers:
        lines.append(name + b": " + value)
    lines.append(b"")
    lines.append(b"")
    return b"\r\n".join(lines)
