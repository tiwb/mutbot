"""Session Declaration 基类 — 公开 API。

定义 Session 及其子类（AgentSession、TerminalSession、DocumentSession）。
Session.type 自动由类的 __module__ + __qualname__ 生成，无需手动声明。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING

import mutobj
from mutobj import impl

if TYPE_CHECKING:
    from mutagent.agent import Agent
    from mutagent.config import Config
    from mutagent.messages import Message
    from mutbot.channel import Channel, ChannelContext
    from mutbot.runtime.session_manager import SessionManager


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
        from mutbot.runtime.session_manager import get_session_class
        return get_session_class(qualified_name)

    def serialize(self) -> dict:
        from mutbot.runtime.session_manager import serialize_session
        return serialize_session(self)

    @classmethod
    def deserialize(cls, data: dict) -> Session:
        """从 dict 重建 Session 实例。默认实现基于 __annotations__ 自动提取字段。"""
        ...

    async def on_create(self, sm: SessionManager) -> None:
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

    # --- 广播能力（Declaration 桩）---

    def broadcast_json(self, data: dict) -> None:
        """广播 JSON 到连接此 Session 的所有 channel。"""
        ...

    def broadcast_binary(self, data: bytes) -> None:
        """广播 binary 到连接此 Session 的所有 channel。"""
        ...

    # --- Channel 生命周期回调（Declaration 桩）---

    async def on_connect(self, channel: Channel, ctx: ChannelContext) -> None:
        """前端连接到此 Session 时调用。"""
        ...

    def on_disconnect(self, channel: Channel, ctx: ChannelContext) -> None:
        """前端断开此 Session 的 channel 时调用。"""
        ...

    async def on_message(self, channel: Channel, raw: dict, ctx: ChannelContext) -> None:
        """收到前端 JSON 消息。"""
        ...

    async def on_data(self, channel: Channel, payload: bytes, ctx: ChannelContext) -> None:
        """收到前端二进制数据。"""
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
        from mutbot.runtime.session_manager import build_default_agent
        return build_default_agent(self, config, log_dir, session_ts, messages)


class TerminalSession(Session):
    """终端 Session"""

    display_name = "Terminal"
    display_icon = "square-terminal"

    scrollback_b64: str = ""  # persisted scrollback, base64-encoded


class DocumentSession(Session):
    """文档编辑 Session"""

    display_name = "Document"
    display_icon = "file-text"

    file_path: str = ""
    language: str = ""


class ClaudeCodeSession(Session):
    """Claude Code CLI Session — spawn CLI 子进程通过 stream-json 管道通信。"""

    display_name = "Claude Code"
    display_icon = "terminal"

    cwd: str = ""
    model: str = ""
    permission_mode: str = ""
    claude_session_id: str = ""


# ---------------------------------------------------------------------------
# SessionChannels Extension — Session 的 channel 管理
# ---------------------------------------------------------------------------

class SessionChannels(mutobj.Extension[Session]):
    """Session 的 channel 管理——框架自动维护，@impl 不需要关心。"""

    _channels: list = mutobj.field(default_factory=list)  # list[Channel]


@impl(Session.broadcast_json)
def _session_broadcast_json(self: Session, data: dict) -> None:
    ext = SessionChannels.get_or_create(self)
    for ch in ext._channels:
        ch.send_json(data)


@impl(Session.broadcast_binary)
def _session_broadcast_binary(self: Session, data: bytes) -> None:
    ext = SessionChannels.get_or_create(self)
    for ch in ext._channels:
        ch.send_binary(data)
