"""Session Declaration 基类 — 公开 API。

定义 Session 及其子类（AgentSession、TerminalSession、DocumentSession）。
Session.type 自动由类的 __module__ + __qualname__ 生成，无需手动声明。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

import mutobj

if TYPE_CHECKING:
    from mutagent.agent import Agent
    from mutagent.config import Config
    from mutagent.messages import Message
    from mutbot.runtime.session_impl import SessionManager


# ---------------------------------------------------------------------------
# Session Declaration 体系
# ---------------------------------------------------------------------------

class Session(mutobj.Declaration):
    """所有 Session 的基类"""

    # UI 元数据（无类型注解，不参与 mutobj 字段处理）
    display_name = ""    # 空串时从类名推导
    display_icon = ""    # Lucide 图标名，空串时用 kind 回退默认

    id: str
    workspace_id: str
    title: str
    type: str = ""
    status: str = ""
    created_at: str = ""
    updated_at: str = ""
    config: dict = mutobj.field(default_factory=dict)

    def __init__(self, **kwargs: Any) -> None:
        # type 未提供或为空时，自动使用全限定名
        if not kwargs.get("type"):
            kwargs["type"] = f"{type(self).__module__}.{type(self).__qualname__}"
        super().__init__(**kwargs)

    @staticmethod
    def get_session_class(qualified_name: str) -> type[Session]:
        """通过全限定名查找 Session 子类，直接使用 mutobj 基础设施。"""
        from mutbot.runtime.session_impl import get_session_class
        return get_session_class(qualified_name)

    def serialize(self) -> dict:
        from mutbot.runtime.session_impl import serialize_session
        return serialize_session(self)

    @classmethod
    def deserialize(cls, data: dict) -> Session:
        """从 dict 重建 Session 实例。默认实现基于 __annotations__ 自动提取字段。"""
        ...

    def on_create(self, sm: SessionManager) -> None:
        """创建后的初始化（设状态、创建关联资源等）。

        sm 提供 terminal_manager、config 等运行时资源。
        各子类按需从 sm 取用，基类默认空操作。
        """
        ...

    def on_stop(self, sm: SessionManager) -> None:
        """停止时的关联资源清理和状态归位。

        runtime 资源（bridge、log handler）由 SessionManager 清理，
        此方法只负责 Session 自身的状态和关联资源（如 PTY）。
        """
        ...

    def on_restart_cleanup(self) -> None:
        """服务器重启时清理残留状态（无需外部资源）。"""
        ...


class AgentSession(Session):
    """Agent 对话 Session"""

    display_name = "Agent"
    display_icon = "message-square"

    model: str = ""
    system_prompt: str = ""
    total_tokens: int = 0
    context_used: int = 0
    context_window: int = 0

    def create_agent(
        self,
        config: Config,
        log_dir: Path | None = None,
        session_ts: str = "",
        messages: list[Message] | None = None,
        **kwargs: Any,
    ) -> Agent:
        """组装并返回 Agent 实例。

        子类覆盖此方法以定制工具集和提示词。
        默认实现保持当前行为（ModuleToolkit + LogToolkit + auto_discover）。
        """
        from mutbot.runtime.session_impl import build_default_agent
        return build_default_agent(self, config, log_dir, session_ts, messages)


class TerminalSession(Session):
    """终端 Session"""

    display_name = "Terminal"
    display_icon = "terminal"


class DocumentSession(Session):
    """文档编辑 Session"""

    display_name = "Document"
    display_icon = "file-text"

    file_path: str = ""
    language: str = ""
