"""ClaudeCodeSession Declaration。

从 mutbot.session 移入，独立于核心 Session 体系。
不 import 此模块则 ClaudeCodeSession 不存在于 class registry。

当前状态：未适配，不可直接运行。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import mutobj

if TYPE_CHECKING:
    from mutbot.channel import Channel, ChannelContext
    from mutbot.runtime.session_manager import SessionManager

# 注意：此处 import Session 会触发 session.py 加载，
# 但 ClaudeCodeSession 本身只有在本模块被 import 时才注册。
from mutbot.session import Session


class ClaudeCodeSession(Session):
    """Claude Code CLI Session — spawn CLI 子进程通过 stream-json 管道通信。"""

    display_name = "Claude Code"
    display_icon = "terminal"

    cwd: str = ""
    model: str = ""
    permission_mode: str = ""
    claude_session_id: str = ""
