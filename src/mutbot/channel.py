"""Channel 核心抽象 — Session 与前端的通信管道。

Channel 是 mutbot 核心层概念，定义 Session 的通信基础设施。
Session 通过 Channel 发送消息给前端，通过 on_message / on_data 接收前端消息。
Channel 的传输实现（WebSocket 多路复用）对 Session 透明。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import mutobj

if TYPE_CHECKING:
    from mutbot.runtime.session_manager import SessionManager
    from mutbot.runtime.terminal import TerminalManager


class Channel(mutobj.Declaration):
    """Session 与前端之间的通信管道。

    Session 通过 Channel 发送消息给前端，通过 on_message / on_data
    接收前端消息。Channel 的传输实现（WebSocket 多路复用）对 Session 透明。
    """

    ch: int                        # 频道 ID（前端路由用）
    session_id: str = ""           # 关联的 Session ID（消息路由用）

    def send_json(self, data: dict) -> None:
        """发送 JSON 消息到前端（仅此 channel）。"""
        ...

    def send_binary(self, data: bytes) -> None:
        """发送二进制数据到前端（仅此 channel）。"""
        ...


@dataclass
class ChannelContext:
    """Channel 操作的运行时上下文。

    与 RpcContext 职责不同：RpcContext 是 RPC 调度上下文（含 broadcast/sender_ws），
    ChannelContext 是 Session 通信上下文（传递给 on_connect/on_message 等回调）。
    """

    workspace_id: str
    session_manager: SessionManager
    terminal_manager: TerminalManager
    event_loop: asyncio.AbstractEventLoop
