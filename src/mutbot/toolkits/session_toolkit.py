"""mutbot.toolkits.session_toolkit -- SessionToolkit 声明与实现。

向导 Agent 用于创建专业 Agent Session 的工具。
这是 mutbot 特有的能力，不放在 mutagent 层。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mutagent.tools import Toolkit

if TYPE_CHECKING:
    from mutbot.runtime.session_impl import SessionManager

logger = logging.getLogger(__name__)


class SessionToolkit(Toolkit):
    """Session 管理工具集，供向导 Agent 创建专业 Agent Session。

    Attributes:
        session_manager: SessionManager 实例。
        workspace_id: 当前 workspace ID。
    """

    session_manager: SessionManager
    workspace_id: str

    def create(self, session_type: str, initial_message: str) -> str:
        """创建一个新的专业 Agent Session。

        Args:
            session_type: Session 类型的全限定名。
            initial_message: 向新 Session 的 Agent 传达的初始需求描述。

        Returns:
            新 Session 的 ID 和类型信息。
        """
        from mutbot.session import Session, AgentSession

        # 验证 session_type
        try:
            session_cls = Session.get_session_class(session_type)
        except ValueError:
            return f"未知的 Session 类型：{session_type}"

        if not issubclass(session_cls, AgentSession):
            return f"{session_type} 不是 Agent Session 类型，无法创建。"

        # 创建 Session
        session = self.session_manager.create(
            workspace_id=self.workspace_id,
            session_type=session_type,
        )

        logger.info(
            "Guide created %s session %s with initial message: %s",
            session_type, session.id, initial_message[:100],
        )

        # 将初始消息存储到 session config 中，Agent 启动时会读取
        session.config["initial_message"] = initial_message

        # 返回结果供向导展示给用户（包含 mutbot://session/ 链接）
        label = session_cls.__name__
        if label.endswith("Session"):
            label = label[:-7]

        return (
            f"已创建 {label} Session（ID: {session.id}，标题: {session.title}）。\n"
            f"用户可以点击链接打开：[{session.title}](mutbot://session/{session.id})\n"
            f"初始需求已传达：{initial_message[:200]}"
        )
