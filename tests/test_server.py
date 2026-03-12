"""mutbot.server 模块测试

涵盖：
- HTTP/1.1 请求/响应（GET, POST, 各种状态码）
- Keep-alive 连接复用
- WebSocket 升级与双向通信
- SSE 格式化
- ASGI lifespan 协议
- Graceful shutdown
- 错误处理（malformed request, app 异常）
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from mutagent.net.asgi import Server
from mutagent.net._protocol import HTTPProtocol, RequestResponseCycle, FlowControl, WSProtocol, format_sse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _simple_app(scope: dict, receive: Any, send: Any) -> None:
    """最简 ASGI app：200 响应 + echo path。"""
    if scope["type"] == "lifespan":
        msg = await receive()
        if msg["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        msg = await receive()
        if msg["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
        return

    if scope["type"] == "http":
        body = f'{scope["method"]} {scope["path"]}'.encode()
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/plain"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })

    elif scope["type"] == "websocket":
        msg = await receive()
        assert msg["type"] == "websocket.connect"
        await send({"type": "websocket.accept"})

        # Echo loop
        while True:
            msg = await receive()
            if msg["type"] == "websocket.disconnect":
                break
            if "text" in msg:
                await send({"type": "websocket.send", "text": f"echo: {msg['text']}"})
            elif "bytes" in msg:
                await send({"type": "websocket.send", "bytes": msg["bytes"]})


async def _error_app(scope: dict, receive: Any, send: Any) -> None:
    """抛出异常的 ASGI app。"""
    if scope["type"] == "lifespan":
        msg = await receive()
        if msg["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        msg = await receive()
        if msg["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
        return
    raise RuntimeError("intentional error")


async def _post_echo_app(scope: dict, receive: Any, send: Any) -> None:
    """Echo POST body。"""
    if scope["type"] == "lifespan":
        msg = await receive()
        if msg["type"] == "lifespan.startup":
            await send({"type": "lifespan.startup.complete"})
        msg = await receive()
        if msg["type"] == "lifespan.shutdown":
            await send({"type": "lifespan.shutdown.complete"})
        return

    if scope["type"] == "http":
        # 收集完整 body
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break

        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"application/octet-stream"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })


async def _start_server(app: Any, port: int) -> Server:
    """启动 server 并返回（不阻塞，用于测试）。"""
    server = Server(app)
    await server._lifespan_startup()
    assert not server._lifespan_startup_failed
    await server.startup(host="127.0.0.1", port=port)
    return server


async def _stop_server(server: Server) -> None:
    """停止 server。"""
    await server.shutdown(timeout=3)
    await server._lifespan_shutdown()


async def _http_request(
    port: int,
    method: str = "GET",
    path: str = "/",
    headers: list[tuple[str, str]] | None = None,
    body: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    """发送 HTTP 请求，返回 (status, headers_dict, body)。"""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        # 构建请求
        lines = [f"{method} {path} HTTP/1.1", "Host: localhost"]
        if headers:
            for name, value in headers:
                lines.append(f"{name}: {value}")
        if body is not None:
            lines.append(f"Content-Length: {len(body)}")
        lines.append("")
        lines.append("")
        request = "\r\n".join(lines).encode()
        if body:
            request += body

        writer.write(request)
        await writer.drain()

        # 读取响应
        response_data = await asyncio.wait_for(reader.read(65536), timeout=5)

        # 解析
        header_end = response_data.find(b"\r\n\r\n")
        header_part = response_data[:header_end].decode()
        resp_body = response_data[header_end + 4:]

        lines_list = header_part.split("\r\n")
        status_line = lines_list[0]
        status_code = int(status_line.split(" ", 2)[1])

        resp_headers: dict[str, str] = {}
        for line in lines_list[1:]:
            if ": " in line:
                name, value = line.split(": ", 1)
                resp_headers[name.lower()] = value

        return status_code, resp_headers, resp_body
    finally:
        writer.close()
        await writer.wait_closed()


# ---------------------------------------------------------------------------
# SSE 格式化测试
# ---------------------------------------------------------------------------

class TestSSE:
    def test_basic(self):
        result = format_sse("hello")
        assert result == b"data: hello\n\n"

    def test_with_event_and_id(self):
        result = format_sse("payload", event="update", id="42")
        assert b"id: 42\n" in result
        assert b"event: update\n" in result
        assert b"data: payload\n" in result

    def test_multiline_data(self):
        result = format_sse("line1\nline2\nline3")
        assert b"data: line1\n" in result
        assert b"data: line2\n" in result
        assert b"data: line3\n" in result


# ---------------------------------------------------------------------------
# HTTP 测试
# ---------------------------------------------------------------------------

_BASE_PORT = 19100  # 测试用端口基数，每个测试用不同端口避免冲突


class TestHTTP:
    @pytest.mark.asyncio
    async def test_get_request(self):
        port = _BASE_PORT + 1
        server = await _start_server(_simple_app, port)
        try:
            status, headers, body = await _http_request(port, "GET", "/hello")
            assert status == 200
            assert body == b"GET /hello"
            assert headers["content-type"] == "text/plain"
        finally:
            await _stop_server(server)

    @pytest.mark.asyncio
    async def test_post_with_body(self):
        port = _BASE_PORT + 2
        server = await _start_server(_post_echo_app, port)
        try:
            status, headers, body = await _http_request(
                port, "POST", "/upload", body=b"hello world",
            )
            assert status == 200
            assert body == b"hello world"
        finally:
            await _stop_server(server)

    @pytest.mark.asyncio
    async def test_query_string(self):
        port = _BASE_PORT + 3
        server = await _start_server(_simple_app, port)
        try:
            status, _, body = await _http_request(port, "GET", "/search?q=test&page=1")
            assert status == 200
            assert body == b"GET /search"
        finally:
            await _stop_server(server)

    @pytest.mark.asyncio
    async def test_app_exception_returns_500(self):
        port = _BASE_PORT + 4
        server = await _start_server(_error_app, port)
        try:
            status, _, _ = await _http_request(port, "GET", "/fail")
            assert status == 500
        finally:
            await _stop_server(server)

    @pytest.mark.asyncio
    async def test_keep_alive(self):
        """同一 TCP 连接上发送两个请求。"""
        port = _BASE_PORT + 5
        server = await _start_server(_simple_app, port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            # 第一个请求
            writer.write(b"GET /first HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            resp1 = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert b"200" in resp1
            assert b"GET /first" in resp1

            # 第二个请求（同一连接）
            writer.write(b"GET /second HTTP/1.1\r\nHost: localhost\r\n\r\n")
            await writer.drain()
            resp2 = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert b"GET /second" in resp2

            writer.close()
            await writer.wait_closed()
        finally:
            await _stop_server(server)


# ---------------------------------------------------------------------------
# WebSocket 测试
# ---------------------------------------------------------------------------

class TestWebSocket:
    @pytest.mark.asyncio
    async def test_upgrade_and_echo(self):
        """WebSocket 握手 + 文本消息 echo。"""
        port = _BASE_PORT + 10
        server = await _start_server(_simple_app, port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            # 发送 upgrade 请求
            ws_request = (
                b"GET /ws HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                b"Sec-WebSocket-Version: 13\r\n"
                b"\r\n"
            )
            writer.write(ws_request)
            await writer.drain()

            # 读取 101 Switching Protocols 响应
            resp = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert b"101" in resp

            # 发送 text frame
            import struct
            text = b"hello ws"
            # WebSocket frame: FIN=1, opcode=1 (text), mask=1
            frame = bytearray()
            frame.append(0x81)  # FIN + text
            frame.append(0x80 | len(text))  # masked + length
            mask = b"\x00\x00\x00\x00"  # 简单 mask
            frame.extend(mask)
            frame.extend(text)  # mask key 全 0，所以 payload 不变

            writer.write(bytes(frame))
            await writer.drain()

            # 读取 echo 响应 frame
            resp = await asyncio.wait_for(reader.read(4096), timeout=5)
            # 响应应包含 "echo: hello ws"
            assert b"echo: hello ws" in resp

            writer.close()
            await writer.wait_closed()
        finally:
            await _stop_server(server)

    @pytest.mark.asyncio
    async def test_binary_echo(self):
        """WebSocket 二进制消息 echo。"""
        port = _BASE_PORT + 11
        server = await _start_server(_simple_app, port)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", port)

            # 握手
            ws_request = (
                b"GET /ws HTTP/1.1\r\n"
                b"Host: localhost\r\n"
                b"Upgrade: websocket\r\n"
                b"Connection: Upgrade\r\n"
                b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
                b"Sec-WebSocket-Version: 13\r\n"
                b"\r\n"
            )
            writer.write(ws_request)
            await writer.drain()
            resp = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert b"101" in resp

            # 发送 binary frame
            data = b"\x00\x01\x02\x03"
            frame = bytearray()
            frame.append(0x82)  # FIN + binary
            frame.append(0x80 | len(data))  # masked
            frame.extend(b"\x00\x00\x00\x00")  # mask
            frame.extend(data)

            writer.write(bytes(frame))
            await writer.drain()

            resp = await asyncio.wait_for(reader.read(4096), timeout=5)
            assert data in resp

            writer.close()
            await writer.wait_closed()
        finally:
            await _stop_server(server)


# ---------------------------------------------------------------------------
# Lifespan 测试
# ---------------------------------------------------------------------------

class TestLifespan:
    @pytest.mark.asyncio
    async def test_startup_shutdown(self):
        """Lifespan startup/shutdown 事件正确触发。"""
        events: list[str] = []

        async def tracking_app(scope: dict, receive: Any, send: Any) -> None:
            if scope["type"] == "lifespan":
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    events.append("startup")
                    await send({"type": "lifespan.startup.complete"})
                msg = await receive()
                if msg["type"] == "lifespan.shutdown":
                    events.append("shutdown")
                    await send({"type": "lifespan.shutdown.complete"})
                return
            # 普通 HTTP
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        port = _BASE_PORT + 20
        server = Server(tracking_app)
        await server._lifespan_startup()
        assert not server._lifespan_startup_failed
        assert "startup" in events

        await server.startup(host="127.0.0.1", port=port)
        await server.shutdown(timeout=2)
        await server._lifespan_shutdown()
        assert "shutdown" in events

    @pytest.mark.asyncio
    async def test_startup_failed(self):
        """Lifespan startup 失败时 server 不启动。"""
        async def failing_app(scope: dict, receive: Any, send: Any) -> None:
            if scope["type"] == "lifespan":
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({
                        "type": "lifespan.startup.failed",
                        "message": "DB connection failed",
                    })
                return

        server = Server(failing_app)
        await server._lifespan_startup()
        assert server._lifespan_startup_failed


# ---------------------------------------------------------------------------
# Server 管理测试
# ---------------------------------------------------------------------------

class TestServerManagement:
    @pytest.mark.asyncio
    async def test_graceful_shutdown_closes_connections(self):
        """Graceful shutdown 关闭所有活跃连接。"""
        port = _BASE_PORT + 30
        server = await _start_server(_simple_app, port)

        # 建立一个连接
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")
        await writer.drain()
        resp = await asyncio.wait_for(reader.read(4096), timeout=5)
        assert b"200" in resp

        # Shutdown
        await _stop_server(server)

        # 连接应已关闭
        data = await reader.read(1)
        assert data == b""  # EOF
        writer.close()

    @pytest.mark.asyncio
    async def test_multiple_sockets(self):
        """支持多个监听地址。"""
        import socket

        port1 = _BASE_PORT + 31
        port2 = _BASE_PORT + 32

        sock1 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock1.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock1.bind(("127.0.0.1", port1))

        sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock2.bind(("127.0.0.1", port2))

        server = Server(_simple_app)
        await server._lifespan_startup()
        await server.startup(sockets=[sock1, sock2])

        try:
            # 两个端口都应该可以访问
            status1, _, body1 = await _http_request(port1, "GET", "/port1")
            assert status1 == 200
            assert body1 == b"GET /port1"

            status2, _, body2 = await _http_request(port2, "GET", "/port2")
            assert status2 == 200
            assert body2 == b"GET /port2"
        finally:
            await server.shutdown(timeout=2)
            await server._lifespan_shutdown()


# ---------------------------------------------------------------------------
# FlowControl 单元测试
# ---------------------------------------------------------------------------

class TestFlowControl:
    def test_initial_state(self):
        """初始状态：可写，未暂停读取。"""
        transport = _MockTransport()
        fc = FlowControl(transport)
        assert fc._is_writable.is_set()
        assert not fc._read_paused

    def test_pause_resume_reading(self):
        transport = _MockTransport()
        fc = FlowControl(transport)
        fc.pause_reading()
        assert fc._read_paused
        assert transport.reading_paused
        fc.resume_reading()
        assert not fc._read_paused
        assert not transport.reading_paused

    def test_pause_resume_writing(self):
        transport = _MockTransport()
        fc = FlowControl(transport)
        fc.pause_writing()
        assert not fc._is_writable.is_set()
        fc.resume_writing()
        assert fc._is_writable.is_set()

    @pytest.mark.asyncio
    async def test_drain_waits_for_writable(self):
        transport = _MockTransport()
        fc = FlowControl(transport)
        fc.pause_writing()

        drained = False

        async def do_drain():
            nonlocal drained
            await fc.drain()
            drained = True

        task = asyncio.create_task(do_drain())
        await asyncio.sleep(0.05)
        assert not drained

        fc.resume_writing()
        await asyncio.sleep(0.05)
        assert drained
        await task


class _MockTransport:
    """asyncio.Transport 的最小 mock。"""

    def __init__(self):
        self.reading_paused = False
        self.written = bytearray()
        self.closed = False

    def pause_reading(self):
        self.reading_paused = True

    def resume_reading(self):
        self.reading_paused = False

    def write(self, data: bytes):
        self.written.extend(data)

    def close(self):
        self.closed = True

    def get_extra_info(self, name, default=None):
        if name == "peername":
            return ("127.0.0.1", 9999)
        if name == "sockname":
            return ("127.0.0.1", 8000)
        return default
