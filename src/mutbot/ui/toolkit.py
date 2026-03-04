"""mutbot.ui.toolkit -- UIToolkitBase + UIToolkit。"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from mutagent.tools import Toolkit

if TYPE_CHECKING:
    from mutagent.messages import ToolSchema

    from mutbot.session import Session
    from mutbot.ui.context import UIContext

logger = logging.getLogger(__name__)


class UIToolkitBase(Toolkit):
    """带 UI 能力的 Toolkit 基础设施（mutbot 专用）。

    提供 UIContext lazy 创建、Session 访问等基础设施。
    不直接暴露为 LLM 工具（_discoverable=False）。

    子类：
        - UIToolkit：通用 UI 工具，暴露 show() 给 LLM
        - ConfigToolkit 等：领域工具，内部调用 self.ui.show()

    绑定链：
        Toolkit.owner → ToolSet → Agent → Session
    """

    _discoverable = False

    @property
    def ui(self) -> UIContext:
        """当前工具执行的 UIContext。首次访问时按需创建。

        UIContext 绑定到当前 tool_call 对应的 ToolCallCard。
        """
        from mutbot.ui.context import UIContext
        from mutbot.ui.context_impl import register_context

        owner = self.owner  # ToolSet
        if owner is None:
            raise RuntimeError("UIToolkitBase.owner not set (not bound to a ToolSet)")

        active_ui = getattr(owner, '_active_ui', None)
        if active_ui is not None:
            return active_ui

        tool_call = getattr(owner, '_current_tool_call', None)
        if tool_call is None:
            raise RuntimeError("UIToolkitBase.ui accessed outside of dispatch")

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
                "UIToolkitBase._resolve_broadcast: broadcast_fn or session_id not set on ToolSet. "
                "UI events won't reach the frontend."
            )
            return None

        async def bound_broadcast(data: dict) -> None:
            await broadcast_fn(session_id, data)

        return bound_broadcast


# ---------------------------------------------------------------------------
# 工具描述常量
# ---------------------------------------------------------------------------

_UI_SHOW_DESCRIPTION = """\
展示交互式 UI 并等待用户提交。返回 {组件id: 值} 字典。

view 结构：
- title: 标题（可选）
- components: 组件列表，每个组件有 type、id 和类型属性
- actions: 按钮列表，如 {"type": "submit", "label": "确认", "primary": true}

常用组件：
- text: 文本输入。属性：label, placeholder, secret, multiline
- select: 选择器。属性：label, options: [{value, label}]
- hint: 只读提示文字。属性：text（支持 Markdown）
- toggle: 布尔开关。属性：label

示例：
{"title": "选择模型", "components": [{"type": "select", "id": "model", "label": "模型", "options": [{"value": "gpt-4", "label": "GPT-4"}, {"value": "claude", "label": "Claude"}]}, {"type": "toggle", "id": "stream", "label": "流式输出"}], "actions": [{"type": "submit", "label": "确认", "primary": true}]}\
"""

_UI_SHOW_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "view": {
            "type": "object",
            "description": "View Schema，描述 UI 的完整状态",
            "properties": {
                "title": {"type": "string"},
                "components": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["type", "id"],
                    },
                },
                "actions": {
                    "type": "array",
                    "items": {"type": "object"},
                },
            },
            "required": ["components"],
        },
    },
    "required": ["view"],
}


class UIToolkit(UIToolkitBase):
    """通用 UI 工具，暴露 show() 给 LLM。

    LLM 通过 UI-show 工具自主构建 View Schema，展示交互式 UI。
    """

    _discoverable = True

    async def show(self, view: dict) -> dict:
        """展示交互式 UI 并等待用户提交。"""
        return await self.ui.show(view)

    def _customize_schema(self, method_name: str, schema: ToolSchema) -> ToolSchema:
        if method_name == "show":
            from dataclasses import replace
            schema = replace(
                schema,
                description=_UI_SHOW_DESCRIPTION,
                input_schema=_UI_SHOW_INPUT_SCHEMA,
            )
        return schema
