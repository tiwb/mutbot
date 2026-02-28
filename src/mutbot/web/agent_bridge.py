"""Agent bridge — connects mutagent Agent to WebSocket transport.

AgentBridge: manages one session's Agent as an asyncio task (no worker thread).
The Agent is now fully async, so the bridge runs in the same event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable, TYPE_CHECKING
from uuid import uuid4

from mutagent.messages import InputEvent, Message, StreamEvent, ToolCall, ToolResult

from mutbot.runtime.session_logging import current_session_id
from mutbot.web.serializers import serialize_stream_event

if TYPE_CHECKING:
    from mutbot.session import AgentSession

logger = logging.getLogger(__name__)

# Type alias for the async broadcast function
BroadcastFn = Callable[[str, dict[str, Any]], Awaitable[None]]


def _local_iso_now() -> str:
    """返回本地时区的 ISO 格式时间戳。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _gen_msg_id() -> str:
    """生成消息 ID（m_ 前缀 + 10 位 hex）。"""
    return "m_" + uuid4().hex[:10]


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
        session: AgentSession,
        persist_fn: Callable[[], None],
    ) -> None:
        self.session_id = session_id
        self.agent = agent
        self.loop = loop
        self.broadcast_fn = broadcast_fn
        self._session = session
        self._persist_fn = persist_fn
        self._input_queue: asyncio.Queue[InputEvent | None] = asyncio.Queue()
        self._agent_task: asyncio.Task | None = None
        # Session 级累计 token 计数器（从 session 元数据恢复）
        self._session_total_tokens: int = session.total_tokens

        # --- 飞行中状态追踪（用于强制停止时补提交部分消息） ---
        self._pending_text: list[str] = []
        self._pending_tool_calls: list[ToolCall] = []
        self._completed_results: list[ToolResult] = []
        self._response_committed: bool = False

        # --- Turn 时间追踪 ---
        self._agent_status: str = "idle"
        self._current_turn_id: str = ""
        self._turn_start_time: float = 0.0

        # --- Response 级状态追踪 ---
        self._response_first_delta: bool = True
        self._response_start_ts: str = ""
        self._response_start_mono: float = 0.0

        # --- Tool 时间追踪: tool_call_id → (iso_ts, mono_time) ---
        self._tool_start_times: dict[str, tuple[str, float]] = {}

    # --- chat_messages 便捷访问 ---

    @property
    def _chat_messages(self) -> list[dict[str, Any]]:
        return self._session.chat_messages

    def _append_chat_message(self, msg: dict[str, Any]) -> None:
        self._session.chat_messages.append(msg)

    def _find_chat_message(self, msg_id: str) -> dict[str, Any] | None:
        """从末尾反向查找 chat_message（最近的消息优先）。"""
        for i in range(len(self._chat_messages) - 1, -1, -1):
            if self._chat_messages[i].get("id") == msg_id:
                return self._chat_messages[i]
        return None

    def _find_last_chat_message(self, msg_type: str, role: str = "") -> dict[str, Any] | None:
        """从末尾反向查找最后一条指定类型的 chat_message。"""
        for i in range(len(self._chat_messages) - 1, -1, -1):
            m = self._chat_messages[i]
            if m.get("type") == msg_type and (not role or m.get("role") == role):
                return m
        return None

    # --- Broadcasting helpers ---

    async def _broadcast_status(self, status: str, **extra: Any) -> None:
        """推送 agent_status 事件。"""
        self._agent_status = status
        data: dict[str, Any] = {"type": "agent_status", "status": status}
        data.update(extra)
        await self.broadcast_fn(self.session_id, data)

    async def _broadcast_token_usage(self, usage: dict[str, int]) -> None:
        """从 response usage 计算并推送 token_usage 事件。"""
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        self._session_total_tokens += input_tokens + output_tokens
        # 下一轮的上下文 ≈ 本轮 input + 本轮 output（output 成为下轮历史）
        context_used = input_tokens + output_tokens

        context_window = getattr(self.agent.client, "context_window", None)
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
            "model": getattr(self.agent.client, "model", ""),
        }
        await self.broadcast_fn(self.session_id, data)

    # --- In-flight state tracking ---

    def _reset_turn_state(self) -> None:
        """Reset in-flight tracking for a new turn."""
        self._pending_text.clear()
        self._pending_tool_calls.clear()
        self._completed_results.clear()
        self._response_committed = False
        self._response_first_delta = True
        self._response_start_ts = ""
        self._response_start_mono = 0.0
        self._tool_start_times.clear()

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
            # 重置 response 级状态，为下一个 response 准备
            self._response_first_delta = True
            self._response_start_ts = ""
            self._response_start_mono = 0.0
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

    # --- chat_messages 事件处理 ---

    def _get_model(self) -> str:
        return getattr(self.agent.client, "model", "") or ""

    def _handle_text_delta(self, event: StreamEvent) -> dict[str, Any]:
        """处理 text_delta 事件，更新 chat_messages，返回增强后的事件数据。"""
        data = serialize_stream_event(event)

        if self._response_first_delta:
            # 首个 text_delta: 创建新的 assistant text chat_message
            self._response_first_delta = False
            self._response_start_ts = _local_iso_now()
            self._response_start_mono = time.monotonic()
            msg_id = _gen_msg_id()
            model = self._get_model()

            self._append_chat_message({
                "id": msg_id,
                "type": "text",
                "role": "assistant",
                "content": event.text,
                "timestamp": self._response_start_ts,
                "model": model,
            })
            # 注入 timestamp、model、id 到首个 text_delta
            data["timestamp"] = self._response_start_ts
            data["model"] = model
            data["id"] = msg_id
        else:
            # 后续 text_delta: 更新最后一条 assistant text 的 content
            cm = self._find_last_chat_message("text", "assistant")
            if cm:
                cm["content"] += event.text

        return data

    def _handle_response_done(self, event: StreamEvent) -> dict[str, Any]:
        """处理 response_done 事件，更新 duration_ms。"""
        data = serialize_stream_event(event)

        if self._response_start_mono:
            duration_ms = round((time.monotonic() - self._response_start_mono) * 1000)
            # 更新 chat_message
            cm = self._find_last_chat_message("text", "assistant")
            if cm:
                cm["duration_ms"] = duration_ms
            # 注入到事件
            data["duration_ms"] = duration_ms

        return data

    def _handle_tool_exec_start(self, event: StreamEvent) -> dict[str, Any]:
        """处理 tool_exec_start 事件，追加 tool_group chat_message。"""
        data = serialize_stream_event(event)

        if event.tool_call:
            tc = event.tool_call
            ts = _local_iso_now()
            mono = time.monotonic()
            msg_id = _gen_msg_id()
            model = self._get_model()

            self._tool_start_times[tc.id] = (ts, mono)

            self._append_chat_message({
                "id": msg_id,
                "type": "tool_group",
                "tool_call_id": tc.id,
                "tool_name": tc.name,
                "arguments": tc.arguments,
                "timestamp": ts,
                "model": model,
            })
            # 注入到事件
            data["timestamp"] = ts
            data["model"] = model
            data["id"] = msg_id

        return data

    def _handle_tool_exec_end(self, event: StreamEvent) -> dict[str, Any]:
        """处理 tool_exec_end 事件，更新 tool_group 结果和耗时。"""
        data = serialize_stream_event(event)

        if event.tool_result:
            tr = event.tool_result
            ts = _local_iso_now()
            duration_ms: int | None = None

            start_info = self._tool_start_times.pop(tr.tool_call_id, None)
            if start_info:
                _, start_mono = start_info
                duration_ms = round((time.monotonic() - start_mono) * 1000)

            # 更新对应的 tool_group chat_message
            for i in range(len(self._chat_messages) - 1, -1, -1):
                cm = self._chat_messages[i]
                if cm.get("type") == "tool_group" and cm.get("tool_call_id") == tr.tool_call_id:
                    cm["result"] = tr.content
                    cm["is_error"] = tr.is_error
                    if duration_ms is not None:
                        cm["duration_ms"] = duration_ms
                    break

            data["timestamp"] = ts
            if duration_ms is not None:
                data["duration_ms"] = duration_ms

        return data

    def _handle_turn_done(self) -> dict[str, Any]:
        """处理 turn_done 事件，追加 turn_done chat_message。"""
        duration = round(time.monotonic() - self._turn_start_time) if self._turn_start_time else 0
        ts = _local_iso_now()
        msg_id = _gen_msg_id()

        self._append_chat_message({
            "id": msg_id,
            "type": "turn_done",
            "turn_id": self._current_turn_id,
            "timestamp": ts,
            "duration_seconds": duration,
        })

        return {
            "type": "turn_done",
            "id": msg_id,
            "timestamp": ts,
            "turn_id": self._current_turn_id,
            "duration_seconds": duration,
        }

    def _handle_error(self, error_msg: str) -> dict[str, Any]:
        """处理 error，追加 error chat_message。"""
        msg_id = _gen_msg_id()
        ts = _local_iso_now()
        model = self._get_model()

        self._append_chat_message({
            "id": msg_id,
            "type": "error",
            "content": error_msg,
            "timestamp": ts,
            "model": model,
        })

        return {
            "type": "error",
            "error": error_msg,
            "id": msg_id,
            "timestamp": ts,
            "model": model,
        }

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

                    # --- 更新 chat_messages + 增强事件 ---
                    if event.type == "text_delta":
                        data = self._handle_text_delta(event)
                        await self.broadcast_fn(self.session_id, data)

                    elif event.type == "response_done":
                        data = self._handle_response_done(event)
                        await self.broadcast_fn(self.session_id, data)

                    elif event.type == "tool_exec_start":
                        data = self._handle_tool_exec_start(event)
                        await self.broadcast_fn(self.session_id, data)
                        tool_name = event.tool_call.name if event.tool_call else ""
                        await self._broadcast_status("tool_calling", tool_name=tool_name)

                    elif event.type == "tool_exec_end":
                        data = self._handle_tool_exec_end(event)
                        await self.broadcast_fn(self.session_id, data)
                        await self._broadcast_status("thinking")

                    elif event.type == "turn_done":
                        data = self._handle_turn_done()
                        await self.broadcast_fn(self.session_id, data)
                        await self._broadcast_status("idle")

                    else:
                        # 其他事件（tool_use_start, tool_use_delta, tool_use_end 等）直接广播
                        data = serialize_stream_event(event)
                        await self.broadcast_fn(self.session_id, data)

                    # 注入 token_usage 事件
                    if event.type == "response_done" and event.response:
                        await self._broadcast_token_usage(event.response.usage)

                    # 在 response_done/turn_done 时持久化 session
                    if event.type in ("response_done", "turn_done"):
                        self._persist_fn()

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
                error_data = self._handle_error(str(exc))
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
        hidden = (data or {}).get("hidden", False)

        if not hidden:
            model = self._get_model()

            # Turn 归组：idle 时新建 turn，busy 时复用
            if self._agent_status == "idle":
                self._current_turn_id = uuid4().hex[:12]
                self._turn_start_time = time.monotonic()

                # 追加 turn_start chat_message
                turn_start_id = _gen_msg_id()
                ts_start = _local_iso_now()
                self._append_chat_message({
                    "id": turn_start_id,
                    "type": "turn_start",
                    "turn_id": self._current_turn_id,
                    "timestamp": ts_start,
                })
                # 广播 turn_start 事件
                asyncio.ensure_future(self.broadcast_fn(self.session_id, {
                    "type": "turn_start",
                    "id": turn_start_id,
                    "turn_id": self._current_turn_id,
                    "timestamp": ts_start,
                }))

            ts = _local_iso_now()
            user_msg_id = _gen_msg_id()

            # 追加 user text chat_message
            self._append_chat_message({
                "id": user_msg_id,
                "type": "text",
                "role": "user",
                "content": text,
                "timestamp": ts,
                "sender": "User",
            })

            # Broadcast user message to all connected clients
            user_event: dict[str, Any] = {
                "type": "user_message", "text": text, "data": data or {},
                "timestamp": ts, "turn_id": self._current_turn_id,
                "model": model, "sender": "User", "id": user_msg_id,
            }
            asyncio.ensure_future(self.broadcast_fn(self.session_id, user_event))
            # 用户发消息后推送 thinking 状态（hidden 消息不推送）
            asyncio.ensure_future(self._broadcast_status("thinking"))
        # 入队放在 ensure_future 之后，确保广播先于 agent 处理（FIFO 调度）
        self._input_queue.put_nowait(event)

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
