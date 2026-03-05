"""mutbot.ui.context -- UIContext Declaration（后端驱动 UI 核心 API）。"""

from __future__ import annotations

from typing import Any

import mutagent

from mutbot.ui.events import UIEvent


class UIContext(mutagent.Declaration):
    """后端 handler 与前端 UI 渲染面的通信通道。

    接口声明在此，实现细节（WebSocket 推送、Future 管理等）在 @impl 中。
    不同渲染面（ToolCallCard、Session 面板等）对应不同的 @impl 实现。

    Attributes:
        context_id: UIContext 实例唯一标识。对于工具 UI，使用 tool_call_id。
        broadcast: 向前端广播消息的回调函数。
    """

    context_id: str
    broadcast: Any  # Callable[[str, dict], None]

    def set_view(self, view: dict) -> None:
        """推送完整视图到前端。前端通过 React reconciliation 平滑更新。

        Args:
            view: View Schema JSON，包含 components 和 actions。
        """
        ...

    async def wait_event(
        self,
        *,
        type: str | None = None,
        source: str | None = None,
    ) -> UIEvent:
        """等待用户事件。可按类型、来源过滤。

        外部取消（如 Agent 停止）时抛 CancelledError。

        Args:
            type: 只接受指定类型的事件（submit / cancel / action / change）。
            source: 只接受指定来源组件的事件。

        Returns:
            匹配的 UIEvent。
        """
        ...

    async def show(self, view: dict) -> dict | None:
        """便捷方法：set_view + wait_event(submit / cancel)。

        Args:
            view: View Schema JSON。

        Returns:
            提交的表单数据（所有组件当前值），取消时返回 None。
        """
        ...

    def close(self, final_view: dict | None = None) -> None:
        """关闭 UI。可指定最终视图（变为 Read-only 快照）。

        Args:
            final_view: 可选的最终视图，关闭后以 Read-only 模式展示。
        """
        ...


# 实现注册在 mutbot.ui.context_impl 中
