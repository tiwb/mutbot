"""mutbot.ui.toolkit -- UIToolkit（带 UI 能力的 Toolkit 基类）。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mutagent.tools import Toolkit

if TYPE_CHECKING:
    from mutbot.session import Session
    from mutbot.ui.context import UIContext

logger = logging.getLogger(__name__)


class UIToolkit(Toolkit):
    """带 UI 能力的 Toolkit 基类（mutbot 专用）。

    通过绑定链访问上下文，按需创建 UIContext。
    非 UI 工具不访问 self.ui → 不创建 UIContext → 零开销。

    绑定链：
        Toolkit.owner → ToolSet → Agent → Session
    """

    @property
    def ui(self) -> UIContext:
        """当前工具执行的 UIContext。首次访问时按需创建。

        UIContext 绑定到当前 tool_call 对应的 ToolCallCard。
        """
        from mutbot.ui.context import UIContext
        from mutbot.ui.context_impl import register_context

        owner = self.owner  # ToolSet
        if owner is None:
            raise RuntimeError("UIToolkit.owner not set (not bound to a ToolSet)")

        active_ui = getattr(owner, '_active_ui', None)
        if active_ui is not None:
            return active_ui

        tool_call = getattr(owner, '_current_tool_call', None)
        if tool_call is None:
            raise RuntimeError("UIToolkit.ui accessed outside of dispatch")

        # 构建 broadcast 闭包：通过绑定链获取 session 级广播函数
        broadcast = self._resolve_broadcast()

        ui = UIContext(
            context_id=tool_call.id,
            broadcast=broadcast,
        )
        register_context(ui)
        object.__setattr__(owner, '_active_ui', ui)
        return ui

    @property
    def session(self) -> Session:
        """当前 Session。"""
        return self.owner.agent.session

    async def show(self, view: dict) -> dict:
        """便捷方法：直接展示 UI 并等待提交。"""
        return await self.ui.show(view)

    def _resolve_broadcast(self):
        """从绑定链解析 broadcast 函数。

        查找路径：ToolSet._broadcast_fn + ToolSet._session_id
        broadcast_fn 由 SessionManager.start() 在创建 AgentBridge 后设置。
        """
        owner = self.owner
        broadcast_fn = getattr(owner, '_broadcast_fn', None)
        session_id = getattr(owner, '_session_id', None)

        if broadcast_fn is None or session_id is None:
            logger.warning(
                "UIToolkit._resolve_broadcast: broadcast_fn or session_id not set on ToolSet. "
                "UI events won't reach the frontend."
            )
            return None

        async def bound_broadcast(data: dict) -> None:
            await broadcast_fn(session_id, data)

        return bound_broadcast
