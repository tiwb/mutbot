"""MutBot — 基于 mutagent 的 Web 应用。"""

__version__ = "0.2.999"

from mutbot.session import Session, AgentSession, TerminalSession, DocumentSession
from mutbot.menu import Menu, MenuItem, MenuResult

# 确保内置 Declaration 子类被注册
import mutbot.builtins  # noqa: F401
