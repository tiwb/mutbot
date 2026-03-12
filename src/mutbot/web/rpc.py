"""WebSocket RPC 框架 — 消息分发与上下文管理。

Workspace 级 WebSocket 的 RPC 基础设施，支持：
- 请求/响应式 RPC 调用
- 服务端事件推送
- 按 method 名分发到对应 handler
- Declaration 子类自动发现
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

import mutobj

if TYPE_CHECKING:
    from mutbot.channel import ChannelContext
    from mutbot.runtime.session_manager import SessionManager
    from mutbot.runtime.terminal import TerminalManager
    from mutbot.runtime.workspace import WorkspaceManager
    from mutbot.web.transport import ChannelManager

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
    # 发送者 WebSocket（Any 类型，兼容新旧接口）
    sender_ws: Any = None
    # dispatch 发送 rpc_result 后执行的回调（handler 可设置）
    _post_send: Callable[[], Any] | None = None

    # 类型安全的 manager 访问（部分字段 Optional，app 级不注入全部 manager）
    session_manager: SessionManager | None = None
    workspace_manager: WorkspaceManager | None = None
    terminal_manager: TerminalManager | None = None
    channel_manager: ChannelManager | None = None
    config: Any = None
    event_loop: asyncio.AbstractEventLoop | None = None

    def make_channel_context(self) -> ChannelContext:
        """从 RpcContext 构造 ChannelContext。"""
        from mutbot.channel import ChannelContext as _CC
        return _CC(
            workspace_id=self.workspace_id,
            session_manager=self.session_manager,
            terminal_manager=self.terminal_manager,
            event_loop=self.event_loop or asyncio.get_running_loop(),
        )

    async def broadcast_event(self, event: str, data: dict | None = None) -> None:
        """广播事件到当前 workspace 所有客户端（含发送者）"""
        await self.broadcast(make_event(event, data))


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

    @classmethod
    def from_declaration(cls, *base_classes: type) -> RpcDispatcher:
        """自动发现指定基类的所有子类，注册其公开方法。

        方法名映射规则：{namespace}.{method_name}
        """
        dispatcher = cls()
        for base in base_classes:
            for rpc_cls in mutobj.discover_subclasses(base):
                instance = rpc_cls()
                ns = instance.namespace
                for name in _get_rpc_methods(rpc_cls, base):
                    method_name = f"{ns}.{name}" if ns else name
                    dispatcher.register(method_name, getattr(instance, name))
        return dispatcher

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


# ---------------------------------------------------------------------------
# RPC Declaration 基类
# ---------------------------------------------------------------------------

class AppRpc(mutobj.Declaration):
    """App 级 RPC 基类（/ws/app 端点）。"""
    namespace: str = ""


class WorkspaceRpc(mutobj.Declaration):
    """Workspace 级 RPC 基类（/ws/workspace/{id} 端点，非 session 相关）。"""
    namespace: str = ""


class SessionRpc(mutobj.Declaration):
    """Session 级 RPC 基类（workspace 内，session 相关操作）。"""
    namespace: str = ""


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------

def _get_rpc_methods(cls: type, base: type) -> list[str]:
    """获取 RPC 类的公开方法（排除基类方法和私有方法）。"""
    base_methods = set(dir(base))
    result: list[str] = []
    for name in dir(cls):
        if name.startswith("_"):
            continue
        if name in base_methods:
            continue
        attr = getattr(cls, name, None)
        if attr is not None and (inspect.isfunction(attr) or inspect.ismethod(attr)):
            result.append(name)
    return result


def _error_response(req_id: str, code: int, message: str) -> dict:
    return {
        "type": "rpc_error",
        "id": req_id,
        "error": {"code": code, "message": message},
    }


def make_event(event: str, data: dict | None = None) -> dict:
    """构造一条服务端推送事件消息"""
    return {"type": "event", "event": event, "data": data or {}}
