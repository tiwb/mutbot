"""WebSocket RPC 框架 — 消息分发与上下文管理。

Workspace 级 WebSocket 的 RPC 基础设施，支持：
- 请求/响应式 RPC 调用
- 服务端事件推送
- 按 method 名分发到对应 handler
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# RPC 错误码（参考 JSON-RPC 2.0）
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_REQUEST = -32600
ERR_INTERNAL = -32000

# Handler 类型：接收 (params, context) → result
RpcHandler = Callable[[dict, "RpcContext"], Awaitable[Any]]


@dataclass
class RpcContext:
    """RPC 调用上下文，传递给每个 handler"""

    workspace_id: str
    # 广播到当前 workspace 所有客户端
    broadcast: Callable[[dict], Awaitable[None]]
    # 管理器引用（由 routes.py 注入）
    managers: dict[str, Any] = field(default_factory=dict)


class RpcDispatcher:
    """按 method 名分发 RPC 请求到注册的 handler。

    用法::

        dispatcher = RpcDispatcher()

        @dispatcher.method("menu.query")
        async def handle_menu_query(params, ctx):
            return [...]

        # 处理一条 WebSocket 消息
        response = await dispatcher.dispatch(message, context)
        if response:
            await websocket.send_json(response)
    """

    def __init__(self) -> None:
        self._handlers: dict[str, RpcHandler] = {}

    def method(self, name: str) -> Callable[[RpcHandler], RpcHandler]:
        """装饰器：注册一个 RPC method handler"""
        def decorator(fn: RpcHandler) -> RpcHandler:
            self._handlers[name] = fn
            return fn
        return decorator

    def register(self, name: str, handler: RpcHandler) -> None:
        """编程式注册 handler"""
        self._handlers[name] = handler

    @property
    def methods(self) -> list[str]:
        """已注册的 method 名列表"""
        return list(self._handlers)

    async def dispatch(self, message: dict, context: RpcContext) -> dict | None:
        """分发一条 RPC 消息，返回响应 dict 或 None（非 RPC 消息时）。

        消息格式：
        - 请求: { "type": "rpc", "id": str, "method": str, "params": dict }
        - 响应: { "type": "rpc_result", "id": str, "result": Any }
        - 错误: { "type": "rpc_error", "id": str, "error": { "code": int, "message": str } }
        """
        msg_type = message.get("type")
        if msg_type != "rpc":
            return None

        req_id = message.get("id", "")
        method_name = message.get("method", "")
        params = message.get("params", {})

        if not method_name:
            return _error_response(req_id, ERR_INVALID_REQUEST, "Missing method name")

        handler = self._handlers.get(method_name)
        if handler is None:
            return _error_response(
                req_id, ERR_METHOD_NOT_FOUND,
                f"Method not found: {method_name}",
            )

        try:
            result = await handler(params, context)
            return {
                "type": "rpc_result",
                "id": req_id,
                "result": result,
            }
        except Exception as exc:
            logger.exception("RPC handler error: method=%s", method_name)
            return _error_response(req_id, ERR_INTERNAL, str(exc))


def _error_response(req_id: str, code: int, message: str) -> dict:
    return {
        "type": "rpc_error",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def make_event(event: str, data: dict | None = None) -> dict:
    """构造一条服务端推送事件消息"""
    return {"type": "event", "event": event, "data": data or {}}
