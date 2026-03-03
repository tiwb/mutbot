"""mutbot.ui -- 后端驱动 UI 框架。"""

from mutbot.ui.context import UIContext
from mutbot.ui.events import UIEvent
from mutbot.ui.toolkit import UIToolkit
from mutbot.ui.context_impl import deliver_event

__all__ = ["UIContext", "UIEvent", "UIToolkit", "deliver_event"]

# 确保 @impl 注册
import mutbot.ui.context_impl  # noqa: F401
