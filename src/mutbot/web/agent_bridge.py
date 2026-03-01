"""Agent bridge — connects mutagent Agent to WebSocket transport.

AgentBridge: manages one session's Agent as an asyncio task.
Pure event forwarding layer — no message construction, no metadata calculation.
Agent.run() manages context.messages internally.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Awaitable, TYPE_CHECKING
from uuid import uuid4

from mutagent.messages import Message, TextBlock, TurnStartBlock

from mutbot.runtime.session_logging import current_session_id
from mutbot.web.serializers import serialize_stream_event

if TYPE_CHECKING:
    from mutbot.session import AgentSession

logger = logging.getLogger(__name__)

# Type alias for the async broadcast function
BroadcastFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class AgentBridge:
    """Manages one Session's Agent as an asyncio task.

    Pure event forwarding: receives user input → constructs Message →
    feeds to Agent → serializes StreamEvents → broadcasts to WebSocket.

    Lifecycle:
        bridge = AgentBridge(session_id, agent, loop, broadcast_fn)
        bridge.start()           # launches agent task
        bridge.send_message(text) # feed user input
        await bridge.cancel()    # cancel current thinking (session stays)
        await bridge.stop()      # graceful shutdown
    """

    def __init__(
        self,
        session_id: str,
        agent,
        loop: asyncio.AbstractEventLoop,
        broadcast_fn: BroadcastFn,
        session: AgentSession,
        persist_fn: Callable[[], None],
        session_status_fn: Callable[[str], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.agent = agent
        self.loop = loop
        self.broadcast_fn = broadcast_fn
        self._session = session
        self._persist_fn = persist_fn
        self._session_status_fn = session_status_fn
        self._input_queue: asyncio.Queue[Message | None] = asyncio.Queue()
        self._agent_task: asyncio.Task | None = None
        # Session 级累计 token 计数器（从 session 元数据恢复）
        self._session_total_tokens: int = session.total_tokens

        # --- Turn 状态追踪 ---
        self._agent_status: str = "idle"
        self._current_turn_id: str = ""
        self._turn_start_time: float = 0.0

    # --- Broadcasting helpers ---

    async def _broadcast_status(self, status: str, **extra: Any) -> None:
        """推送 agent_status 事件，并同步更新 session.status。"""
        self._agent_status = status
        data: dict[str, Any] = {"type": "agent_status", "status": status}
        data.update(extra)
        await self.broadcast_fn(self.session_id, data)
        # 同步 session.status：working → "running"，idle → ""
        if self._session_status_fn is not None:
            if status == "idle":
                self._session_status_fn("")
            elif status in ("thinking", "tool_calling"):
                self._session_status_fn("running")

    async def _broadcast_token_usage(self, usage: dict[str, int]) -> None:
        """从 response usage 计算并推送 token_usage 事件。"""
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        self._session_total_tokens += input_tokens + output_tokens
        context_used = input_tokens + output_tokens

        context_window = getattr(self.agent.llm, "context_window", None)
        context_percent: float | None = None
        if context_window and context_used:
            context_percent = round(context_used / context_window * 100, 1)

        # 同步到 session 元数据（随 _persist 自然落盘）
        self._session.total_tokens = self._session_total_tokens
        self._session.context_used = context_used
        self._session.context_window = context_window or 0

        data: dict[str, Any] = {
            "type": "token_usage",
            "context_used": context_used,
            "context_window": context_window,
            "context_percent": context_percent,
            "session_total_tokens": self._session_total_tokens,
            "model": getattr(self.agent.llm, "model", ""),
        }
        await self.broadcast_fn(self.session_id, data)

    # --- Input stream ---

    async def _input_stream(self):
        """Async generator: read Message objects from asyncio.Queue."""
        while True:
            item = await self._input_queue.get()
            if item is None:
                return
            yield item

    # --- Agent task lifecycle ---

    def start(self) -> None:
        """Launch the agent task in the event loop."""
        self._start_agent_task()

    def _start_agent_task(self) -> None:
        """Create and start a new agent task."""
        async def _run():
            current_session_id.set(self.session_id)
            try:
                async for event in self.agent.run(
                    self._input_stream(),
                    check_pending=lambda: not self._input_queue.empty(),
                ):
                    # 纯 serialize + forward
                    data = serialize_stream_event(event)

                    if event.type == "tool_exec_start":
                        await self.broadcast_fn(self.session_id, data)
                        tool_name = event.tool_call.name if event.tool_call else ""
                        await self._broadcast_status("tool_calling", tool_name=tool_name)
                    elif event.type == "tool_exec_end":
                        await self.broadcast_fn(self.session_id, data)
                        await self._broadcast_status("thinking")
                    elif event.type == "turn_done":
                        # Enrich with bridge-level turn info
                        if self._turn_start_time:
                            data["duration_seconds"] = round(time.monotonic() - self._turn_start_time)
                        await self.broadcast_fn(self.session_id, data)
                        await self._broadcast_status("idle")
                    else:
                        await self.broadcast_fn(self.session_id, data)

                    # Token usage on response_done
                    if event.type == "response_done" and event.response:
                        await self._broadcast_token_usage(event.response.usage)

                    # 持久化 context.messages
                    if event.type in ("response_done", "turn_done"):
                        self._persist_fn()

                await self._broadcast_status("idle")
                await self.broadcast_fn(self.session_id, {"type": "agent_done"})
            except asyncio.CancelledError:
                logger.info("Session %s: agent task cancelled", self.session_id)
                # agent.run() finally 块已清理 context.messages，直接持久化
                self._persist_fn()
                try:
                    await self._broadcast_status("idle")
                    await self.broadcast_fn(self.session_id, {"type": "agent_cancelled"})
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("Agent error in session %s", self.session_id)
                try:
                    await self.broadcast_fn(self.session_id, {"type": "error", "error": str(exc)})
                    await self._broadcast_status("idle")
                    await self.broadcast_fn(self.session_id, {"type": "agent_done"})
                except Exception:
                    pass

        self._agent_task = self.loop.create_task(_run())

    def send_message(self, text: str, data: dict | None = None, *, skip_user_broadcast: bool = False) -> None:
        """Feed a user message into the Agent.

        Constructs a full Message (with id/timestamp/sender + TurnStartBlock)
        and enqueues it for agent.run().

        Args:
            skip_user_broadcast: 为 True 时跳过 user_message 广播（调用方已预先广播）。
        """
        hidden = (data or {}).get("hidden", False)
        msg_id = "m_" + uuid4().hex[:10]
        ts = time.time()
        blocks: list = []

        if not hidden:
            # Turn 归组：idle 时新建 turn，busy 时复用
            if self._agent_status == "idle":
                self._current_turn_id = uuid4().hex[:12]
                self._turn_start_time = time.monotonic()

                # 广播 turn_start 事件
                asyncio.ensure_future(self.broadcast_fn(self.session_id, {
                    "type": "turn_start",
                    "turn_id": self._current_turn_id,
                }))

            blocks.append(TurnStartBlock(turn_id=self._current_turn_id))
            blocks.append(TextBlock(text=text))

            # Broadcast user message to all connected clients
            if not skip_user_broadcast:
                user_event: dict[str, Any] = {
                    "type": "user_message", "text": text, "data": data or {},
                    "id": msg_id, "timestamp": ts,
                    "turn_id": self._current_turn_id,
                    "sender": "User",
                }
                asyncio.ensure_future(self.broadcast_fn(self.session_id, user_event))
            asyncio.ensure_future(self._broadcast_status("thinking"))
        else:
            # Hidden message: 仍需 TurnStartBlock 触发 agent 处理
            if not self._current_turn_id:
                self._current_turn_id = uuid4().hex[:12]
            blocks.append(TurnStartBlock(turn_id=self._current_turn_id))
            blocks.append(TextBlock(text=text))

        msg = Message(
            role="user",
            blocks=blocks,
            id=msg_id,
            timestamp=ts,
            sender="User",
        )
        self._input_queue.put_nowait(msg)

    async def cancel(self) -> None:
        """Cancel current agent thinking without stopping the session.

        After cancel, the bridge remains usable. The next send_message()
        will be processed by the existing agent task.
        """
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
            try:
                await self._agent_task
            except asyncio.CancelledError:
                pass
            # Restart agent task so it can accept new messages
            self._start_agent_task()

    async def stop(self) -> None:
        """Graceful shutdown: signal stop, cancel task, await completion."""
        self._input_queue.put_nowait(None)  # stop input_stream
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
            try:
                await self._agent_task
            except asyncio.CancelledError:
                pass
