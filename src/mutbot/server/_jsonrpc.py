"""JSON-RPC 2.0 分发器（通用，MCP 和 WebSocket RPC 都可复用）。"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger("mutbot.server.jsonrpc")

# JSON-RPC 2.0 标准错误码
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

Handler = Callable[..., Awaitable[Any]]


@dataclass
class JsonRpcError(Exception):
    """JSON-RPC 错误。"""
    code: int
    message: str
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            d["data"] = self.data
        return d


@dataclass
class JsonRpcDispatcher:
    """JSON-RPC 2.0 方法分发器。

    用法::

        dispatch = JsonRpcDispatcher()

        @dispatch.method("tools/list")
        async def list_tools(params):
            return {"tools": [...]}

        # 处理单条 JSON-RPC 消息
        response = await dispatch.handle(message_dict)
    """

    _handlers: dict[str, Handler] = field(default_factory=dict)
    _notification_handlers: dict[str, Handler] = field(default_factory=dict)

    def method(self, name: str) -> Callable[[Handler], Handler]:
        """注册 JSON-RPC 方法处理器（装饰器）。"""
        def decorator(fn: Handler) -> Handler:
            self._handlers[name] = fn
            return fn
        return decorator

    def notification(self, name: str) -> Callable[[Handler], Handler]:
        """注册 notification 处理器（无 id，不需要响应）。"""
        def decorator(fn: Handler) -> Handler:
            self._notification_handlers[name] = fn
            return fn
        return decorator

    def add_method(self, name: str, handler: Handler) -> None:
        """编程式注册方法处理器。"""
        self._handlers[name] = handler

    def add_notification(self, name: str, handler: Handler) -> None:
        """编程式注册 notification 处理器。"""
        self._notification_handlers[name] = handler

    async def handle(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """处理单条 JSON-RPC 消息，返回响应（notification 返回 None）。"""
        # 验证基本格式
        if message.get("jsonrpc") != "2.0":
            return _error_response(None, INVALID_REQUEST, "Missing or invalid jsonrpc version")

        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params", {})

        # JSON-RPC response（来自对端的响应，不是请求）
        if method is None:
            if "result" in message or "error" in message:
                # 这是一个 response，交给 notification handler
                handler = self._notification_handlers.get("__response__")
                if handler:
                    try:
                        await handler(message)
                    except Exception:
                        logger.exception("Response handler error")
                return None
            return _error_response(msg_id, INVALID_REQUEST, "Missing method")

        if not isinstance(method, str):
            return _error_response(msg_id, INVALID_REQUEST, "Method must be a string")

        # Notification（无 id）
        if msg_id is None:
            handler = self._notification_handlers.get(method)
            if handler:
                try:
                    await handler(params)
                except Exception:
                    logger.exception("Notification handler error for %s", method)
            return None

        # Request（有 id，需要响应）
        handler = self._handlers.get(method)
        if handler is None:
            return _error_response(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")

        try:
            result = await handler(params)
            return _success_response(msg_id, result)
        except JsonRpcError as e:
            return _error_response(msg_id, e.code, e.message, e.data)
        except Exception as e:
            logger.exception("Handler error for %s", method)
            return _error_response(msg_id, INTERNAL_ERROR, str(e))

    async def handle_bytes(self, data: bytes) -> bytes | None:
        """处理原始 JSON bytes，返回响应 bytes（notification 返回 None）。

        支持单条消息和 batch（数组）。
        """
        try:
            parsed = json.loads(data)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return json.dumps(
                _error_response(None, PARSE_ERROR, f"Parse error: {e}")
            ).encode()

        if isinstance(parsed, list):
            # Batch 请求
            return await self._handle_batch(parsed)
        elif isinstance(parsed, dict):
            response = await self.handle(parsed)
            if response is None:
                return None
            return json.dumps(response).encode()
        else:
            return json.dumps(
                _error_response(None, INVALID_REQUEST, "Request must be object or array")
            ).encode()

    async def _handle_batch(self, messages: list[Any]) -> bytes | None:
        """处理 JSON-RPC batch 请求。"""
        if not messages:
            return json.dumps(
                _error_response(None, INVALID_REQUEST, "Empty batch")
            ).encode()

        responses: list[dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                responses.append(
                    _error_response(None, INVALID_REQUEST, "Batch item must be object")
                )
                continue
            response = await self.handle(msg)
            if response is not None:
                responses.append(response)

        if not responses:
            return None  # 全是 notification
        return json.dumps(responses).encode()


def _success_response(msg_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _error_response(msg_id: Any, code: int, message: str,
                    data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": error}


def make_request(msg_id: Any, method: str, params: Any = None) -> dict[str, Any]:
    """构造 JSON-RPC request。"""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        msg["params"] = params
    return msg


def make_notification(method: str, params: Any = None) -> dict[str, Any]:
    """构造 JSON-RPC notification。"""
    msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        msg["params"] = params
    return msg
