"""Agent bridge — connects mutagent Agent to WebSocket transport.

WebUserIO: a UserIO-like object that bridges Agent I/O to async queues.
AgentBridge: manages the Agent thread and event forwarder for one session.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from typing import Any, Callable, Awaitable

from mutagent.messages import Content, InputEvent, StreamEvent

from mutbot.web.serializers import serialize_content, serialize_stream_event

logger = logging.getLogger(__name__)

# Type alias for the async broadcast function
BroadcastFn = Callable[[str, dict[str, Any]], Awaitable[None]]


class WebUserIO:
    """Bridges Agent I/O to async WebSocket transport.

    The Agent runs synchronously in a worker thread.  This object provides:
    - ``input_stream()`` — blocking generator consumed by Agent.run()
    - ``present(content)`` — callback when Agent produces Content output.
    """

    def __init__(
        self,
        input_queue: queue.Queue,
        event_callback: Callable[[dict[str, Any]], None],
    ) -> None:
        self.input_queue = input_queue
        self.event_callback = event_callback
        self._stop_event = threading.Event()

    def request_stop(self) -> None:
        """Signal the input stream to stop."""
        self._stop_event.set()

    def input_stream(self):
        """Blocking generator that yields InputEvent objects."""
        while True:
            try:
                item = self.input_queue.get(timeout=0.5)
            except queue.Empty:
                if self._stop_event.is_set():
                    return
                continue
            if item is None:
                # Sentinel: stop iteration
                return
            yield item

    def present(self, content: Content) -> None:
        """Forward Content to the WebSocket via callback."""
        self.event_callback({
            "type": "present",
            "content": serialize_content(content),
        })


class AgentBridge:
    """Manages one Session's Agent thread and event forwarding.

    The bridge owns both the agent worker thread and the event forwarder task.
    This ensures one forwarder per session (not per WebSocket connection),
    preventing event loss from competing consumers.

    Lifecycle:
        bridge = AgentBridge(session_id, agent, web_userio, loop, broadcast_fn)
        bridge.start()           # launches agent thread + event forwarder
        bridge.send_message(text) # feed user input
        await bridge.stop()      # graceful shutdown
    """

    def __init__(
        self,
        session_id: str,
        agent,
        web_userio: WebUserIO,
        loop: asyncio.AbstractEventLoop,
        broadcast_fn: BroadcastFn,
        event_recorder: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.session_id = session_id
        self.agent = agent
        self.web_userio = web_userio
        self.loop = loop
        self.broadcast_fn = broadcast_fn
        self.event_recorder = event_recorder
        self._event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._agent_task: asyncio.Task | None = None
        self._forwarder_task: asyncio.Task | None = None

    def start(self) -> None:
        """Launch the agent thread and event forwarder task."""
        self._agent_task = self.loop.create_task(
            asyncio.to_thread(self._run_agent)
        )
        self._forwarder_task = self.loop.create_task(
            self._forward_events()
        )

    async def _forward_events(self) -> None:
        """Read events from the internal queue and broadcast to all WS clients."""
        event_count = 0
        try:
            while True:
                data = await self._event_queue.get()
                if data is None:
                    logger.info("Session %s: forwarder done after %d events", self.session_id, event_count)
                    await self.broadcast_fn(self.session_id, {"type": "agent_done"})
                    break
                # Record event to disk before broadcasting
                if self.event_recorder:
                    try:
                        self.event_recorder(data)
                    except Exception:
                        logger.exception("Event recording failed for session %s", self.session_id)
                event_count += 1
                etype = data.get("type", "?")
                logger.debug("Session %s: forward #%d type=%s", self.session_id, event_count, etype)
                await self.broadcast_fn(self.session_id, data)
        except asyncio.CancelledError:
            logger.info("Session %s: forwarder cancelled after %d events", self.session_id, event_count)

    def _run_agent(self) -> None:
        """Synchronous agent loop — runs in a worker thread."""
        try:
            for event in self.agent.run(self.web_userio.input_stream()):
                data = serialize_stream_event(event)
                self.loop.call_soon_threadsafe(self._event_queue.put_nowait, data)
        except Exception as exc:
            logger.exception("Agent error in session %s", self.session_id)
            error_data = {"type": "error", "error": str(exc)}
            self.loop.call_soon_threadsafe(self._event_queue.put_nowait, error_data)
        finally:
            # Signal that the agent has finished
            self.loop.call_soon_threadsafe(self._event_queue.put_nowait, None)

    def send_message(self, text: str, data: dict | None = None) -> None:
        """Feed a user message into the Agent."""
        event = InputEvent(type="user_message", text=text, data=data or {})
        self.web_userio.input_queue.put(event)
        # Broadcast user message to all connected clients.
        # Recording to disk happens in _forward_events() when it processes
        # this event from the queue — no need to record here.
        user_event = {"type": "user_message", "text": text, "data": data or {}}
        self.loop.call_soon_threadsafe(self._event_queue.put_nowait, user_event)

    async def stop(self) -> None:
        """Graceful shutdown: signal stop, cancel tasks, await completion."""
        # Signal the input stream to exit
        self.web_userio.request_stop()
        self.web_userio.input_queue.put(None)

        tasks: list[asyncio.Task] = []
        if self._forwarder_task and not self._forwarder_task.done():
            self._forwarder_task.cancel()
            tasks.append(self._forwarder_task)
        if self._agent_task and not self._agent_task.done():
            self._agent_task.cancel()
            tasks.append(self._agent_task)

        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=3)
            if pending:
                logger.warning(
                    "Session %s: %d task(s) did not finish within timeout",
                    self.session_id, len(pending),
                )
