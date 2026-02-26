"""Agent bridge — connects mutagent Agent to WebSocket transport.

AgentBridge: manages one session's Agent as an asyncio task (no worker thread).
The Agent is now fully async, so the bridge runs in the same event loop.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

from mutagent.messages import InputEvent, Message, StreamEvent, ToolCall, ToolResult

from mutbot.runtime.session_logging import current_session_id
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
        await bridge.cancel()    # cancel current thinking (session stays)
        await bridge.stop()      # graceful shutdown
    """

    def __init__(
        self,
        session_id: str,
        agent,
        loop: asyncio.AbstractEventLoop,
        broadcast_fn: BroadcastFn,
        event_recorder: Callable[[dict[str, Any]], None] | None = None,
        initial_session_total_tokens: int = 0,
    ) -> None:
        self.session_id = session_id
        self.agent = agent
        self.loop = loop
        self.broadcast_fn = broadcast_fn
        self.event_recorder = event_recorder
        self._input_queue: asyncio.Queue[InputEvent | None] = asyncio.Queue()
        self._agent_task: asyncio.Task | None = None
        # Session 级累计 token 计数器（可从历史事件恢复）
        self._session_total_tokens: int = initial_session_total_tokens

        # --- 飞行中状态追踪（用于强制停止时补提交部分消息） ---
        self._pending_text: list[str] = []
        self._pending_tool_calls: list[ToolCall] = []
        self._completed_results: list[ToolResult] = []
        self._response_committed: bool = False

    # --- Broadcasting helpers ---

    async def _broadcast_status(self, status: str, **extra: Any) -> None:
        """推送 agent_status 事件。"""
        data: dict[str, Any] = {"type": "agent_status", "status": status}
        data.update(extra)
        await self.broadcast_fn(self.session_id, data)

    async def _broadcast_token_usage(self, usage: dict[str, int]) -> None:
        """从 response usage 计算并推送 token_usage 事件。"""
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        self._session_total_tokens += input_tokens + output_tokens

        context_window = getattr(self.agent.client, "context_window", None)
        context_percent: float | None = None
        if context_window and input_tokens:
            context_percent = round(input_tokens / context_window * 100, 1)

        data: dict[str, Any] = {
            "type": "token_usage",
            "context_used": input_tokens,
            "context_window": context_window,
            "context_percent": context_percent,
            "session_total_tokens": self._session_total_tokens,
            "model": getattr(self.agent.client, "model", ""),
        }
        if self.event_recorder:
            try:
                self.event_recorder(data)
            except Exception:
                logger.exception("Event recording failed for session %s", self.session_id)
        await self.broadcast_fn(self.session_id, data)

    # --- In-flight state tracking ---

    def _reset_turn_state(self) -> None:
        """Reset in-flight tracking for a new turn."""
        self._pending_text.clear()
        self._pending_tool_calls.clear()
        self._completed_results.clear()
        self._response_committed = False

    def _track_event(self, event: StreamEvent) -> None:
        """Track in-flight state from stream events."""
        if event.type == "text_delta" and event.text:
            self._pending_text.append(event.text)
        elif event.type == "tool_use_start" and event.tool_call:
            self._pending_tool_calls.append(event.tool_call)
        elif event.type == "tool_exec_end" and event.tool_result:
            self._completed_results.append(event.tool_result)
        elif event.type == "response_done":
            # response_done 之后 agent 已将 assistant msg 提交到 messages
            self._response_committed = True
            self._pending_text.clear()
            self._pending_tool_calls.clear()
        elif event.type == "turn_done":
            self._reset_turn_state()

    def _commit_partial_state(self) -> None:
        """Commit in-flight state to agent.messages after forced cancellation.

        Ensures the conversation history reflects what happened before
        interruption, so the LLM can see the context on next call.
        """
        if not self._response_committed:
            # 阶段 1: LLM 流式输出中 — 提交部分 assistant 消息
            partial_text = "".join(self._pending_text)
            if partial_text or self._pending_tool_calls:
                content = partial_text + "\n\n[interrupted]" if partial_text else "[interrupted]"
                self.agent.messages.append(Message(
                    role="assistant",
                    content=content,
                    tool_calls=list(self._pending_tool_calls),
                ))
                logger.info(
                    "Session %s: committed partial assistant message (%d chars, %d tool_calls)",
                    self.session_id, len(partial_text), len(self._pending_tool_calls),
                )
                # 如果有 tool_calls 但没有 results，需要补齐 interrupted 的 results
                if self._pending_tool_calls:
                    results = []
                    completed_ids = {r.tool_call_id for r in self._completed_results}
                    for r in self._completed_results:
                        results.append(r)
                    for tc in self._pending_tool_calls:
                        if tc.id not in completed_ids:
                            results.append(ToolResult(
                                tool_call_id=tc.id,
                                content="[Tool execution interrupted by user]",
                                is_error=True,
                            ))
                    self.agent.messages.append(Message(role="user", tool_results=results))
        else:
            # 阶段 2: 工具执行中 — assistant 已提交，补提交 tool_results
            # 从 agent.messages 最后的 assistant 消息中获取 tool_calls
            expected_calls: list[ToolCall] = []
            if self.agent.messages and self.agent.messages[-1].role == "assistant":
                expected_calls = self.agent.messages[-1].tool_calls

            if expected_calls:
                completed_ids = {r.tool_call_id for r in self._completed_results}
                results: list[ToolResult] = list(self._completed_results)
                for tc in expected_calls:
                    if tc.id not in completed_ids:
                        results.append(ToolResult(
                            tool_call_id=tc.id,
                            content="[Tool execution interrupted by user]",
                            is_error=True,
                        ))
                if results:
                    self.agent.messages.append(Message(role="user", tool_results=results))
                    logger.info(
                        "Session %s: committed %d tool results (%d interrupted)",
                        self.session_id, len(results),
                        len(results) - len(self._completed_results),
                    )

        self._reset_turn_state()

    # --- Input stream ---

    async def _input_stream(self):
        """Async generator: read InputEvent objects from asyncio.Queue."""
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
                    # 追踪飞行中状态
                    self._track_event(event)

                    data = serialize_stream_event(event)
                    if self.event_recorder:
                        try:
                            self.event_recorder(data)
                        except Exception:
                            logger.exception("Event recording failed for session %s", self.session_id)
                    await self.broadcast_fn(self.session_id, data)

                    # 注入 agent_status 事件
                    if event.type == "tool_exec_start":
                        tool_name = event.tool_call.name if event.tool_call else ""
                        await self._broadcast_status("tool_calling", tool_name=tool_name)
                    elif event.type == "tool_exec_end":
                        await self._broadcast_status("thinking")
                    elif event.type == "turn_done":
                        await self._broadcast_status("idle")

                    # 注入 token_usage 事件
                    if event.type == "response_done" and event.response:
                        await self._broadcast_token_usage(event.response.usage)

                await self._broadcast_status("idle")
                await self.broadcast_fn(self.session_id, {"type": "agent_done"})
            except asyncio.CancelledError:
                logger.info("Session %s: agent task cancelled", self.session_id)
                self._commit_partial_state()
                try:
                    await self._broadcast_status("idle")
                    await self.broadcast_fn(self.session_id, {"type": "agent_cancelled"})
                except Exception:
                    pass
            except Exception as exc:
                logger.exception("Agent error in session %s", self.session_id)
                self._reset_turn_state()
                error_data = {"type": "error", "error": str(exc)}
                try:
                    await self.broadcast_fn(self.session_id, error_data)
                    await self._broadcast_status("idle")
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
        # 用户发消息后立即推送 thinking 状态
        asyncio.ensure_future(self._broadcast_status("thinking"))

    async def cancel(self) -> None:
        """Cancel current agent thinking without stopping the session.

        After cancel, the bridge remains usable. The next send_message()
        will be processed by the existing agent task (which continues
        reading from _input_stream after the cancelled turn).
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
