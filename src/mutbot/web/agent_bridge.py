"""Agent bridge — connects mutagent Agent to WebSocket transport.

AgentBridge: manages one session's Agent as an asyncio task (no worker thread).
The Agent is now fully async, so the bridge runs in the same event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

from mutagent.messages import InputEvent, StreamEvent

from mutbot.web.serializers import serialize_stream_event

logger = logging.getLogger(__name__)

# Type alias for the async broadcast function
BroadcastFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class AgentBridge:
    """Manages one Session's Agent as an asyncio task.

    With the async Agent, no worker thread is needed. The bridge runs
    the Agent as an asyncio task in the same event loop, using
    asyncio.Queue for input and direct await for broadcasting.

    Lifecycle:
        bridge = AgentBridge(session_id, agent, loop, broadcast_fn)
        bridge.start()           # launches agent task
        bridge.send_message(text) # feed user input
        await bridge.stop()      # graceful shutdown
    """

    def __init__(
        self,
        session_id: str,
        agent,
        loop: asyncio.AbstractEventLoop,
        broadcast_fn: BroadcastFn,
        event_recorder: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.agent = agent
        self.loop = loop
        self.broadcast_fn = broadcast_fn
        self.event_recorder = event_recorder
        self._input_queue: asyncio.Queue[InputEvent | None] = asyncio.Queue()
        self._agent_task: asyncio.Task | None = None

    async def _input_stream(self):
        """Async generator: read InputEvent objects from asyncio.Queue."""
        while True:
            item = await self._input_queue.get()
            if item is None:
                return
            yield item

    def start(self) -> None:
        """Launch the agent task in the event loop."""
        async def _run():
            try:
                async for event in self.agent.run(self._input_stream()):
                    data = serialize_stream_event(event)
                    if self.event_recorder:
                        try:
                            self.event_recorder(data)
                        except Exception:
                            logger.exception("Event recording failed for session %s", self.session_id)
                    await self.broadcast_fn(self.session_id, data)
                await self.broadcast_fn(self.session_id, {"type": "agent_done"})
            except asyncio.CancelledError:
                logger.info("Session %s: agent task cancelled", self.session_id)
            except Exception as exc:
                logger.exception("Agent error in session %s", self.session_id)
                error_data = {"type": "error", "error": str(exc)}
                try:
                    await self.broadcast_fn(self.session_id, error_data)
                    await self.broadcast_fn(self.session_id, {"type": "agent_done"})
                except Exception:
                    pass

        self._agent_task = self.loop.create_task(_run())

    def send_message(self, text: str, data: dict | None = None) -> None:
        """Feed a user message into the Agent."""
        event = InputEvent(type="user_message", text=text, data=data or {})
        self._input_queue.put_nowait(event)
        # Broadcast user message to all connected clients
        user_event = {"type": "user_message", "text": text, "data": data or {}}
        if self.event_recorder:
            try:
                self.event_recorder(user_event)
            except Exception:
                pass
        asyncio.ensure_future(self.broadcast_fn(self.session_id, user_event))

    async def stop(self) -> None:
        """Graceful shutdown: signal stop, cancel task, await completion."""
        self._input_queue.put_nowait(None)  # stop input_stream
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
            try:
                await self._agent_task
            except asyncio.CancelledError:
                pass
