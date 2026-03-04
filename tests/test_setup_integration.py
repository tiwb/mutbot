"""Setup 向导集成测试

涵盖：
- ConnectionManager pending events 队列与 flush
- AgentBridge.send_message hidden 不广播
- AgentBridge.inject_tool_call 注入逻辑
- AgentBridge.request_tool 运行时注入
- AgentBridge._input_stream pending 检查
- AgentBridge._execute_pending_tools 事件广播与 context 追加
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# ConnectionManager pending events
# ---------------------------------------------------------------------------

class TestConnectionManagerPendingEvents:
    """测试 ConnectionManager 的 pending events 队列机制。"""

    def _make_manager(self):
        from mutbot.web.connection import ConnectionManager
        return ConnectionManager()

    def test_queue_event_stores(self):
        """无连接时 queue_event 存入 pending。"""
        cm = self._make_manager()
        cm.queue_event("ws1", "open_session", {"session_id": "s1"})
        assert "ws1" in cm._pending_events
        assert len(cm._pending_events["ws1"]) == 1
        msg = cm._pending_events["ws1"][0]
        assert msg["type"] == "event"
        assert msg["event"] == "open_session"
        assert msg["data"]["session_id"] == "s1"

    def test_queue_multiple_events(self):
        """多个事件入队。"""
        cm = self._make_manager()
        cm.queue_event("ws1", "event_a", {"x": 1})
        cm.queue_event("ws1", "event_b", {"y": 2})
        assert len(cm._pending_events["ws1"]) == 2

    @pytest.mark.asyncio
    async def test_flush_on_connect(self):
        """连接时 flush pending events 给新客户端。"""
        cm = self._make_manager()
        cm.queue_event("ws1", "open_session", {"session_id": "s1"})

        # 模拟 WebSocket
        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()

        await cm.connect("ws1", ws)

        ws.accept.assert_called_once()
        ws.send_json.assert_called_once()
        sent = ws.send_json.call_args[0][0]
        assert sent["event"] == "open_session"
        # flush 后 pending 清空
        assert "ws1" not in cm._pending_events

    @pytest.mark.asyncio
    async def test_no_pending_no_flush(self):
        """无 pending 时连接不 flush。"""
        cm = self._make_manager()
        ws = AsyncMock()
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()

        await cm.connect("ws1", ws)

        ws.accept.assert_called_once()
        ws.send_json.assert_not_called()


# ---------------------------------------------------------------------------
# AgentBridge hidden message
# ---------------------------------------------------------------------------

class TestAgentBridgeHidden:
    """测试 AgentBridge.send_message 的 hidden 标记行为。"""

    def _make_bridge(self, loop):
        from mutbot.web.agent_bridge import AgentBridge
        from mutbot.session import AgentSession

        agent = MagicMock()
        broadcast_calls = []
        session = AgentSession(id="test-session", workspace_id="ws1", title="Test")

        async def mock_broadcast(session_id, data):
            broadcast_calls.append(data)

        bridge = AgentBridge(
            session_id="test-session",
            agent=agent,
            loop=loop,
            broadcast_fn=mock_broadcast,
            session=session,
            persist_fn=lambda: None,
        )
        return bridge, broadcast_calls

    @pytest.mark.asyncio
    async def test_normal_message_broadcasts(self):
        """普通消息广播 user_message 和 thinking 状态。"""
        loop = asyncio.get_running_loop()
        bridge, calls = self._make_bridge(loop)
        bridge.send_message("hello", data={})

        # 入队了消息
        assert not bridge._input_queue.empty()
        # 让 ensure_future 的协程执行
        await asyncio.sleep(0.01)
        # 应广播 user_message 和 thinking
        types = [c.get("type") or c.get("status") for c in calls]
        assert any(c.get("type") == "user_message" for c in calls)
        assert any(c.get("status") == "thinking" for c in calls)

    @pytest.mark.asyncio
    async def test_hidden_message_no_broadcast(self):
        """hidden 消息不广播 user_message，不推送 thinking 状态。"""
        loop = asyncio.get_running_loop()
        bridge, calls = self._make_bridge(loop)
        bridge.send_message("__setup__", data={"hidden": True})

        # 入队了消息
        assert not bridge._input_queue.empty()
        # 让 ensure_future 协程有机会执行（如果有的话）
        await asyncio.sleep(0.01)
        # 不应有任何广播
        assert len(calls) == 0

    @pytest.mark.asyncio
    async def test_hidden_message_enters_queue(self):
        """hidden 消息正常进入 input queue。"""
        loop = asyncio.get_running_loop()
        bridge, _ = self._make_bridge(loop)
        bridge.send_message("__setup__", data={"hidden": True})

        item = bridge._input_queue.get_nowait()
        assert item is not None
        assert item.role == "user"
        # Message blocks 包含 TurnStartBlock + TextBlock
        from mutagent.messages import TextBlock
        text_blocks = [b for b in item.blocks if isinstance(b, TextBlock)]
        assert len(text_blocks) == 1
        assert text_blocks[0].text == "__setup__"


# ---------------------------------------------------------------------------
# AgentBridge.request_tool
# ---------------------------------------------------------------------------

class TestAgentBridgeRequestTool:
    """测试 AgentBridge.request_tool 运行时注入逻辑。"""

    def _make_bridge(self, loop):
        from mutbot.web.agent_bridge import AgentBridge
        from mutbot.session import AgentSession

        agent = MagicMock()
        # mock tools.query 返回 True（已注册）
        agent.tools.query.return_value = True
        broadcast_calls = []
        session = AgentSession(id="test-session", workspace_id="ws1", title="Test")

        async def mock_broadcast(session_id, data):
            broadcast_calls.append(data)

        bridge = AgentBridge(
            session_id="test-session",
            agent=agent,
            loop=loop,
            broadcast_fn=mock_broadcast,
            session=session,
            persist_fn=lambda: None,
        )
        return bridge, broadcast_calls

    @pytest.mark.asyncio
    async def test_request_adds_to_pending(self):
        """request_tool 将工具调用添加到 pending 列表。"""
        loop = asyncio.get_running_loop()
        bridge, _ = self._make_bridge(loop)
        bridge.request_tool("Config-llm")

        assert len(bridge._pending_tool_calls) == 1
        assert bridge._pending_tool_calls[0] == ("Config-llm", {})

    @pytest.mark.asyncio
    async def test_request_pushes_trigger_message(self):
        """request_tool 推送触发消息到 input queue。"""
        loop = asyncio.get_running_loop()
        bridge, _ = self._make_bridge(loop)
        bridge.request_tool("Config-llm")

        assert not bridge._input_queue.empty()
        msg = bridge._input_queue.get_nowait()
        assert msg.role == "user"

    @pytest.mark.asyncio
    async def test_request_with_input(self):
        """request_tool 支持传入工具参数。"""
        loop = asyncio.get_running_loop()
        bridge, _ = self._make_bridge(loop)
        bridge.request_tool("Config-llm", {"key": "value"})

        assert bridge._pending_tool_calls[0] == ("Config-llm", {"key": "value"})


# ---------------------------------------------------------------------------
# AgentBridge.cancel 清空 pending
# ---------------------------------------------------------------------------

class TestAgentBridgeCancelClearsPending:
    """测试 cancel() 会清空 _pending_tool_calls。"""

    def _make_bridge(self, loop):
        from mutbot.web.agent_bridge import AgentBridge
        from mutbot.session import AgentSession

        agent = MagicMock()
        agent.tools.query.return_value = True

        async def mock_broadcast(session_id, data):
            pass

        session = AgentSession(id="test-session", workspace_id="ws1", title="Test")
        bridge = AgentBridge(
            session_id="test-session",
            agent=agent,
            loop=loop,
            broadcast_fn=mock_broadcast,
            session=session,
            persist_fn=lambda: None,
        )
        return bridge

    @pytest.mark.asyncio
    async def test_cancel_clears_pending(self):
        """cancel 后 _pending_tool_calls 应为空。"""
        loop = asyncio.get_running_loop()
        bridge = self._make_bridge(loop)

        # 手动添加 pending 并创建一个 mock agent task
        bridge._pending_tool_calls.append(("Config-llm", {}))
        bridge._pending_tool_calls.append(("Config-llm", {"key": "val"}))
        assert len(bridge._pending_tool_calls) == 2

        # 模拟 agent task：创建一个不会完成的 task
        async def forever():
            await asyncio.sleep(999)
        bridge._agent_task = loop.create_task(forever())

        await bridge.cancel()

        assert bridge._pending_tool_calls == []
        # cancel 后 agent task 应被重启（非 None）
        assert bridge._agent_task is not None

        # 清理
        bridge._agent_task.cancel()
        try:
            await bridge._agent_task
        except asyncio.CancelledError:
            pass


# ---------------------------------------------------------------------------
# AgentBridge._input_stream pending 检查
# ---------------------------------------------------------------------------

class TestInputStreamPendingCheck:
    """测试 _input_stream 在 yield 前执行 pending tools。"""

    def _make_bridge(self, loop):
        from mutbot.web.agent_bridge import AgentBridge
        from mutbot.session import AgentSession

        agent = MagicMock()
        agent.tools.query.return_value = True
        # mock dispatch 为 async
        agent.tools.dispatch = AsyncMock()
        agent.context.messages = []
        broadcast_calls = []
        persist_calls = []
        session = AgentSession(id="test-session", workspace_id="ws1", title="Test")

        async def mock_broadcast(session_id, data):
            broadcast_calls.append(data)

        bridge = AgentBridge(
            session_id="test-session",
            agent=agent,
            loop=loop,
            broadcast_fn=mock_broadcast,
            session=session,
            persist_fn=lambda: persist_calls.append(1),
        )
        return bridge, broadcast_calls, persist_calls

    @pytest.mark.asyncio
    async def test_pending_executed_before_yield(self):
        """_input_stream 有 pending 时先执行再 yield 消息。"""
        from mutagent.messages import Message, TextBlock, TurnStartBlock
        loop = asyncio.get_running_loop()
        bridge, broadcasts, persists = self._make_bridge(loop)

        # 注入 pending tool
        bridge._pending_tool_calls.append(("Test-tool", {}))

        # 推送消息
        msg = Message(role="user", blocks=[TurnStartBlock(turn_id="t1"), TextBlock(text="hi")])
        bridge._input_queue.put_nowait(msg)

        # 从 _input_stream 取一条
        stream = bridge._input_stream()
        yielded = await stream.__anext__()

        # pending 已清空
        assert len(bridge._pending_tool_calls) == 0
        # dispatch 被调用
        bridge.agent.tools.dispatch.assert_called_once()
        # 广播了 turn_start、tool_exec_start、tool_exec_end、turn_done
        event_types = [b.get("type") for b in broadcasts]
        assert "turn_start" in event_types
        # 持久化被调用
        assert len(persists) >= 1
        # yield 的是原始消息
        assert yielded is msg

    @pytest.mark.asyncio
    async def test_no_pending_yields_directly(self):
        """无 pending 时直接 yield 消息。"""
        from mutagent.messages import Message, TextBlock, TurnStartBlock
        loop = asyncio.get_running_loop()
        bridge, broadcasts, _ = self._make_bridge(loop)

        msg = Message(role="user", blocks=[TurnStartBlock(turn_id="t1"), TextBlock(text="hi")])
        bridge._input_queue.put_nowait(msg)

        stream = bridge._input_stream()
        yielded = await stream.__anext__()

        # 无广播（没有 pending tools）
        assert len(broadcasts) == 0
        assert yielded is msg


# ---------------------------------------------------------------------------
# AgentBridge._execute_pending_tools 事件广播与 context
# ---------------------------------------------------------------------------

class TestExecutePendingTools:
    """测试 _execute_pending_tools 的事件广播和 context 追加。"""

    def _make_bridge(self, loop):
        from mutbot.web.agent_bridge import AgentBridge
        from mutbot.session import AgentSession

        agent = MagicMock()
        agent.tools.dispatch = AsyncMock()
        agent.context.messages = []
        broadcast_calls = []
        persist_calls = []
        session = AgentSession(id="test-session", workspace_id="ws1", title="Test")

        async def mock_broadcast(session_id, data):
            broadcast_calls.append(data)

        bridge = AgentBridge(
            session_id="test-session",
            agent=agent,
            loop=loop,
            broadcast_fn=mock_broadcast,
            session=session,
            persist_fn=lambda: persist_calls.append(1),
        )
        return bridge, broadcast_calls, persist_calls

    @pytest.mark.asyncio
    async def test_broadcasts_full_lifecycle(self):
        """执行 pending tool 广播完整生命周期事件。"""
        loop = asyncio.get_running_loop()
        bridge, broadcasts, persists = self._make_bridge(loop)

        bridge._pending_tool_calls.append(("Test-tool", {"x": 1}))
        await bridge._execute_pending_tools()

        event_types = [b.get("type") for b in broadcasts]
        assert event_types[0] == "turn_start"
        # tool_exec_start 和 tool_exec_end 通过 serialize_stream_event 序列化
        assert any("tool_exec" in str(b) or b.get("type", "").startswith("tool_exec") for b in broadcasts)
        assert "turn_done" in event_types
        # 持久化
        assert len(persists) == 1

    @pytest.mark.asyncio
    async def test_appends_assistant_message_to_context(self):
        """执行后 context.messages 中有 assistant 消息和 ToolUseBlock。"""
        from mutagent.messages import ToolUseBlock
        loop = asyncio.get_running_loop()
        bridge, _, _ = self._make_bridge(loop)

        bridge._pending_tool_calls.append(("Test-tool", {}))
        await bridge._execute_pending_tools()

        assert len(bridge.agent.context.messages) == 1
        msg = bridge.agent.context.messages[0]
        assert msg.role == "assistant"
        tool_blocks = [b for b in msg.blocks if isinstance(b, ToolUseBlock)]
        assert len(tool_blocks) == 1
        assert tool_blocks[0].name == "Test-tool"

    @pytest.mark.asyncio
    async def test_clears_pending_after_execution(self):
        """执行后 pending 列表清空。"""
        loop = asyncio.get_running_loop()
        bridge, _, _ = self._make_bridge(loop)

        bridge._pending_tool_calls.append(("A", {}))
        bridge._pending_tool_calls.append(("B", {}))
        await bridge._execute_pending_tools()

        assert len(bridge._pending_tool_calls) == 0
        assert bridge.agent.tools.dispatch.call_count == 2
