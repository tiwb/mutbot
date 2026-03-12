"""Web 框架基础 — View / WebSocketView / Request / Response / Router。

轻量 ASGI 框架层，替代 FastAPI + Starlette。
基于 mutobj.Declaration 自动发现，支持路径参数和静态文件。
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, unquote

import mutobj

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / Response
# ---------------------------------------------------------------------------

class Request:
    """HTTP 请求封装。"""

    __slots__ = (
        "method", "path", "raw_path", "headers", "query_params",
        "path_params", "_scope", "_receive", "_body",
    )

    def __init__(
        self,
        scope: dict[str, Any],
        receive: Any,
        *,
        path_params: dict[str, str] | None = None,
    ) -> None:
        self._scope = scope
        self._receive = receive
        self._body: bytes | None = None

        self.method: str = scope.get("method", "GET")
        self.path: str = scope.get("path", "/")
        self.raw_path: str = scope.get("raw_path", self.path.encode()).decode("latin-1") if isinstance(scope.get("raw_path"), bytes) else self.path

        # headers: list[(bytes, bytes)] → dict[str, str] (小写 key)
        raw_headers = scope.get("headers", [])
        self.headers: dict[str, str] = {
            k.decode("latin-1"): v.decode("latin-1")
            for k, v in raw_headers
        }

        # query_params
        qs = scope.get("query_string", b"")
        if isinstance(qs, bytes):
            qs = qs.decode("latin-1")
        parsed = parse_qs(qs, keep_blank_values=True)
        self.query_params: dict[str, str] = {
            k: v[0] for k, v in parsed.items()
        }

        self.path_params: dict[str, str] = path_params or {}

    async def body(self) -> bytes:
        if self._body is not None:
            return self._body
        chunks: list[bytes] = []
        while True:
            msg = await self._receive()
            chunks.append(msg.get("body", b""))
            if not msg.get("more_body", False):
                break
        self._body = b"".join(chunks)
        return self._body

    async def json(self) -> Any:
        raw = await self.body()
        return json.loads(raw)


class Response:
    """HTTP 响应。"""

    __slots__ = ("status", "headers", "body")

    def __init__(
        self,
        status: int = 200,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.headers = headers or {}

    async def send(self, send_fn: Any) -> None:
        raw_headers: list[tuple[bytes, bytes]] = [
            (k.encode(), v.encode()) for k, v in self.headers.items()
        ]
        if "content-length" not in self.headers:
            raw_headers.append((b"content-length", str(len(self.body)).encode()))
        await send_fn({
            "type": "http.response.start",
            "status": self.status,
            "headers": raw_headers,
        })
        await send_fn({"type": "http.response.body", "body": self.body})


def json_response(data: Any, status: int = 200) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode("utf-8")
    return Response(
        status=status,
        body=body,
        headers={"content-type": "application/json; charset=utf-8"},
    )


def html_response(html: str, status: int = 200) -> Response:
    body = html.encode("utf-8")
    return Response(
        status=status,
        body=body,
        headers={"content-type": "text/html; charset=utf-8"},
    )


class StreamingResponse:
    """流式 HTTP 响应。"""

    __slots__ = ("status", "headers", "body_iterator")

    def __init__(
        self,
        body_iterator: AsyncIterator[bytes],
        *,
        status: int = 200,
        media_type: str = "text/event-stream",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.headers.setdefault("content-type", media_type)
        self.body_iterator = body_iterator

    async def send(self, send_fn: Any) -> None:
        raw_headers: list[tuple[bytes, bytes]] = [
            (k.encode(), v.encode()) for k, v in self.headers.items()
        ]
        await send_fn({
            "type": "http.response.start",
            "status": self.status,
            "headers": raw_headers,
        })
        async for chunk in self.body_iterator:
            await send_fn({
                "type": "http.response.body",
                "body": chunk if isinstance(chunk, bytes) else chunk.encode(),
                "more_body": True,
            })
        await send_fn({"type": "http.response.body", "body": b"", "more_body": False})


# ---------------------------------------------------------------------------
# WebSocketConnection
# ---------------------------------------------------------------------------

class WebSocketDisconnect(Exception):
    """WebSocket 正常断开异常。"""
    def __init__(self, code: int = 1000) -> None:
        self.code = code
        super().__init__(f"WebSocket disconnected (code={code})")


class WebSocketConnection:
    """WebSocket 连接封装。"""

    __slots__ = (
        "path", "query_params", "path_params",
        "_scope", "_receive", "_send",
    )

    def __init__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
        *,
        path_params: dict[str, str] | None = None,
    ) -> None:
        self._scope = scope
        self._receive = receive
        self._send = send

        self.path: str = scope.get("path", "/")

        qs = scope.get("query_string", b"")
        if isinstance(qs, bytes):
            qs = qs.decode("latin-1")
        parsed = parse_qs(qs, keep_blank_values=True)
        self.query_params: dict[str, str] = {
            k: v[0] for k, v in parsed.items()
        }
        self.path_params: dict[str, str] = path_params or {}

    async def accept(self) -> None:
        await self._send({"type": "websocket.accept"})

    async def receive(self) -> dict[str, Any]:
        return await self._receive()

    async def receive_json(self) -> Any:
        while True:
            msg = await self._receive()
            if msg.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(msg.get("code", 1000))
            if "text" in msg:
                return json.loads(msg["text"])
            # binary 等其他帧类型 → 跳过

    async def send_json(self, data: Any) -> None:
        await self._send({
            "type": "websocket.send",
            "text": json.dumps(data, ensure_ascii=False),
        })

    async def send_bytes(self, data: bytes) -> None:
        await self._send({"type": "websocket.send", "bytes": data})

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self._send({
            "type": "websocket.close",
            "code": code,
            "reason": reason,
        })


# ---------------------------------------------------------------------------
# View / WebSocketView Declaration
# ---------------------------------------------------------------------------

class View(mutobj.Declaration):
    """HTTP 路由声明基类。一个 path 一个类，方法名 = HTTP method。"""

    path: str = ""

    async def get(self, request: Request) -> Response | StreamingResponse:
        return Response(status=405)

    async def post(self, request: Request) -> Response | StreamingResponse:
        return Response(status=405)

    async def put(self, request: Request) -> Response | StreamingResponse:
        return Response(status=405)

    async def delete(self, request: Request) -> Response | StreamingResponse:
        return Response(status=405)


class WebSocketView(mutobj.Declaration):
    """WebSocket 路由声明基类。"""

    path: str = ""

    async def connect(self, ws: WebSocketConnection) -> None:
        """WebSocket 生命周期入口。方法返回即断开。"""
        await ws.close(code=4405, reason="Not implemented")


# ---------------------------------------------------------------------------
# Router (ASGI app)
# ---------------------------------------------------------------------------

# 匹配 {param_name} 路径参数
_PARAM_RE = re.compile(r"\{(\w+)\}")


def _compile_path(path: str) -> tuple[re.Pattern[str], list[str]]:
    """将 /foo/{bar}/baz 编译为正则 + 参数名列表。"""
    param_names: list[str] = []
    regex_parts: list[str] = []
    last_end = 0
    for m in _PARAM_RE.finditer(path):
        regex_parts.append(re.escape(path[last_end:m.start()]))
        regex_parts.append(r"([^/]+)")
        param_names.append(m.group(1))
        last_end = m.end()
    regex_parts.append(re.escape(path[last_end:]))
    pattern = re.compile("^" + "".join(regex_parts) + "$")
    return pattern, param_names


class _Route:
    __slots__ = ("path", "pattern", "param_names", "handler", "is_ws", "_auto")

    def __init__(self, path: str, handler: Any, *, is_ws: bool = False) -> None:
        self.path = path
        self.pattern, self.param_names = _compile_path(path)
        self.handler = handler
        self.is_ws = is_ws


class Router:
    """ASGI app — 路由分发 + 静态文件 fallback。

    通过 mutobj.discover_subclasses 自动发现 View/WebSocketView，
    也支持手动 add / add_ws。
    """

    def __init__(self) -> None:
        self._routes: list[_Route] = []
        self._static_dirs: list[tuple[str, Path]] = []
        self._gen: int = -1

    def add(self, path: str, view: View) -> None:
        self._routes.append(_Route(path, view, is_ws=False))

    def add_ws(self, path: str, ws_view: WebSocketView) -> None:
        self._routes.append(_Route(path, ws_view, is_ws=True))

    def add_static(self, prefix: str, directory: str | Path) -> None:
        d = Path(directory) if isinstance(directory, str) else directory
        self._static_dirs.append((prefix.rstrip("/"), d))

    def discover(self) -> None:
        """从 Declaration 注册表发现 View/WebSocketView 子类。"""
        gen = mutobj.get_registry_generation()
        if gen == self._gen:
            return
        self._gen = gen

        # 清除旧的自动发现路由，保留手动添加的
        self._routes = [r for r in self._routes if not getattr(r, "_auto", False)]

        for view_cls in mutobj.discover_subclasses(View):
            view = view_cls()
            if view.path:
                route = _Route(view.path, view, is_ws=False)
                route._auto = True  # type: ignore[attr-defined]
                self._routes.append(route)

        for ws_cls in mutobj.discover_subclasses(WebSocketView):
            ws_view = ws_cls()
            if ws_view.path:
                route = _Route(ws_view.path, ws_view, is_ws=True)
                route._auto = True  # type: ignore[attr-defined]
                self._routes.append(route)

    def _match(self, path: str, *, ws: bool = False) -> tuple[Any, dict[str, str]] | None:
        for route in self._routes:
            if route.is_ws != ws:
                continue
            m = route.pattern.match(path)
            if m:
                params = {
                    name: unquote(val)
                    for name, val in zip(route.param_names, m.groups())
                }
                return route.handler, params
        return None

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        scope_type = scope.get("type")

        if scope_type == "lifespan":
            await self._handle_lifespan(scope, receive, send)
            return

        path: str = scope.get("path", "/")

        if scope_type == "websocket":
            result = self._match(path, ws=True)
            if result:
                ws_view, params = result
                ws_conn = WebSocketConnection(scope, receive, send, path_params=params)
                try:
                    await ws_view.connect(ws_conn)
                except Exception:
                    logger.exception("WebSocket error: %s", path)
            else:
                # 404 for WebSocket
                await send({"type": "websocket.close", "code": 4404, "reason": "Not found"})
            return

        if scope_type == "http":
            # HTTP 路由匹配
            result = self._match(path, ws=False)
            if result:
                view, params = result
                request = Request(scope, receive, path_params=params)
                method = scope.get("method", "GET").lower()
                handler = getattr(view, method, None)
                if handler is None:
                    resp = Response(status=405)
                else:
                    try:
                        resp = await handler(request)
                    except Exception:
                        logger.exception("HTTP handler error: %s %s", scope.get("method"), path)
                        resp = json_response({"error": "Internal Server Error"}, status=500)
                await resp.send(send)
                return

            # 静态文件 fallback
            if scope.get("method") == "GET":
                for prefix, directory in self._static_dirs:
                    rel = path[len(prefix):] if path.startswith(prefix) else None
                    if rel is None:
                        continue
                    if not rel or rel == "/":
                        rel = "/index.html"
                    file_path = directory / rel.lstrip("/")
                    # 安全检查
                    try:
                        resolved = file_path.resolve()
                        if not str(resolved).startswith(str(directory.resolve())):
                            continue
                    except (OSError, ValueError):
                        continue
                    if resolved.is_file():
                        await self._serve_file(resolved, send)
                        return
                    # SPA fallback: 非文件扩展名 → index.html
                    if "." not in resolved.name:
                        index = directory / "index.html"
                        if index.is_file():
                            await self._serve_file(index, send)
                            return

            # 404
            resp = Response(status=404, body=b"Not Found")
            await resp.send(send)
            return

    async def _serve_file(self, file_path: Path, send: Any) -> None:
        """发送静态文件。"""
        content_type, _ = mimetypes.guess_type(str(file_path))
        if content_type is None:
            content_type = "application/octet-stream"

        body = file_path.read_bytes()
        headers: list[tuple[bytes, bytes]] = [
            (b"content-type", content_type.encode()),
            (b"content-length", str(len(body)).encode()),
        ]

        # 缓存控制：HTML 不缓存，其他资源缓存 1 天
        if content_type.startswith("text/html"):
            headers.append((b"cache-control", b"no-cache"))
        else:
            headers.append((b"cache-control", b"public, max-age=86400"))

        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": headers,
        })
        await send({"type": "http.response.body", "body": body})

    # --- Lifespan 透传 ---

    _lifespan_handler: Any = None

    def set_lifespan(self, handler: Any) -> None:
        """设置 lifespan handler（async context manager factory）。"""
        self._lifespan_handler = handler

    async def _handle_lifespan(
        self, scope: dict[str, Any], receive: Any, send: Any,
    ) -> None:
        if self._lifespan_handler is None:
            # 无 lifespan handler → 直接 complete
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            msg = await receive()
            if msg["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
            return

        # 使用 async context manager 作为 lifespan
        started = False
        try:
            ctx = self._lifespan_handler(self)
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                await ctx.__aenter__()
                started = True
                await send({"type": "lifespan.startup.complete"})

            msg = await receive()
            if msg["type"] == "lifespan.shutdown":
                await ctx.__aexit__(None, None, None)
                await send({"type": "lifespan.shutdown.complete"})
        except Exception:
            logger.exception("Lifespan error")
            if not started:
                await send({
                    "type": "lifespan.startup.failed",
                    "message": "Lifespan startup failed",
                })
            else:
                await send({"type": "lifespan.shutdown.complete"})
