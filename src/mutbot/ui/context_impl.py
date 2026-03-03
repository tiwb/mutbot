"""mutbot.ui.context_impl -- UIContext 的 WebSocket 实现。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import mutagent

from mutbot.ui.context import UIContext
from mutbot.ui.events import UIEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 全局活跃 UIContext 注册表（用于前端事件路由）
# ---------------------------------------------------------------------------

_active_contexts: dict[str, UIContext] = {}


def register_context(ctx: UIContext) -> None:
    """注册一个活跃的 UIContext。"""
    _active_contexts[ctx.context_id] = ctx


def unregister_context(context_id: str) -> None:
    """注销 UIContext。"""
    _active_contexts.pop(context_id, None)


def deliver_event(context_id: str, event: UIEvent) -> bool:
    """将前端事件分发到对应的 UIContext。

    Returns:
        True 如果找到对应的 UIContext 并成功投递，False 否则。
    """
    ctx = _active_contexts.get(context_id)
    if ctx is None:
        logger.warning("deliver_event: no active UIContext for context_id=%s", context_id)
        return False
    _deliver_to_context(ctx, event)
    return True


def _deliver_to_context(ctx: UIContext, event: UIEvent) -> None:
    """将事件投递到 UIContext 的事件队列。"""
    queue: asyncio.Queue[UIEvent] | None = getattr(ctx, '_event_queue', None)
    if queue is not None:
        queue.put_nowait(event)


# ---------------------------------------------------------------------------
# UIContext @impl
# ---------------------------------------------------------------------------

def _get_event_queue(ctx: UIContext) -> asyncio.Queue[UIEvent]:
    """获取或初始化事件队列。"""
    queue = getattr(ctx, '_event_queue', None)
    if queue is None:
        queue = asyncio.Queue()
        object.__setattr__(ctx, '_event_queue', queue)
    return queue


def _get_closed(ctx: UIContext) -> bool:
    return getattr(ctx, '_closed', False)


@mutagent.impl(UIContext.set_view)
def set_view(self: UIContext, view: dict) -> None:
    if _get_closed(self):
        logger.warning("set_view called on closed UIContext %s", self.context_id)
        return
    msg: dict[str, Any] = {
        "type": "ui_view",
        "context_id": self.context_id,
        "view": view,
    }
    _fire_and_forget_broadcast(self, msg)


@mutagent.impl(UIContext.wait_event)
async def wait_event(
    self: UIContext,
    *,
    type: str | None = None,
    source: str | None = None,
) -> UIEvent:
    if _get_closed(self):
        raise RuntimeError("wait_event called on closed UIContext")

    queue = _get_event_queue(self)

    while True:
        event = await queue.get()
        # 按 type / source 过滤
        if type is not None and event.type != type:
            continue
        if source is not None and event.source != source:
            continue
        return event


@mutagent.impl(UIContext.show)
async def show(self: UIContext, view: dict) -> dict:
    self.set_view(view)
    event = await self.wait_event(type="submit")
    return event.data


@mutagent.impl(UIContext.close)
def close(self: UIContext, final_view: dict | None = None) -> None:
    if _get_closed(self):
        return
    object.__setattr__(self, '_closed', True)

    # 发送 ui_close 消息
    msg: dict[str, Any] = {
        "type": "ui_close",
        "context_id": self.context_id,
    }
    if final_view is not None:
        msg["final_view"] = final_view
    _fire_and_forget_broadcast(self, msg)

    # 注销
    unregister_context(self.context_id)

    logger.debug("UIContext %s closed", self.context_id)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _fire_and_forget_broadcast(ctx: UIContext, data: dict) -> None:
    """调用 broadcast 函数。broadcast 可以是 sync 或 async。"""
    broadcast = ctx.broadcast
    if broadcast is None:
        return
    result = broadcast(data)
    # 如果 broadcast 返回 coroutine，调度到事件循环
    if asyncio.iscoroutine(result):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(result)
        except RuntimeError:
            pass


# 注册实现
mutagent.register_module_impls(__import__(__name__))
