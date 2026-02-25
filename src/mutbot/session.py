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


# ---------------------------------------------------------------------------
# Session Declaration 体系
# ---------------------------------------------------------------------------

class Session(mutobj.Declaration):
    """所有 Session 的基类"""

    id: str
    workspace_id: str
    title: str
    type: str = ""
    status: str = "active"
    created_at: str = ""
    updated_at: str = ""
    config: dict = mutobj.field(default_factory=dict)
    deleted: bool = False

    def __init__(self, **kwargs: Any) -> None:
        # type 未提供或为空时，自动使用全限定名
        if not kwargs.get("type"):
            kwargs["type"] = f"{type(self).__module__}.{type(self).__qualname__}"
        super().__init__(**kwargs)

    def serialize(self) -> dict:
        """序列化为可持久化的 dict"""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "title": self.title,
            "type": self.type,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "config": self.config,
            "deleted": self.deleted,
        }


class AgentSession(Session):
    """Agent 对话 Session"""

    model: str = ""
    system_prompt: str = ""

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

    pass


class DocumentSession(Session):
    """文档编辑 Session"""

    file_path: str = ""
    language: str = ""


# ---------------------------------------------------------------------------
# Session 类查找（基于 mutobj 子类发现）
# ---------------------------------------------------------------------------

# 旧短名称 → 全限定名映射（持久化向后兼容）
_LEGACY_TYPE_MAP: dict[str, str] = {
    "agent": "mutbot.session.AgentSession",
    "terminal": "mutbot.session.TerminalSession",
    "document": "mutbot.session.DocumentSession",
    "guide": "mutbot.builtins.guide.GuideSession",
    "researcher": "mutbot.builtins.researcher.ResearcherSession",
}

# 默认 Session 类型（新建 Session 时使用）
DEFAULT_SESSION_TYPE = "mutbot.builtins.guide.GuideSession"


def get_session_class(qualified_name: str) -> type[Session]:
    """通过全限定名查找 Session 子类，直接使用 mutobj 基础设施。

    支持旧短名称（如 "agent"）的向后兼容映射。
    """
    resolved = _LEGACY_TYPE_MAP.get(qualified_name, qualified_name)
    for cls in mutobj.discover_subclasses(Session):
        if f"{cls.__module__}.{cls.__qualname__}" == resolved:
            return cls
    raise ValueError(f"Unknown session type: {qualified_name!r}")
