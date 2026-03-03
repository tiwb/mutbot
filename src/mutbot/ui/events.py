"""mutbot.ui.events -- UI 事件数据结构。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class UIEvent:
    """前端 → 后端的用户交互事件。

    Attributes:
        type: 事件类型（submit / cancel / action / change）。
        data: 事件数据。submit 时为全部组件当前值，action 时包含 action id。
        source: 事件来源组件 id（change 事件时使用）。
        context_id: 关联的 UIContext id。
    """

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    source: str | None = None
    context_id: str | None = None
